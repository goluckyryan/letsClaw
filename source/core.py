"""core.py — the letsClaw core: conversations that outlive their clients.

One process holds many named sessions. Clients (terminal, WebUI, Discord) attach
to a session by name, several may attach to the same one, and all of them see the
same event stream. Nothing here knows what a terminal is.

Two rules shape everything below:

*   `Session.emit()` is synchronous and never blocks. The engine invokes its
    streaming callback from inside the SSE read loop, so anything that awaited a
    socket there would stall the model read for every session at once. Emit only
    appends to per-client deques; writer tasks own the sockets.
*   One turn at a time per session, enforced by a lock. A second submit is
    refused with `busy` rather than queued, because running it would silently
    execute against a context its sender never saw.
"""

import asyncio
import copy
import itertools
import json
import logging
import re
import secrets
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import yaml

import behavior
import config as configlib
import config_edit
import paths
import session as store
from llm_engine import ChatResult, RoundInterrupted
from ssh_forward import SSHForwardManager
from token_counter import count_messages, count_text, count_tools

logger = logging.getLogger("letclaw.core")

PROTOCOL_VERSION = 1

DEFAULT_ROLLOVER_PCT = 90
DEFAULT_ROLLOVER_MODE = "auto"
# The handoff is the only thing that survives a rollover besides one exchange, so
# it is the whole of the session's memory afterwards. Four sections now share it
# (RULED OUT was added so dead ends are not re-walked), and 800 could not hold
# them. It costs this many tokens of system prompt permanently — cheap next to
# re-exploring an approach that was already rejected.
HANDOFF_MAX_TOKENS = 1500
HANDOFF_MIN_TOKENS = 256
HANDOFF_MARGIN = 200
# The handoff never takes more than 1/this of the window. At 100k+ the absolute
# cap binds first and this does nothing; on a small model it is what stops the
# summary becoming a permanent tax on the context it was meant to save.
HANDOFF_WINDOW_SHARE = 20
# cl100k plus our framing under-counts this stack by ~30% (chat-template markers
# and the server's own tool rendering are invisible to us). Only used when the
# server withholds usage: rolling over early costs a summary, late costs a 400.
ESTIMATE_SCALE = 1.6

SESSION_MARKER = "\n=== SESSION ===\n"
CARRYOVER_MARKER = "\n\n=== CONTINUED SESSION ===\n"

# Seconds held back from turn_timeout so the closing round still fits inside the
# wall after a long think. Thinking stops at turn_timeout - this.
# Slack left between the pad and the context ceiling. The pad rides in the
# prompt and grows every round, and our token count runs low (see
# ESTIMATE_SCALE), so the headroom wall trips this far short of the real edge.
PAD_CONTEXT_MARGIN = 2000
# How many times a server that cannot resume is asked to conclude before we give
# up. Only the fallback path uses it; resumable servers never come here.
FORCED_CONCLUSION_TRIES = 3
# Below this much free context there is no room to think in, so the pad is not
# worth starting — the pre-turn check rolls over instead where it may.
MIN_THINK_HEADROOM = 4000
# How much of a compacted pad stays word-for-word. A checkpoint replaces the
# distant past, but the model must still resume *mid-sentence*, so the tail it
# left off in is never summarised.
PAD_TAIL_TOKENS = 2000
# How often the live thinking-token count goes out to the clients. Fast enough
# to read as a counter rather than a stopwatch, slow enough that the tokeniser
# runs a few times a second instead of once per delta.
REASONING_STAT_INTERVAL = 0.25

# How long close() waits for a cancelled turn to unwind before persisting
# anyway. Long enough for an exec child to take its SIGKILL and for the model
# read to abort, short enough that a wedged turn cannot hold up a systemctl
# stop until its timeout fires and SIGKILLs the core mid-write.
CLOSE_TURN_GRACE = 10

def ev(t, **kw):
    """An event is just a tagged dict; 13 types don't warrant a class each."""
    return {"t": t, **kw}


def now_stamp():
    """Local wall-clock time, seconds: when a message happened.

    Display metadata for the clients' timestamps — a history message and the
    events that draw it carry the same stamp, so a live turn and its replay
    read identically. Never sent to the model: api_messages() drops it.
    """
    return datetime.now().isoformat(timespec="seconds")


def api_messages(history):
    """History as the model sees it, display-only keys stripped.

    The `at` stamp is for the clients' clocks, not the prompt: it would ride
    in every request as invisible overhead, and a server that echoes unknown
    keys could surface it in the answer.
    """
    return [{k: v for k, v in m.items() if k != "at"} for m in history]


class Subscriber:
    """One attached client's outbound lane.

    Bounded on purpose: a half-open peer accepts a few hundred KB into socket
    buffers and then blocks for minutes of TCP retransmit. The turn must never
    wait for that, so the queue overflows and the client is dropped — it can
    reconnect and replay by seq.
    """

    def __init__(self, wants_reasoning=True, maxlen=2000):
        self.queue = deque(maxlen=maxlen)
        self.wake = asyncio.Event()
        self.wants_reasoning = wants_reasoning
        self.overflowed = False

    def push(self, event):
        # Only the thinking *text* is withheld. reasoning_stat is a token count,
        # and a client that hides the thinking is exactly the one with nothing
        # else to show for a two-minute think, so the count always goes out.
        if event["t"] == "reasoning" and not self.wants_reasoning:
            return
        if len(self.queue) == self.queue.maxlen:
            self.overflowed = True  # writer task will close the connection
        self.queue.append(event)
        self.wake.set()


def settled(history):
    """A copy of `history` with any unfinished trailing turn rolled back.

    What goes to disk should be a conversation someone could pick up, and a
    turn can stop part-way through. close() does cancel the turn tasks and wait
    for them, but only for CLOSE_TURN_GRACE: a turn blocked on a model that
    stopped answering outlives that and is abandoned mid-shape. And nothing is
    joined at all when the process does not get to run close() — a kill -9, an
    OOM, a power cut. Three shapes mean "this turn did not finish":

      assistant with tool_calls   no results came back
      role="tool"                 results whose asking turn is being unwound
      role="user"                 the question the model never got to answer

    Popped back to front, so a whole interrupted turn unwinds as a unit and what
    is left ends on a plain assistant message — or on the system message alone.
    The live history is untouched: the clients have already drawn it.
    """
    out = list(history)
    while out:
        last = out[-1]
        if last.get("tool_calls") or last.get("role") in ("tool", "user"):
            out.pop()
        else:
            break
    return out


def truncated_tool_calls(result):
    """Tool calls whose arguments did not finish generating.

    A round cut off at the token cap can stop in the middle of a tool call's
    JSON. Such a call must never run: `rm -rf /tmp/scratch` truncated to
    `rm -rf /` is the same shape of accident, and the half-written JSON also
    fails the server's own validation on the next request, killing the turn.
    """
    bad = []
    for tc in result.tool_calls or []:
        args = (tc.get("arguments") or "").strip()
        if not args:
            continue  # a no-argument call is legitimately empty
        try:
            json.loads(args)
        except (ValueError, TypeError):
            bad.append(tc)
    return bad


def strip_tool_markup(text):
    """Drop tool-call markup a resumed round may have written as prose.

    The raw transport talks to /completion, which has no tool-call parser, so a
    model that decides mid-answer to run `exec` emits the template's own
    <tool_call> XML as plain text. It cannot be honoured — a resumed round is
    past the point where tools can run — and showing the user raw markup is
    worse than showing them nothing, so it comes out.
    """
    if not text or "<tool_call>" not in text:
        return text
    cleaned = re.sub(r"<tool_call>.*?(?:</tool_call>|\Z)", "", text, flags=re.S)
    return cleaned.strip()


class Session:
    """One conversation: history, model, rollover policy, attached clients."""

    def __init__(self, name, manager, config):
        self.name = name
        self.manager = manager
        self.config = config
        conv = config.get("conversation", {})

        self.engine = None
        self.model_name = None
        self.base_behavior = ""
        self.behavior = ""
        # models/sessions/<name>.md — per session, which for the Discord bot
        # means per channel, since it names sessions after them.
        self.session_behavior = ""
        self.history = []
        self.tools = []
        self.schemas = []
        self.tools_tok = 0

        # int_or, not int: this runs on every session creation, so a config
        # that says `max_iterations: eight` would not fail once and loudly at
        # startup — it would fail here, for every session anyone tries to open,
        # for the life of the process, with /reload unable to help because the
        # value is already committed. See reload() for the other half of this.
        self.max_tool_rounds = configlib.int_or(
            (config.get("tools") or {}).get("max_iterations", 8), 8)
        # int() because YAML gives a string for `max_history_messages: "50"`,
        # and `len(history) > "50"` raises on 3.x rather than comparing.
        # 0 (or a negative, or junk) means "never trim", matching
        # rollover_at_percent's documented `0 = never` — and matching what the
        # code accidentally did before, since history[-0:] is the whole list,
        # not the empty one the number literally asks for.
        self.max_history = configlib.int_or(conv.get("max_history_messages", 100), 100)
        # Per session, never on the shared config dict: a session that disables
        # its own rollover must not disable everyone else's.
        self.rollover_pct = self._configured_rollover_pct()
        self.rollover_mode = str(conv.get("rollover_mode", DEFAULT_ROLLOVER_MODE)).lower()
        if self.rollover_mode not in ("auto", "ask", "off"):
            logger.warning("session %s: bad rollover_mode %r, using auto",
                           name, self.rollover_mode)
            self.rollover_mode = "auto"
        # Roll over before a turn that has no room left to think in, rather than
        # after one whose thinking was squeezed out. Auto mode only.
        self.rollover_before_think = bool(conv.get("rollover_before_think", True))
        self.prompt_timeout = configlib.int_or(conv.get("prompt_timeout", 60), 60)
        self.declined_at = None
        self.warned_over = False
        # Everything this session has ever asked the model to generate. An
        # odometer, not a gauge: /clear empties the conversation but does not
        # rewind what it cost, and it carries across a restart with the session.
        self.total_output = 0
        # How many times this session has rolled over (auto, /new, or an
        # accepted ask). Like total_output it survives /clear and a restart —
        # it counts the session's life, not the current window.
        self.rollover_count = 0

        # Stable identity, unlike self.name, which the user may change at any
        # time. Everything written to disk carries this, so the archives of one
        # conversation stay findable across every name it has ever had.
        # Overwritten by load_state for a session restored from its live file.
        #
        # 4 bytes = 8 hex, matching live_path's digest. Short enough to type into
        # a grep, and the index accumulates for the life of the install, so the
        # birthday bound is what matters rather than the live session count: at
        # 8 hex a thousand sessions collide with probability ~0.01%, at 4 hex
        # a hundred collide with probability ~7% — and a collision silently
        # merges two conversations in the index, which is worse than no index.
        self.session_id = secrets.token_hex(4)

        self.lock = asyncio.Lock()
        self.subscribers = set()
        self.dead = False       # deleted; sockets still on it must be re-homed
        self._seq = itertools.count(1)
        self.last_seq = 0
        self.turn_events = []   # current turn only; history serves fresh attaches
        self.turn_id = 0
        self.pending_ask = None  # (request_id, Future)
        # The turn in flight, if any. On the session rather than on whichever
        # socket happened to start it: a turn belongs to the conversation, and
        # every client attached to it watches the same output, so every one of
        # them must be able to stop it. Held by the server as a local, /stop
        # worked only from the submitting socket — so a second tab could not
        # stop what it was watching, and neither could the first one after a
        # reconnect handed it a fresh handler with no handle in it.
        self._turn_task = None
        self._last_used = None   # measured size of the next prompt, if known
        # Set to the OSError text when a rollover could not archive the window.
        # An automatic rollover will not retry while this is set: the disk that
        # was full one turn ago is still full, and retrying every turn would
        # turn one failure into an error on every message. /rollover ignores it,
        # because a person asking again has usually just fixed the cause.
        self.rollover_blocked = None
        # Serialises this session's own writes to state/live/. Not self.lock:
        # every save point below is already inside it.
        self._save_lock = asyncio.Lock()
        self._persisted = False  # has this session ever been written to disk?
        # A reload that arrived mid-turn, waiting for the turn to end.
        self._pending_config = None
        # /steering interjections queued for the turn in flight. add_steering appends
        # from the socket reader (lock-free — the turn holds self.lock for its
        # whole duration, and taking it there would deadlock); _turn drains
        # them, because it alone may move the history.
        self._steering = []

        # The append-only record of what this session generates — reasoning
        # included, tool output untruncated. Opened on the first thing worth
        # recording and closed by a rollover; see store.Journal.
        self.journal_on = bool(conv.get("journal", True))
        self._journal = None

    # ---- events ----------------------------------------------------------

    def emit(self, event):
        """Fan out one event. Synchronous and non-blocking, by contract."""
        self.last_seq = next(self._seq)
        event["seq"] = self.last_seq
        event["session"] = self.name
        # Deltas are append-only, so coalescing a backlog is lossless and keeps
        # a 2000-delta turn from replaying token by token.
        if event["t"] == "text":
            if self.turn_events and self.turn_events[-1]["t"] == "text":
                self.turn_events[-1]["delta"] += event["delta"]
                self.turn_events[-1]["seq"] = event["seq"]
            else:
                self.turn_events.append(dict(event))
        # Both are live-only, never replayed: the thinking text because it is not
        # kept, its running count because a reconnect would otherwise replay
        # hundreds of superseded figures to land on the one that is current.
        elif event["t"] not in ("reasoning", "reasoning_stat"):
            self.turn_events.append(dict(event))
        for sub in self.subscribers:
            sub.push(event)
        return event

    def attach(self, sub):
        self.subscribers.add(sub)

    def detach(self, sub):
        self.subscribers.discard(sub)

    def snapshot(self, last_seq=0):
        """What a (re)attaching client needs: state, plus any missed turn events."""
        missed = [e for e in self.turn_events if e["seq"] > last_seq] if last_seq else []
        # Built directly rather than via emit(), so it must stamp these itself.
        return ev("hello",
                  session=self.name,
                  seq=self.last_seq,
                  proto=PROTOCOL_VERSION,
                  model=self.model_name,
                  budget=self.budget,
                  busy=self.busy,
                  theme=configlib.config_theme(self.config),
                  pin=configlib.config_pin(self.config),
                  messages=[m for m in self.history if m.get("role") != "system"],
                  rollover={"mode": self.rollover_mode, "percent": self.rollover_pct,
                            "count": self.rollover_count},
                  missed=missed,
                  gap=bool(last_seq and missed and missed[0]["seq"] > last_seq + 1))

    # ---- state -----------------------------------------------------------

    @property
    def budget(self):
        return getattr(self.engine, "context_length", configlib.DEFAULT_CONTEXT_LENGTH)

    @property
    def busy(self):
        """Is a turn in flight?

        The lock alone is not enough: start_turn creates a task that does not
        reach `async with self.lock` until the loop next runs it, and two
        submits arriving in one batch would both pass a lock-only test.
        """
        return self.lock.locked() or bool(self._turn_task
                                          and not self._turn_task.done())

    def estimate(self, messages=None):
        """Deliberately conservative context size, for servers that hide usage."""
        msgs = self.history if messages is None else messages
        return int((count_messages(msgs) + self.tools_tok) * ESTIMATE_SCALE)

    def used_now(self):
        """How full the window is: measured where the server told us, else estimated.

        estimate() carries a deliberate +60% (ESTIMATE_SCALE) for servers that
        withhold usage. Spending that margin when the real figure is already
        known costs tens of thousands of phantom tokens on a large window, and
        the pre-turn headroom check reads it as context pressure that is not
        there.

        _last_used is only ever the size of the *current* window: every path
        that rebuilds the history from nothing clears it, so this is either a
        measurement of what is in the window now, or None.
        """
        return self._last_used if self._last_used is not None else self.estimate()

    def _configured_rollover_pct(self):
        """What the config says this session's rollover threshold should be.

        Read back rather than remembered because rollover_pct is also a
        runtime brake: _after_turn zeroes it when a *fresh* window already
        exceeds the threshold, since rolling over again would archive an empty
        conversation and hit the same wall immediately. That brake is tied to
        the window that tripped it, so the commands that throw the window away
        (/clear, /new) ask here for the configured value again instead of
        leaving automatic rollover off for the life of the session.
        """
        conv = self.config.get("conversation", {})
        return configlib.int_or(
            conv.get("rollover_at_percent", DEFAULT_ROLLOVER_PCT) or 0,
            DEFAULT_ROLLOVER_PCT)

    def fresh_history(self, carryover=""):
        """System message: the three behavior layers, then the carryover.

        base (every session) → model (per model entry) → session (per session,
        so per channel for Discord) → carryover (the rollover handoff).

        The base (identity + tool notes) is in the prompt even when a session
        has neither of the other two. The carryover stays last, and that is
        load-bearing: carryover() recovers it by partitioning on its marker and
        taking everything after, so anything appended below it would be read
        back as part of the handoff and re-appended on the next rebuild,
        growing the prompt every turn.
        """
        content = self.base_behavior
        if self.behavior:
            content += "\n=== BEHAVIOR ===\n" + self.behavior
        if self.session_behavior:
            content += SESSION_MARKER + self.session_behavior
        if carryover:
            content += CARRYOVER_MARKER + carryover
        return [{"role": "system", "content": content}]

    async def reload_session_behavior(self):
        """Re-read models/sessions/<name>.md and rebuild the system message.

        Called when the name changes, since that is what picks the file.
        """
        entry = (self.config.get("models", {}) or {}).get(self.model_name) or {}
        _, _, self.session_behavior = await self.manager.behavior_for(
            entry, self.name)
        carried = self.carryover()
        body = [m for m in self.history if m["role"] != "system"]
        self.history = self.fresh_history(carried) + body

    async def refresh_behavior(self):
        """Re-read the three layers from disk, for a window about to be rebuilt.

        Only called from rollover, /clear and /new — the paths that replace the
        prompt prefix anyway. /reload deliberately does not call this on a live
        window; see SessionManager.reload.

        The caller rebuilds the history itself: this only moves the attributes
        that fresh_history reads, so one rebuild serves both.
        """
        # Reconcile the cache with disk first, then compare against what *this
        # session* is using. Both halves are needed, and the second is the one
        # that is easy to get wrong: /reload also reconciles the cache, so a
        # rollover that asked only "did disk change?" would be told no and keep
        # serving the old text for ever. The question is whether the session's
        # layers match the cache, not whether the cache just moved.
        await self.manager.refresh_behaviors()
        entry = (self.config.get("models", {}) or {}).get(self.model_name) or {}
        fresh = await self.manager.behavior_for(entry, self.name)
        was = (self.base_behavior, self.behavior, self.session_behavior)
        if fresh == was:
            return []
        (self.base_behavior, self.behavior, self.session_behavior) = fresh

        keys = self.manager.behavior_keys_for(entry, self.name)
        names = [Path(keys[i]).name if i < len(keys) else label
                 for i, label in enumerate(("base", "model", "session"))]
        moved = [names[i] for i in range(3) if was[i] != fresh[i]]
        self.emit(ev("notice", level="info",
                     text="behavior re-read from disk: " + ", ".join(moved)))
        return moved

    def carryover(self):
        """The handoff a rollover parked in the system message, or "".

        Anything that rebuilds history[0] has to put this back. It holds a summary
        the model wrote and the path to the archived transcript — regenerate the
        prompt without it and the session forgets what it was doing, with no way
        to recover it short of reading the transcript by hand.
        """
        if not self.history:
            return ""
        _, sep, tail = (self.history[0].get("content") or "").partition(CARRYOVER_MARKER)
        return tail if sep else ""

    def repair_history(self):
        """Roll the in-memory history back to its last settled shape.

        Delegates to settled() so memory and disk agree on what an unfinished
        turn is: an interrupted or crashed turn must never leave the server an
        unanswerable tool call (which 400s forever after), a dangling tool
        result, or a question the model never got to answer.
        """
        before = len(self.history)
        self.history = settled(self.history)
        if len(self.history) < before:
            logger.warning("session %s: rolled back %d unfinished turn message(s)",
                           self.name, before - len(self.history))

    # ---- config reload ---------------------------------------------------

    def apply_config(self, blob):
        """Take a reload's precomputed changes. Synchronous, deliberately.

        Everything that needs disk or a lock was done by SessionManager.reload
        before this was handed over, so this function only assigns. That matters
        for the deferred path: it runs inside run_turn's finally, which also
        executes while a CancelledError is propagating, and an await there can be
        interrupted a second time and leave the session half-updated.

        Settings this session changed for itself are left alone. The test is
        divergence, not equality with the new value: a field is reapplied only if
        it still holds what the *old* config said. Without it, a session whose
        rollover was auto-disabled at _after_turn would have the threshold handed
        straight back, and would disable it again on the next turn, forever.
        """
        self.tools = blob["tools"]
        self.schemas = blob["schemas"]
        self.tools_tok = blob["tools_tok"]

        for attr, was, now in blob["scalars"]:
            if getattr(self, attr) == was:
                setattr(self, attr, now)

        if blob["engine"] is not None and blob["engine"] is not self.engine:
            # The engine being dropped still owns an httpx connection pool. On
            # the immediate path reload() closes it afterwards; on the deferred
            # path reload() has already finished its cleanup, and it counted
            # this very engine as live precisely because the session still
            # pointed at it. So nothing would ever close it: one leaked pool
            # per reload that landed while the session was busy. Handed over
            # rather than closed here, because this function must not await.
            self.manager.retire_engine(self.engine)
            self.engine = blob["engine"]
        # history is deliberately untouched. The three behavior layers sit in
        # history[0], which is the prompt prefix the server has already
        # tokenised; rebuilding it here would cost every live session its KV
        # cache mid-conversation. An edited behavior file is already in the
        # manager's cache and is read by refresh_behavior() at the next
        # rollover, /clear or /new — the paths that replace that prefix anyway.

        if blob["lost_model"]:
            self.emit(ev("notice", level="warn",
                         text=f"model '{self.model_name}' is no longer in the config — "
                              f"this session keeps using it, but /model cannot return to it"))
        self.emit(ev("session_state", what="reloaded", model=self.model_name,
                     budget=self.budget, theme=configlib.config_theme(self.config),
                     pin=configlib.config_pin(self.config), changed=blob["changed"]))

    # ---- the journal -----------------------------------------------------

    async def jot(self, header, body=""):
        """Record one block, if journalling is on. Never raises, never blocks
        for long: the write is off the event loop and a failed one is dropped.

        Called from inside a turn, so it must stay cheap — it is a file append
        after the generation it records, not on the token path.
        """
        if not self.journal_on:
            return
        if self._journal is None:
            self._journal = store.Journal(self.session_id, self.name,
                                          self.model_name, self.config)
        await self._journal.write(header, body)

    async def close_journal(self):
        """End the current segment. Returns its path, or None if it is empty."""
        journal, self._journal = self._journal, None
        if journal is None:
            return None
        try:
            return await journal.close()
        except OSError as e:
            self.emit(ev("notice", level="warn",
                         text=f"could not close the journal: {e}"))
            return None

    # ---- persistence -----------------------------------------------------

    async def persist(self):
        """Write this session to state/live/ so a restart does not lose it.

        Called after every turn and after anything that rewrites the history, so
        a kill -9 costs the turn in flight and nothing else. Never takes
        self.lock — every caller already holds it — and never raises: failing to
        save is worth a log line, not a dead session.

        A session nobody has spoken in is not written. Attaching to a name is how
        you create it, so a typo would otherwise leave a file that, since nothing
        expires, would sit in the sidebar for good. Once written it keeps its file
        even when emptied by /clear: the conversation is gone, the session is not.
        """
        history = settled(self.history)
        body = [m for m in history if m.get("role") != "system"]
        if not body and not self._persisted:
            return
        payload = {
            "v": store.LIVE_SCHEMA,
            "name": self.name,
            # Deliberately additive rather than a LIVE_SCHEMA bump: a bump makes
            # load_live skip every file written before it, which would throw away
            # the user's live conversations to gain an id. load_state mints one
            # for a file that predates this field instead.
            "session_id": self.session_id,
            "model": self.model_name,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            # Restored so a client reconnecting with an old last_seq is told it
            # missed something, instead of being silently handed a numbering
            # that restarted at 1 underneath it.
            "last_seq": self.last_seq,
            "turn_id": self.turn_id,
            # Runtime-mutated policy. rollover_pct in particular is zeroed when
            # even a fresh window trips the threshold; re-reading it from config
            # would put the session straight back into that loop.
            "rollover_pct": self.rollover_pct,
            "declined_at": self.declined_at,
            "warned_over": self.warned_over,
            "last_used": self._last_used,
            "total_output": self.total_output,
            "rollover_count": self.rollover_count,
            # The system message goes too. A rolled-over session carries a
            # === CONTINUED SESSION === block holding a model-written handoff and
            # a transcript path — regenerating the prompt would destroy it.
            "messages": history,
        }
        try:
            async with self._save_lock:
                await store.save_live(payload, self.config)
            self._persisted = True
        except (OSError, TypeError, ValueError) as e:
            logger.warning("session %s: could not save: %s", self.name, e)

    def load_state(self, payload):
        """Take the non-history half of a saved payload. See restore()."""
        # A file written before session_id existed has none; the one minted in
        # __init__ stands and is persisted on the next save.
        self.session_id = payload.get("session_id") or self.session_id
        # int_or throughout: this is a file on disk, and a torn write or a
        # hand-edit makes any of these a string or None. restore() walks every
        # session in state/live, so one unparseable number raising here would
        # cost not just its own conversation but every one after it in the
        # directory listing. A bad field is worth a log line and a default.
        self.last_seq = configlib.int_or(payload.get("last_seq") or 0, 0)
        self._seq = itertools.count(self.last_seq + 1)
        self.turn_id = configlib.int_or(payload.get("turn_id") or 0, 0)
        if payload.get("rollover_pct") is not None:
            self.rollover_pct = configlib.int_or(payload["rollover_pct"],
                                                 self.rollover_pct)
        self.declined_at = payload.get("declined_at")
        self.warned_over = bool(payload.get("warned_over"))
        # None stays None — "not measured" is a real state here, distinct from
        # zero — but anything else has to be a number, because used() hands it
        # straight to the arithmetic behind the context gauge.
        last_used = payload.get("last_used")
        self._last_used = None if last_used is None else configlib.int_or(last_used, None)
        self.total_output = configlib.int_or(payload.get("total_output") or 0, 0)
        self.rollover_count = configlib.int_or(payload.get("rollover_count") or 0, 0)
        self._persisted = True

    # ---- the turn --------------------------------------------------------

    def start_turn(self, text, origin=None):
        """Run one turn as a task owned by the session. Returns it, or None if
        a turn is already in flight.

        The gate is here rather than in the caller so that _turn_task always
        points at a turn that is really running. A refused submit returns
        immediately, and letting that task overwrite the handle would leave
        /stop cancelling something already finished while the real turn ran on.
        """
        if self.busy:
            self.emit(ev("busy", reason="a turn is already running in this session"))
            return None
        task = asyncio.create_task(self.run_turn(text, origin=origin))
        self._turn_task = task

        def done(t):
            # Only if it is still ours: by the time this runs the next turn may
            # already have claimed the slot.
            if self._turn_task is t:
                self._turn_task = None
            # run_turn handles its own exceptions, but persist() in its finally
            # can still raise. Retrieved so it is logged here rather than
            # surfacing later as "exception was never retrieved".
            if not t.cancelled() and t.exception() is not None:
                logger.error("session %s: turn task died", self.name,
                             exc_info=t.exception())

        task.add_done_callback(done)
        return task

    def stop_turn(self):
        """Cancel the turn in flight, whoever started it. True if there was one."""
        task = self._turn_task
        if task is None or task.done():
            return False
        task.cancel()
        logger.info("session %s: turn cancelled by /stop", self.name)
        return True

    def add_steering(self, text):
        """Queue a /steering for the turn in flight. True if it was taken.

        A /steering is extra information for a turn already running — the model
        picks it up at the next round. It is a real user message, so it enters
        the history (and is replayed to fresh attaches like any other); it is
        not started as a turn of its own. When nothing is running there is
        nothing to steer, so it is refused rather than queued into the next
        turn.

        The append is lock-free on purpose: the socket reader calls this, and
        taking self.lock here would deadlock against the turn that holds it.
        _turn drains the queue, and it alone may move the history.
        """
        text = (text or "").strip()
        if not text:
            return False
        if not self.busy:
            self.emit(ev("notice", level="warn",
                         text="no turn is running — just send a message"))
            return False
        self._steering.append(text)
        logger.info("session %s: /steering queued (%d pending)", self.name,
                    len(self._steering))
        return True

    async def run_turn(self, text, origin=None):
        """One user turn: model rounds, tool rounds, then rollover or trim.

        Refuses rather than queues when a turn is already running — see the
        module docstring.
        """
        # lock.locked(), not self.busy — the one place that difference is
        # deliberate. start_turn has already pointed _turn_task at the task
        # running this very call, so `busy` is True by construction here and
        # the turn would refuse itself. This check is only a backstop for a
        # caller that reaches run_turn directly instead of through start_turn;
        # the real gate against two concurrent submits is in start_turn.
        if self.lock.locked():
            self.emit(ev("busy", reason="a turn is already running in this session"))
            return
        async with self.lock:
            self.turn_id += 1
            self.turn_events.clear()
            # One stamp for the turn: the event and the history message that
            # draw the same question must agree, or a replay across a second
            # boundary would read a different time than the live turn.
            turn_at = now_stamp()
            self._turn_at = turn_at
            self.emit(ev("turn_start", turn_id=self.turn_id, text=text, origin=origin,
                         at=turn_at))
            try:
                if self.engine and self.engine.turn_timeout:
                    # The turn's hard wall (models.<name>.turn_timeout). A wall
                    # hit mid-round cancels the turn; the cleanup below is the
                    # same as a /stop, and the finally persists and announces.
                    await asyncio.wait_for(self._turn(text),
                                           timeout=self.engine.turn_timeout)
                else:
                    await self._turn(text)
            except asyncio.TimeoutError:
                wall = self.engine.turn_timeout
                self.repair_history()
                self.emit(ev("notice", level="warn",
                             text=(f"turn hit the {wall // 3600} h wall — cancelled"
                                   if wall >= 3600 else
                                   f"turn hit the {wall} s wall — cancelled")))
            except asyncio.CancelledError:
                self.repair_history()
                self.emit(ev("notice", level="warn", text="turn cancelled"))
                raise
            except Exception as e:                     # never kill the session
                logger.exception("session %s: turn failed", self.name)
                self.repair_history()
                self.emit(ev("error", msg=f"{type(e).__name__}: {e}", fatal=False))
            finally:
                # Saved before the turn is announced over, so turn_end means the
                # conversation is on disk and not merely in memory — anything
                # that acts on turn_end (a client, a test, a shutdown script)
                # would otherwise be racing the write. Still inside the lock, so
                # the history cannot move underneath it, and in its own finally
                # so a failing save can never swallow the event.
                try:
                    await self.persist()
                finally:
                    self.emit(ev("turn_end", turn_id=self.turn_id))
                # A /steering that landed in the last instant — after _turn finished
                # but before the turn was announced over: nothing is running any
                # more, so say so rather than let it queue into the next turn.
                if self._steering:
                    self._steering = []
                    self.emit(ev("notice", level="warn",
                                 text="no turn is running — just send a message"))
                # A reload that landed mid-turn. Applied here rather than made to
                # wait for the lock, so a session that is never idle still picks
                # it up. Still inside the lock, so nothing moves underneath it;
                # its own try, so a bad apply cannot kill the session; and it runs
                # on the cancellation path too, since run_turn re-raises after
                # this finally.
                if self._pending_config is not None:
                    blob, self._pending_config = self._pending_config, None
                    try:
                        self.apply_config(blob)
                    except Exception:
                        logger.exception("session %s: deferred config reload failed",
                                         self.name)

    async def _turn(self, text):
        # Before the user message lands, so a roll starts the fresh session with
        # this turn's question in it rather than one turn late.
        await self._headroom_check()
        self.history.append({"role": "user", "content": text,
                             "at": getattr(self, "_turn_at", None) or now_stamp()})
        user_tok = count_text(text)
        # After the headroom check, so the question opens the segment it is
        # actually answered in rather than the one that just rolled away.
        await self.jot(f"turn {self.turn_id} user", text)

        turn_t0 = time.monotonic()
        tools_used = 0
        last_ttft = None
        last_prompt = None   # size of the last prompt actually sent
        full_response = ""
        had_error = False
        last_finish = None   # finish_reason of the last round that returned
        last_reasoning = ""  # that round's reasoning — the seed of a thinking pad
        reasoning_parts = []
        # What the round currently streaming has emitted, so a /steering interrupt
        # can keep exactly what the client already saw. Cleared at each round's
        # start; read only when a round is interrupted.
        emitted = []
        reason_emitted = []
        # The live thinking-token figure the clients count up with. Produced here
        # rather than in the browser so it is the same tiktoken count the ⚡ line
        # ends the turn on — a chars/4 guess lands 10-20% away on Qwen, and two
        # numbers for one quantity read as a bug. Spans the whole turn: every
        # round, every pad resume, and the waits between them.
        reason_tok = 0         # counted so far
        reason_pending = []    # deltas that arrived since the last count
        reason_t0 = None       # first reasoning delta of the turn
        reason_sent = 0.0      # monotonic of the last figure pushed

        # Output accounting. A turn can span several rounds, and every one of
        # them generates: text, reasoning, and the tool-call arguments. The
        # server's completion_tokens covers all three exactly, so it is summed
        # when available — but only trusted if *every* round reported one, since
        # a partial sum silently understates. Otherwise the local estimate,
        # accumulated the same way, and the figure goes out marked estimated.
        rounds = out_reported = 0
        out_measured = out_estimated = 0

        def push_reasoning_stat(now, flush=False):
            """Count whatever has arrived since the last push and send the total.

            Counted a slice at a time, so a push costs the new text rather than
            the whole turn's thinking. Each slice ends just *before* its last
            space, and the rest is held back to open the next one: a token
            carries its leading space (" alpha" is one token, "alpha" and " "
            are two), so cutting anywhere else inflates the running sum. `flush`
            counts the held-back tail too, for when no more is coming.
            """
            nonlocal reason_tok, reason_sent, reason_t0
            if reason_t0 is None:
                reason_t0 = turn_t0
            if reason_pending:
                text = "".join(reason_pending)
                cut = len(text) if flush else text.rfind(" ")
                if cut > 0:
                    reason_tok += count_text(text[:cut])
                    reason_pending.clear()
                    if text[cut:]:
                        reason_pending.append(text[cut:])
            reason_sent = now
            self.emit(ev("reasoning_stat", tokens=reason_tok,
                         thinking=round(now - reason_t0, 1)))

        def tally(result, history_shaped=True):
            """Fold one round's usage in. Shared by the loop and its fallback.

            history_shaped=False for a resumed round. Its prompt carries the
            thinking pad, which is thrown away at the end of the turn and never
            reaches history, so letting it set last_prompt would measure the
            window as tens of thousands of tokens fuller than it really is — and
            trip a rollover on the next check. Output still counts: those tokens
            were genuinely generated.
            """
            nonlocal last_prompt, rounds, out_reported, out_measured, out_estimated
            nonlocal reason_tok   # re-synced below, on the round boundary
            rounds += 1
            if history_shaped and result.prompt_tokens is not None:
                last_prompt = result.prompt_tokens
            if result.completion_tokens is not None:
                out_reported += 1
                out_measured += result.completion_tokens
            if result.reasoning:
                reasoning_parts.append(result.reasoning)
                # Re-sync the live count on the round boundary: counting the
                # whole thing at once settles any drift the slices left, and
                # picks up a round whose reasoning arrived on the result instead
                # of through on_reasoning (the non-streaming path).
                # Never downwards, though — a counter that ticks back a few
                # tokens mid-think reads as a bug, and the ⚡ line quotes this
                # same figure, so the two still cannot disagree.
                reason_pending.clear()
                reason_tok = max(reason_tok, count_text("".join(reasoning_parts)))
                push_reasoning_stat(time.monotonic())
            # Only for a round the server did NOT report, so the two counters
            # partition the rounds instead of overlapping. Their sum is then
            # the best figure available for the turn whatever the mix: exact
            # where the server spoke, estimated only where it did not. Adding
            # an estimate for every round — as this did — meant a turn where
            # three rounds of five reported had to throw all three exact
            # numbers away and estimate the lot, because the total was no
            # longer a sum of anything.
            if result.completion_tokens is None:
                out_estimated += (count_text(result.text or "")
                                  + count_text(result.reasoning or ""))
                for tc in result.tool_calls or []:
                    out_estimated += (count_text(tc.get("name") or "")
                                      + count_text(tc.get("arguments") or ""))

        async def call_round(tools=None, overrides=None, pad=None, closing=False,
                             extra_messages=None):
            """One model round: stream, tally, and return the result.

            Shared by the tool loop, the thinking-pad resumes and the fallback
            conclusion rounds, so every one of them lands in the same accounting
            and emits the same events. With `pad` set the round is a resume —
            the pad is handed back to the model instead of the question being
            re-asked, and tools are never offered.
            """
            nonlocal last_ttft, last_finish, last_reasoning
            t0 = time.monotonic()
            ttft_box = {"v": None}
            # A tool-loop round (tools offered, no pad) is interruptible: a /steering
            # queued while it streams raises RoundInterrupted on the next chunk,
            # so the model stops mid-thought and re-plans with the correction
            # instead of finishing a long wrong path. Pad resumes and the
            # no-tools closing round are not — a /steering there waits for the next
            # boundary, and the turn's end says so rather than dropping it.
            interruptible = pad is None and tools is not None
            emitted.clear()
            reason_emitted.clear()

            def on_text(chunk, box=ttft_box):
                if box["v"] is None:
                    box["v"] = time.monotonic() - t0  # measured at the model, not the client
                # First words of the round: the thinking behind them is over, so
                # settle the count before this delta goes out. Clients close the
                # thinking off when text arrives, and a figure a throttle window
                # short is the one they would close it on.
                if reason_pending:
                    push_reasoning_stat(time.monotonic(), flush=True)
                if interruptible and self._steering:
                    raise RoundInterrupted()
                first = not emitted
                emitted.append(chunk)
                # First words of the block: the client opens its message there,
                # so that delta carries the timestamp and the rest do not.
                self.emit(ev("text", delta=chunk,
                             **({"at": now_stamp()} if first else {})))

            def on_reasoning(chunk):
                nonlocal reason_t0
                if interruptible and self._steering:
                    raise RoundInterrupted()
                first = not reason_emitted
                self.emit(ev("reasoning", delta=chunk,
                             **({"at": now_stamp()} if first else {})))
                now = time.monotonic()
                if reason_t0 is None:
                    reason_t0 = now
                reason_pending.append(chunk)
                reason_emitted.append(chunk)
                if now - reason_sent >= REASONING_STAT_INTERVAL:
                    push_reasoning_stat(now)

            # extra_messages ride along for this one round without entering the
            # conversation: the closing round needs the "do not call tools"
            # directive, but a turn's history must not collect scaffolding.
            # Display-only keys stripped at the boundary: the model sees
            # role/content/tool_calls, never the timestamps.
            msgs = api_messages(self.history) + list(extra_messages or [])
            if pad is None:
                result = await self.engine.chat_with_tools(
                    msgs, tools=tools, on_text=on_text,
                    on_reasoning=on_reasoning, overrides=overrides)
            else:
                result = await self.engine.resume_round(
                    msgs, pad, closing=closing, tools=tools,
                    max_tokens=(overrides or {}).get("max_tokens"),
                    on_text=on_text, on_reasoning=on_reasoning)
            if ttft_box["v"] is not None:
                last_ttft = ttft_box["v"]
            # A resumed round's prompt is history + pad, not history: see tally.
            tally(result, history_shaped=pad is None)
            # Journalled here rather than at the call sites, so every path
            # through the model lands in the record by construction: the tool
            # loop, each pad resume, a compaction, the closing round. The pad
            # in particular exists nowhere else — it never enters history — so
            # this is the only place its reasoning can be kept at all.
            phase = "closing" if closing else ("resume" if pad is not None else "round")
            tag = f"turn {self.turn_id} {phase} {rounds}"
            if result.reasoning:
                await self.jot(f"{tag} reasoning", result.reasoning)
            if result.text:
                await self.jot(f"{tag} text", result.text)
            last_finish = result.finish_reason
            last_reasoning = result.reasoning or ""
            return result

        async def run_tools(result):
            """Record one round's tool calls and execute them.

            Shared by the tool loop and by a thinking pad's conclusion, which may
            decide the right ending is an action rather than a sentence.
            """
            nonlocal tools_used
            # A call cut off mid-JSON is never executed and never stored as it
            # came: the arguments go in as {} so the history stays valid for the
            # server, and the model is told why nothing ran.
            cut = {id(tc) for tc in truncated_tool_calls(result)}
            self.history.append({
                "role": "assistant",
                "content": result.text or "",
                "at": now_stamp(),
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"],
                                  "arguments": "{}" if id(tc) in cut else tc["arguments"]}}
                    for tc in result.tool_calls
                ],
            })
            # Reserve every result slot before running anything, so a turn
            # torn off mid-loop still leaves a well-formed history.
            slots = {}
            for tc in result.tool_calls:
                # Reserved before running: the stamp is the reservation moment,
                # rewritten when the result lands (run_tools below).
                msg = {"role": "tool", "tool_call_id": tc["id"],
                       "content": "error: cancelled", "at": now_stamp()}
                slots[tc["id"]] = msg
                self.history.append(msg)

            for tc in result.tool_calls:
                # Emitted BEFORE the call: a 120-second exec used to be silent.
                self.emit(ev("tool_call", id=tc["id"], name=tc["name"],
                             arguments=tc["arguments"], at=now_stamp()))
                tag = f"turn {self.turn_id} round {rounds}"
                # Recorded before the call runs, so a command that hangs until
                # the turn wall still leaves behind what was attempted.
                await self.jot(f"{tag} tool_call {tc['name']} {tc['id']}",
                               tc["arguments"])
                tool = next((t for t in self.tools if t.name == tc["name"]), None)
                if id(tc) in cut:
                    out = ("error: the call was cut off at the output limit and "
                           "its arguments are incomplete, so it was not run. "
                           "Issue it again, more briefly.")
                    full = out
                    self.emit(ev("notice", level="warn",
                                 text=f"{tc['name']} call was truncated mid-arguments "
                                      "— not run"))
                elif tool is None:
                    out = full = f"error: tool '{tc['name']}' is not enabled"
                else:
                    # The full output goes to the journal and the clipped one to
                    # the model. Searching a record of elided middles finds
                    # nothing, which is the whole reason the record exists.
                    full, out = await tool.run_full(tc["arguments"])
                tools_used += 1
                slots[tc["id"]]["content"] = out
                slots[tc["id"]]["at"] = now_stamp()
                await self.jot(
                    f"{tag} tool_result {tc['name']} {tc['id']} {len(full)} chars", full)
                self.emit(ev("tool_result", id=tc["id"], name=tc["name"],
                             size=len(out), at=slots[tc["id"]]["at"]))

        def was_cut_off(result):
            """Reasoning ate the whole budget: thought hard, said nothing."""
            return (not result.text and not result.wants_tool
                    and result.finish_reason == "length"
                    and self.engine.reasoning)

        async def drain_steering():
            """Move queued /steering's into the history as user messages.

            Called only from here, where self.lock is held, so the history may
            move. Each interjection is a real user message: replayed to fresh
            attaches like any other, and shown live to every attached client
            by the steering event that carries the same text.
            """
            queued, self._steering = self._steering, []
            for text in queued:
                at = now_stamp()
                self.history.append({"role": "user", "content": text, "at": at})
                self.emit(ev("steering", text=text, turn_id=self.turn_id, at=at))
                await self.jot(f"turn {self.turn_id} steering", text)

        async def record_partial():
            """Keep what a /steering interrupted, so history matches the screen.

            The partial answer is appended as a plain assistant message — the
            same shape a finished round leaves — and the interrupted thinking
            goes to the journal, the only place a round's reasoning is kept.
            Nothing to keep if the round was still in reasoning and no answer
            text had been emitted.
            """
            if reason_emitted:
                await self.jot(f"turn {self.turn_id} interrupted reasoning",
                               "".join(reason_emitted))
            partial = "".join(emitted)
            if partial.strip():
                self.history.append({"role": "assistant", "content": partial,
                                     "at": now_stamp()})
                await self.jot(f"turn {self.turn_id} partial (steering)", partial)

        try:
            for _ in range(self.max_tool_rounds):
                # A /steering that landed while the previous tool ran (or before any
                # round) is drained here, before the model is asked again — the
                # same boundary a mid-stream one reaches, without interrupting
                # a round that is already between tools.
                await drain_steering()
                try:
                    result = await call_round(tools=self.schemas)
                except RoundInterrupted:
                    # A /steering landed mid-round: keep what the model already said
                    # (it is on the client's screen), hand it the correction,
                    # and let the next round re-plan. Nothing is lost — the
                    # partial answer and the interjection both reach history.
                    await record_partial()
                    await drain_steering()
                    continue
                if not result.wants_tool:
                    # A cut-off round is resumed rather than lost — see
                    # _think_pad. Its conclusion may be an answer or, if the
                    # thinking decided the ending is an action, a tool call;
                    # the latter rejoins this loop with the result in hand.
                    if was_cut_off(result):
                        result = await self._think_pad(call_round, turn_t0,
                                                       result.reasoning)
                        if result.wants_tool:
                            await run_tools(result)
                            continue
                    if self._steering:
                        # The answer is complete, but a /steering arrived as it was
                        # finishing — or during the pad resume above, which is
                        # not interruptible. Record the answer, hand over the
                        # correction, and let the next round answer it: a /steering
                        # is never dropped just for arriving a moment late.
                        if result.text:
                            self.history.append(
                                {"role": "assistant", "content": result.text,
                                 "at": now_stamp()})
                        await drain_steering()
                        continue
                    full_response = result.text
                    break

                await run_tools(result)
            else:
                self.emit(ev("notice", level="warn",
                             text=f"{self.max_tool_rounds} tool rounds used — "
                                  "asking for a final answer"))
                result = await call_round()
                if was_cut_off(result):
                    # Out of tool rounds, so this conclusion must be words.
                    result = await self._think_pad(call_round, turn_t0,
                                                   result.reasoning, allow_tools=False)
                full_response = result.text
        except asyncio.CancelledError:
            raise
        except Exception as e:
            had_error = True
            self.emit(ev("error", msg=f"{type(e).__name__}: {e}", fatal=False))
            if tools_used == 0:
                try:
                    full_response = await self.engine.chat(api_messages(self.history))
                    self.emit(ev("text", delta=full_response, at=now_stamp()))
                    # The one answer that never goes through call_round.
                    await self.jot(f"turn {self.turn_id} fallback text", full_response)
                    had_error = False
                except Exception as e2:
                    self.emit(ev("error", msg=f"{type(e2).__name__}: {e2}", fatal=False))
                    # repair_history(), not history.pop(): the comment this
                    # replaces asserted the last entry is the user message, and
                    # it need not be. A /steering that arrived while the first
                    # attempt was in flight has already been appended, so the
                    # pop took the steering line and left the unanswered user
                    # message behind — the one shape settled() exists to
                    # prevent, and the one the server 400s on. settled() unwinds
                    # the whole unfinished turn instead of guessing its length.
                    self.repair_history()
                    return

        # The closing round is the one round a /steering cannot interrupt and has no
        # next round to answer it: clear whatever is left rather than let it
        # bleed into the next turn, and say so — resending is one line.
        if self._steering:
            left, self._steering = self._steering, []
            self.emit(ev("notice", level="warn",
                         text=f"{len(left)} /steering arrived as the turn was ending — "
                              f"send it again"))

        # Deltas from a round that died before tally() could re-sync are still
        # uncounted here. Counting them now keeps the figure below — and the last
        # one the clients were given — the same number.
        if reason_pending:
            push_reasoning_stat(time.monotonic(), flush=True)

        # The two counters partition the rounds (see tally), so their sum is the
        # figure however the rounds split. out_exact is about how to *label* it,
        # not how to compute it: "measured" is a claim that every round supplied
        # a number, and a mixed turn must not make that claim — it renders with
        # the ~ prefix below and sets output_measured=False on the event.
        out_exact = rounds > 0 and out_reported == rounds
        out_tok = out_measured + out_estimated
        # Counted before the branches, so a turn that errored out after two good
        # rounds still records what those rounds generated. Rounds that never
        # returned never reached tally(), so they contribute nothing.
        self.total_output += out_tok

        if not had_error and full_response:
            self.history.append({"role": "assistant", "content": full_response,
                                 "at": now_stamp()})
            # How full the window is = what the NEXT prompt will cost: the prompt
            # just sent, plus the answer we appended. Deliberately not
            # prompt+completion — completion includes reasoning, which is thrown
            # away rather than kept, so that figure is inflated and can even fall
            # as the conversation grows.
            used = (last_prompt + count_text(full_response) + 4
                    if last_prompt is not None else self.estimate())
            self._last_used = used
            self.emit(ev("stats",
                         ttft=last_ttft,
                         user_tokens=user_tok,
                         llm_tokens=count_text(full_response),
                         # The running count the spinner has been showing all
                         # turn, not a fresh tally of the same text: one number,
                         # so the live figure and this one cannot disagree.
                         reasoning_tokens=reason_tok,
                         tool_calls=tools_used,
                         # Everything generated this turn, across every round:
                         # the answer, the reasoning, and the tool-call arguments.
                         # llm_tokens above is the final answer alone, which
                         # is the smaller and more useful number to read; this is
                         # the one that was actually paid for.
                         output_tokens=out_tok,
                         output_measured=out_exact,
                         output_total=self.total_output,
                         used=used, budget=self.budget,
                         measured=last_prompt is not None))
        elif not had_error:
            # The window still grew even though nothing was appended: the user
            # message went into history at the top of the turn, and the prompt
            # the server measured already includes it. Leaving _last_used at the
            # previous turn's value made used_now() report a window smaller than
            # the real one, so the headroom check never tripped and a reasoning
            # model that keeps returning empty could run the context to the wall
            # without a single rollover. No answer to add, so no answer term.
            if last_prompt is not None:
                self._last_used = last_prompt
            # No answer, but the generation still happened and still cost — say
            # how much, or a turn spent entirely on reasoning looks free.
            # finish_reason says whether the cap was actually hit, so the advice
            # matches the cause instead of guessing.
            if last_finish == "length" and not self.engine.reasoning:
                # The forced-conclusion loop is off, so the cap advice is still
                # the useful one; with it on, the loop said what it did already.
                self.emit(ev("notice", level="warn",
                             text="the answer hit the output cap mid-reasoning — "
                                  "raise max_output_tokens (or the model's "
                                  "max_tokens) and try again"))
            else:
                self.emit(ev("notice", level="info",
                             text=f"no answer — the model likely spent its budget on "
                                  f"reasoning ({'' if out_exact else '~'}{out_tok} output tok)"))

        # Rollover is evaluated only here: the tool loop has fully drained, so no
        # tool_call/tool_result pair can be split. A thinking pad never reaches
        # history — only the answer does — so a long think cannot drag the
        # rollover trip point forward. Keep it that way.
        if not had_error:
            await self._after_turn(getattr(self, "_last_used", None))

    def _pad_headroom(self):
        """Context tokens a thinking pad may still grow into."""
        answer = self.engine.max_output or (self.engine.extra_params.get("max_tokens") or 0)
        return self.budget - self.used_now() - answer - PAD_CONTEXT_MARGIN

    async def _headroom_check(self):
        """Roll over *before* a turn when there is no room left to think in.

        A session sitting just under the rollover threshold has a full window
        and no headroom, so the pad's context wall would trip on the first round —
        thinking would be off exactly where a hard question needs it. Rolling
        first gives the turn a fresh window. Only in auto mode: `ask` must not
        put a question before the turn has even started, and `off` means off.
        """
        if not (self.engine and self.engine.reasoning and self.budget):
            return
        if self.rollover_mode != "auto" or not self.rollover_before_think:
            return
        # rollover_at_percent: 0 means never, and _after_turn already honours it.
        # This path has to as well, or a session that switched rollover off — by
        # config, or by the auto-disable when even a fresh window trips the
        # threshold — would still be rolled over from here, which is both a
        # surprise and, in the auto-disable case, the loop that guard prevents.
        if not self.rollover_pct:
            return
        # Same guard as _maybe_roll, for the same reason: this fires before
        # every turn, so an unarchivable window would error on every one.
        if self.rollover_blocked:
            return
        if self._pad_headroom() >= MIN_THINK_HEADROOM:
            return
        used = self.used_now()
        self.emit(ev("notice", level="info",
                     text=f"only {max(0, self._pad_headroom())} tok of thinking room "
                          f"left — rolling over before the turn"))
        await self.roll_over("no thinking headroom", used,
                             int(self.budget * self.rollover_pct / 100))

    async def _think_pad(self, call_round, turn_t0, seed, allow_tools=True):
        """Resume a cut-off round instead of re-asking the question.

        The model's reasoning and its answer come out of one budget, so a round
        can end having thought hard and said nothing. Rather than bin that work,
        the reasoning becomes a *pad*: handed back unclosed, so the model carries
        on mid-sentence. The pad grows round by round until a wall trips, and then
        the answer phase closes the thinking block itself — which both forces an
        answer (models often never close it) and makes the answer's budget
        genuinely separate, since past `</think>` no more reasoning can happen.

        The pad lives here and nowhere else: it never enters history, so it costs
        nothing after the turn and cannot move the rollover point.
        """
        pad = seed or ""
        lap_cap = self.engine.extra_params.get("max_tokens") or None
        answer_cap = self.engine.max_output or lap_cap
        # Thinking must stop early enough that the closing round still fits
        # inside the turn wall that run_turn already holds us to.
        wall = self.engine.turn_timeout
        reserve = getattr(self.engine, "answer_time_reserve", configlib.DEFAULT_ANSWER_TIME_RESERVE)
        think_deadline = (turn_t0 + max(0, wall - reserve)) if wall else None

        transport = await self.engine.resume_transport()
        if transport == "off":
            return await self._forced_conclusion(call_round, answer_cap)
        # Only the chat transport parses tool calls; see the answer phase below.
        tools_ok = allow_tools and transport == "chat"

        rounds = compactions = 0
        # Unbounded on purpose. There is no round budget, because a count is not
        # a limit the machine actually has: the pad is stopped by time, by
        # context, or by the model running dry, and each of those is real. The
        # loop still cannot spin — see the no-progress guard at the bottom.
        while True:
            if think_deadline and time.monotonic() >= think_deadline:
                self.emit(ev("notice", level="info",
                             text=f"thinking stopped at the {reserve} s answer reserve "
                                  f"after {rounds} round(s) — concluding"))
                break
            if self._pad_headroom() - count_text(pad) <= 0:
                if compactions >= self.engine.max_pad_compactions:
                    self.emit(ev("notice", level="info",
                                 text=f"thinking filled the context after {rounds} "
                                      "round(s) — concluding"))
                    break
                if count_text(pad) <= PAD_TAIL_TOKENS * 2:
                    # Everything here would survive as the verbatim tail, so
                    # there is nothing to distil — a checkpoint round would only
                    # spend budget to make the pad bigger.
                    self.emit(ev("notice", level="info",
                                 text=f"thinking filled the context after {rounds} "
                                      "round(s), too little to compact — concluding"))
                    break
                compacted = await self._compact_pad(call_round, pad, answer_cap)
                compactions += 1
                # On failure keep the pad we already have: concluding from a
                # complete pad is the fallback, losing it is never.
                if not compacted:
                    break
                pad = compacted
                continue   # re-test the walls against the smaller pad
            rounds += 1
            self.emit(ev("notice", level="info",
                         text=f"reasoning cut off — resuming it (round {rounds}, "
                              f"pad ~{count_text(pad)} tok)"))
            result = await call_round(overrides={"max_tokens": lap_cap}, pad=pad)
            pad += result.reasoning or ""
            if result.wants_tool and not truncated_tool_calls(result):
                # The thinking decided the next step is an action. Same answer
                # as the closing round's below: hand it back, and the tool loop
                # runs it and carries the turn on. On the raw transport this is
                # a call the engine recovered from the template's own markup —
                # before, it could only be dropped, and the user was shown the
                # XML instead.
                self.emit(ev("notice", level="info",
                             text="the thinking called a tool — running it and "
                                  "continuing the turn"))
                return result
            if result.text:
                # The model closed the block itself and answered. Take that only
                # if it actually finished: a resumed round runs under the small
                # per-round cap, so an answer that hit the cap is half a
                # sentence, and one that was pure tool markup is nothing once
                # stripped. Either way the pad is complete — fall through and let
                # the closing round write the answer under the answer budget.
                text = strip_tool_markup(result.text)
                if text and result.finish_reason != "length":
                    return ChatResult(text)
                break
            if result.finish_reason != "length":
                break  # stopped without text and without being cut off
            if not result.reasoning:
                # The no-progress guard, and the only thing standing in for the
                # round counter that used to bound this loop. Cut off at the cap
                # having written nothing: the pad cannot grow, so the context
                # wall will never move and every further round would be
                # identical. Everything else here shrinks headroom or stops.
                self.emit(ev("notice", level="info",
                             text=f"reasoning stopped making progress after {rounds} "
                                  "round(s) — concluding"))
                break

        # Answer phase: we write </think> ourselves, so this cap buys answer only.
        #
        # Tools are offered here, and only here. A resume must not call one: there
        # is no way to splice a tool result into the middle of an unclosed
        # thinking block, so an interrupted resume would forfeit the pad — the very
        # work this exists to keep. By the closing round the pad has already done
        # its job of deciding what to do, so spending it to launch a call is a
        # fair trade, and the turn continues with the result in hand.
        #
        # The raw transport is the exception: /completion has no tool-call
        # parser, so a call there would reach the user as <tool_call> markup.
        # There the directive from base.md rides along instead (without joining
        # the conversation) to ask for words rather than an action.
        tools = self.schemas if (self.schemas and tools_ok) else None
        self.emit(ev("notice", level="info",
                     text=f"closing thinking (~{count_text(pad)} tok) and writing the "
                          f"answer under {answer_cap or 'the model default'} tok"
                          f"{'' if tools else ' (no tools)'}"))
        result = await call_round(
            overrides={"max_tokens": answer_cap}, pad=pad, closing=True,
            tools=tools,
            extra_messages=None if tools else
            [{"role": "user", "content": self.manager.conclude_directive}])
        if result.wants_tool and not truncated_tool_calls(result):
            self.emit(ev("notice", level="info",
                         text="the conclusion is an action — running it and "
                              "continuing the turn"))
            return result
        if result.wants_tool:
            # The answer budget went on a call that never finished generating.
            # Spending one more round on words beats ending the turn on nothing.
            self.emit(ev("notice", level="warn",
                         text="the closing round was cut off mid tool call — "
                              "asking for words instead"))
            result = await call_round(
                overrides={"max_tokens": answer_cap}, pad=pad, closing=True,
                extra_messages=[{"role": "user",
                                 "content": self.manager.conclude_directive}])
        text = strip_tool_markup(result.text)
        if not text:
            self.emit(ev("notice", level="warn",
                         text="the answer phase came back empty — raising "
                              "max_output_tokens may help"))
        return ChatResult(text)

    @staticmethod
    def _pad_tail(pad, tokens=None):
        """The last `tokens` worth of pad, word-for-word.

        A checkpoint may replace the distant past, but never the tail: the model
        has to resume *mid-sentence*, and it cannot do that from a summary of
        where it was. The end of this string is the exact point it left off.

        The default is read here rather than bound as an argument default, so
        the module constant stays the single source of truth for both this and
        the "too little to compact" test in _think_pad.
        """
        tokens = PAD_TAIL_TOKENS if tokens is None else tokens
        if count_text(pad) <= tokens:
            return pad
        tail = pad[-tokens * 4:]          # ~4 chars/token, then trimmed to fit
        while tail and count_text(tail) > tokens:
            tail = tail[len(tail) // 8 or 1:]
        return tail

    async def _compact_pad(self, call_round, pad, cap):
        """Distil a pad that has filled the context, so thinking can continue.

        The same move `roll_over` makes one level up: when the room runs out,
        keep the meaning and drop the volume. The model writes down what it has
        established — its own words, deliberately chosen, rather than an outside
        summariser guessing which intermediate values mattered — and that note
        plus the verbatim tail becomes the new pad.

        Lossy by construction, which is why it is off unless asked for: a
        derivation that carries exact figures through many steps is usually
        better concluded from a complete pad than continued from a summarised
        one. Returns "" if the checkpoint fails, meaning: conclude instead.
        """
        before = count_text(pad)
        self.emit(ev("notice", level="info",
                     text=f"thinking filled the context (~{before} tok) — "
                          "checkpointing it and carrying on"))
        result = await call_round(
            overrides={"max_tokens": cap}, pad=pad, closing=True,
            extra_messages=[{"role": "user",
                             "content": self.manager.checkpoint_directive}])
        note = strip_tool_markup(result.text)
        if not note:
            self.emit(ev("notice", level="warn",
                         text="the checkpoint came back empty — concluding instead"))
            return ""
        new_pad = f"{note}\n\n{self._pad_tail(pad)}"
        after = count_text(new_pad)
        if after >= before:
            # No room bought, so another round would trip the same wall forever.
            self.emit(ev("notice", level="warn",
                         text=f"the checkpoint ({after} tok) did not shrink the pad "
                              f"({before} tok) — concluding instead"))
            return ""
        self.emit(ev("notice", level="info",
                     text=f"checkpoint: pad {before} → {after} tok "
                          f"(kept the last ~{PAD_TAIL_TOKENS} tok verbatim)"))
        return new_pad

    async def _forced_conclusion(self, call_round, cap):
        """Fallback when the server cannot resume a cut-off round.

        No prefill means the reasoning cannot be handed back, so the only lever
        left is to say so: the directive from base.md goes in as a user message
        and the round re-runs. The work already done is lost — this is the path
        the thinking pad exists to avoid.
        """
        # Never *lower* the cap: a max_tokens above max_output_tokens would make
        # the retry smaller than the round that just failed.
        configured = self.engine.extra_params.get("max_tokens") or 0
        cap = max(cap or 0, configured) or None
        directive = self.manager.conclude_directive
        for _ in range(FORCED_CONCLUSION_TRIES):
            self.emit(ev("notice", level="warn",
                         text=f"reasoning was cut off and this server cannot resume it "
                              f"— forcing a conclusion under {cap or 'the model default'} tok"))
            self.history.append({"role": "user", "content": directive,
                                  "at": now_stamp()})
            try:
                result = await call_round(overrides={"max_tokens": cap} if cap else None)
            except BaseException:
                self.history.pop()  # a directive that never got a round is noise
                raise
            if result.text:
                return ChatResult(strip_tool_markup(result.text))
            self.history.pop()  # a directive with no answer is noise next turn
            if result.finish_reason != "length":
                break
        self.emit(ev("notice", level="warn",
                     text="forced conclusions came back empty — raising "
                          "max_output_tokens may help"))
        return ChatResult("")

    async def _after_turn(self, measured_used):
        trip = int(self.budget * self.rollover_pct / 100) if self.budget and self.rollover_pct else 0
        rolled = False
        if trip:
            used = measured_used if measured_used is not None else self.estimate()
            if used >= trip:
                rolled = await self._maybe_roll(used, trip)
        if rolled:
            self.declined_at, self.warned_over = None, False
            if trip and self.estimate() >= trip:
                self.emit(ev("notice", level="warn",
                             text=f"the fresh session already exceeds {self.rollover_pct}% "
                                  f"of {self.budget} tok — automatic rollover disabled; "
                                  "/new still works"))
                self.rollover_pct = 0
        elif self.max_history > 0 and len(self.history) > self.max_history:
            system = [m for m in self.history if m["role"] == "system"]
            rest = [m for m in self.history if m["role"] != "system"][-self.max_history:]
            while rest and rest[0]["role"] == "tool":
                rest = rest[1:]  # never open on an orphaned tool result
            self.history = system + rest
            # The window just got smaller, so the measured size no longer
            # describes it. used() documents _last_used as "the size of the
            # *current* window, or None" and every other path that reshapes the
            # history honours that; leaving it set here would have the next
            # headroom check read a trimmed window as the full one it used to
            # be, and roll over — archiving and dropping a window to relieve
            # pressure that the trim had already relieved.
            self._last_used = None

    async def _maybe_roll(self, used, trip):
        pct = used * 100 // self.budget if self.budget else 0
        # A rollover that could not archive its window left this set. Trying
        # again unprompted would raise the same error on every turn from here
        # on; the person has been told once and /rollover is the way back.
        if self.rollover_blocked:
            return False
        if self.rollover_mode == "auto":
            return await self.roll_over(f"context at {pct}%", used, trip)
        if self.rollover_mode == "ask":
            # Don't re-ask every turn once refused; wait for real growth.
            if self.declined_at is not None and used < self.declined_at + max(1, self.budget // 20):
                return False
            if await self._ask_rollover(used, pct):
                return await self.roll_over(f"context at {pct}%", used, trip)
            self.declined_at = used
            self.emit(ev("notice", level="info",
                         text="keeping the session; /new rolls over when you're ready"))
            return False
        if not self.warned_over:  # off
            self.emit(ev("notice", level="warn",
                         text=f"past {self.rollover_pct}% of the window — /new starts a fresh session"))
            self.warned_over = True
        return False

    async def _ask_rollover(self, used, pct):
        """Ask the attached clients, but never wait on them forever."""
        request_id = f"ask-{self.turn_id}-{self.last_seq}"
        fut = asyncio.get_running_loop().create_future()
        self.pending_ask = (request_id, fut)
        self.emit(ev("rollover_ask", request_id=request_id, used=used,
                     budget=self.budget, percent=pct, timeout_s=self.prompt_timeout))
        try:
            return bool(await asyncio.wait_for(fut, timeout=self.prompt_timeout))
        except asyncio.TimeoutError:
            # Declining is the safe default: the decline path already exists and
            # the window is merely full, not overflowed.
            self.emit(ev("notice", level="warn", text="no answer — keeping the session"))
            return False
        finally:
            self.pending_ask = None

    def answer_rollover(self, request_id, accept):
        """Idempotent: a second client answering a resolved ask is ignored."""
        if not self.pending_ask:
            return False
        rid, fut = self.pending_ask
        if rid != request_id or fut.done():
            return False
        fut.set_result(bool(accept))
        return True

    async def roll_over(self, reason, used=None, trip=None):
        """Archive, distil a handoff, and start a fresh window.

        Archive first: a failed summariser or a full disk costs the summary,
        never the conversation. Returns True if the window was actually rolled.

        That promise used to be only a comment. A failed save_transcript warned
        and fell through, and ~40 lines later `self.history = fresh` threw the
        conversation away anyway — so the one failure the ordering exists to
        survive was the one that lost everything. Now it aborts, and the window
        it could not archive is still there to try again.

        The mirror of that: once the archive *has* been written, the window is
        replaced even if this is cancelled part-way through, so the same
        messages are never archived twice.
        """
        self.rollover_count += 1
        used = used if used is not None else self.used_now()
        self.emit(ev("rollover_start", reason=reason, used=used,
                     budget=self.budget, count=self.rollover_count))
        try:
            transcript = await store.save_transcript(
                self.history, self.model_name, self.config, self.name,
                session_id=self.session_id)
        except OSError as e:
            # error, not warn: nothing was rolled, the window is still full, and
            # the next turn will be refused the same way until the disk is fixed.
            self.rollover_count -= 1
            self.rollover_blocked = str(e)
            self.emit(ev("notice", level="error",
                         text=f"could not save the transcript ({e}) — rollover "
                              "abandoned and the conversation kept. Free some "
                              "disk, then /rollover."))
            self.emit(ev("rollover_failed", reason=reason, error=str(e),
                         used=used, budget=self.budget))
            return False
        # Cleared only on a save that worked, so the guard cannot outlive the
        # condition that set it.
        self.rollover_blocked = None
        # Closed here, not at the end: the path has to exist before the
        # carryover that advertises it is built, and the next segment then
        # opens on the first turn of the fresh window.
        record = await self.close_journal()

        # Past here the transcript is on disk and the journal segment is
        # closed, so the old window has ended whether or not the rest of this
        # runs. What is left — summarise, append, re-read the behavior file —
        # is all optional polish on the new window, and all of it awaits. A
        # cancellation in the middle of it (a stopped turn, a shutdown) used to
        # leave self.history still holding the window that was just archived:
        # the next turn tripped the same headroom check and archived the same
        # messages again, under a second transcript. So the swap below runs on
        # the way out of this block either way, and a cancellation costs the
        # summary rather than duplicating the window.
        handoff = ""
        handoff_path = None
        try:
            # Three caps: the absolute one, a share of the window so a small
            # model does not spend an eighth of its context on the summary for
            # ever, and what is actually free right now.
            summary_max = min(HANDOFF_MAX_TOKENS,
                              self.budget // HANDOFF_WINDOW_SHARE,
                              self.budget - used - HANDOFF_MARGIN)
            if summary_max >= HANDOFF_MIN_TOKENS:
                try:
                    handoff = await store.build_handoff(self.engine, self.history,
                                                          max_tokens=summary_max,
                                                          prior=self.carryover())
                except Exception as e:
                    self.emit(ev("notice", level="warn",
                                 text=f"handoff failed ({e}) — carrying the last exchange only"))
            else:
                self.emit(ev("notice", level="warn",
                             text="no headroom to summarise — carrying the last exchange only"))

            if handoff:
                try:
                    handoff_path = await store.append_handoff(
                        handoff, self.model_name, transcript, self.config, self.name,
                        journal_path=record)
                except OSError as e:
                    self.emit(ev("notice", level="warn", text=f"could not append handoff: {e}"))

            # The prompt prefix is about to be replaced wholesale, so this is
            # the cheap moment to pick up an edited behavior file: the KV cache
            # the re-read would cost is being thrown away regardless.
            await self.refresh_behavior()
        finally:
            fresh = self.fresh_history(
                store.format_carryover(handoff, transcript, journal_path=record))
            tail = store.last_exchange(self.history)
            if tail and (not trip or self.estimate(fresh + tail) < trip):
                fresh += tail
            elif tail:
                self.emit(ev("notice", level="warn",
                             text="dropped the carried exchange — it would not fit"))
            self.history = fresh
            # Measured against the window that just ended. Keeping it would
            # have used_now() report a full window to the next turn's headroom
            # check, which would roll over again at once — and again after that.
            self._last_used = None
            # handoff is the file's path; handoff_text is the summary itself,
            # so a client can show the user what the compaction kept without
            # opening it. Emitted from the finally with the rest: a client that
            # saw rollover_start has to see the window close even when the turn
            # carrying it was stopped.
            self.emit(ev("rollover_done",
                         transcript=str(transcript) if transcript else None,
                         record=str(record) if record else None,
                         handoff=str(handoff_path) if handoff_path else None,
                         handoff_text=handoff or None,
                         used=self.estimate(), budget=self.budget,
                         count=self.rollover_count))
        return True

    # ---- commands --------------------------------------------------------

    async def command(self, name, args=""):
        """Run a slash command. Returns a reply dict for the caller only.

        Read-only commands are replies, not broadcasts: /info is one client's
        view of the session and has no business appearing in everyone's stream.
        State-changing ones take the turn lock, because a /clear landing between
        a tool-call turn and its results would corrupt the history for good.
        """
        # Case-insensitive: /rollOver reads better than /rollover and a client
        # should not have to care which one the user typed.
        name = name.lstrip("/").lower()
        if name == "info":
            # The same figure the headroom check works from, so what a person
            # reads here is what the session is actually acting on. Marked
            # honestly: `estimated` is only true when the server withheld usage
            # and the +60% pad is all we have.
            used = self.used_now()
            return {"ok": True, "info": {
                "session": self.name, "session_id": self.session_id,
                "model": self.model_name,
                "base_url": (str(self.engine.client.base_url)
                             if self.engine else None),
                "messages": len(self.history),
                "recent": [{"role": m["role"],
                            "content": (m.get("content") or "")[:120],
                            "tokens": count_text(m.get("content") or "")}
                           for m in self.history[-5:]],
                "used": used, "budget": self.budget,
                "estimated": self._last_used is None,
                "tools_tokens": self.tools_tok,
                # Since the session was created, surviving /clear and restarts.
                "output_total": self.total_output,
                "rollover": {"mode": self.rollover_mode, "percent": self.rollover_pct,
                             "trip": int(self.budget * self.rollover_pct / 100)
                                     if self.rollover_pct else 0,
                             "count": self.rollover_count},
            }}
        if name == "behavior":
            return {"ok": True, "model": self.model_name,
                    "session": self.name,
                    "base": self.base_behavior or "",
                    "behavior": self.behavior or "",
                    "session_behavior": self.session_behavior or ""}
        if name == "models":
            return {"ok": True, "current": self.model_name,
                    "configured": configlib.known_models(self.config)}
        if name == "reload":
            # Manager-level, and deliberately outside the turn lock: reload defers
            # any session that is busy, so taking the lock here would have it defer
            # the very session that asked, or deadlock waiting on itself.
            return await self.manager.reload()

        # self.busy, not lock.locked(): a turn created by start_turn does not
        # reach `async with self.lock` until the loop next runs it, so a /clear
        # arriving in that gap saw an idle session and wiped the history out
        # from under a turn that was about to start. rename and delete already
        # use the property for this reason.
        if self.busy:
            return {"ok": False, "error": "a turn is running in this session"}
        async with self.lock:
            # Each of these rewrites the history, so each has to reach disk:
            # a /clear followed by a crash would otherwise bring back the
            # conversation the user just threw away.
            if name == "clear":
                await self.refresh_behavior()
                self.history = self.fresh_history()
                self.declined_at, self.warned_over = None, False
                self.rollover_pct = self._configured_rollover_pct()
                self._last_used = None
                # /clear starts a new window as surely as a rollover does, it
                # just does not summarise on the way. Leaving the segment open
                # would append the next, unrelated conversation to the end of
                # this one, with the turn numbers running straight on.
                record = await self.close_journal()
                # used/budget for the same reason rollover_done carries them: a
                # rebuilt window is not an empty one — it holds the three
                # behavior layers and the tool schemas. A client that assumed
                # zero here drew a 0% gauge over a prompt with the MD in it,
                # which looks exactly like the MD having been dropped.
                self.emit(ev("session_state", what="cleared", messages=0,
                             used=self.used_now(), budget=self.budget,
                             record=str(record) if record else None))
                await self.persist()
                return {"ok": True}
            if name == "rollover":
                trip = (int(self.budget * self.rollover_pct / 100)
                        if self.rollover_pct else 0)
                # Asked for by a person, so the guard an earlier disk failure
                # set is cleared first: they have usually just freed the space
                # and this is how they say so. If it fails again it is set again.
                self.rollover_blocked = None
                if not await self.roll_over("on request", trip=trip):
                    return {"ok": False,
                            "error": "could not archive this window — the "
                                     "conversation is untouched. See the error "
                                     "above; free some disk and try again."}
                self.declined_at, self.warned_over = None, False
                await self.persist()
                return {"ok": True}
            if name == "new":
                # The hard one: nothing about this session survives it. /clear
                # forgets the conversation but leaves the archives; this takes
                # them too, so there is nothing left to grep and nothing to
                # carry forward. Irreversible, deliberately — a rollover is
                # what you want when the past is worth keeping.
                await self.refresh_behavior()
                self.history = self.fresh_history()
                self.declined_at, self.warned_over = None, False
                self.rollover_count = 0
                self.rollover_pct = self._configured_rollover_pct()
                self._last_used = None
                await self.close_journal()
                files, freed = await store.purge_archives(self.session_id,
                                                          self.config)
                self.emit(ev("session_state", what="wiped", messages=0,
                             used=self.used_now(), budget=self.budget,
                             files=files, bytes=freed))
                await self.persist()
                logger.info("session %s wiped: %d archive file(s), %d bytes",
                            self.name, files, freed)
                return {"ok": True, "files": files, "bytes": freed}
            if name == "model":
                target = args.strip()
                if not target:
                    return {"ok": True, "current": self.model_name,
                            "configured": configlib.known_models(self.config)}
                try:
                    await self.use_model(target)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                self.emit(ev("session_state", what="model", model=self.model_name,
                             budget=self.budget))
                await self.persist()
                return {"ok": True, "model": self.model_name, "budget": self.budget}
        return {"ok": False, "error": f"unknown command: /{name}"}

    async def use_model(self, model_name=None):
        """Point this session at a model, keeping the conversation.

        The engine comes from the manager's cache and is never closed here —
        another session may be mid-stream on it.
        """
        engine, name, entry = await self.manager.engine_for(model_name)
        # Logged before the assignment, so the line carries both ends of the
        # move. Without it the only record of which model a session is on is the
        # restore line at startup, which makes "why is this session on that
        # model?" unanswerable for the whole of a long-running core.
        if self.model_name and name != self.model_name:
            logger.info("session %s switched model: %s -> %s",
                        self.name, self.model_name, name)
        self.engine = engine
        self.model_name = name
        # The base is shared by every session, the model file is per model,
        # and the session file is named for this session.
        (self.base_behavior, self.behavior,
         self.session_behavior) = await self.manager.behavior_for(entry, self.name)
        # carryover() must be read before history[0] is replaced, and passed back
        # in: switching models after a rollover used to drop the handoff on the floor.
        carried = self.carryover()
        if self.history:
            body = [m for m in self.history if m["role"] != "system"]
            self.history = self.fresh_history(carried) + body
        else:
            self.history = self.fresh_history(carried)


class SessionManager:
    """Sessions by name, and one engine per model shared between them."""

    def __init__(self, config, config_path=None):
        self.config = config
        # Where config came from, so /reload re-reads the file actually in use
        # rather than the default when --config pointed somewhere else.
        self.config_path = Path(config_path or configlib.CONFIG_PATH)
        self.sessions = {}
        self._engines = {}
        self.behaviors = behavior.BehaviorStore(self.config)
        # ssh port-forwards the core owns (models with an ssh_forward entry).
        # configure() is synchronous and spawns nothing; the keepalive loop
        # starts from the server's on_startup, where a running loop exists.
        self.forwards = SSHForwardManager()
        self.forwards.configure(self.config)
        # Session ids in use, from logs/session_ID.log. None until first needed:
        # __init__ is synchronous and this is a file read.
        self._ids = None
        # Read synchronously at startup — one small local file, the same way
        # the config file itself is read. A missing file warns and degrades to
        # an empty base; the core keeps starting.
        self.base_behavior = self.behaviors.read(
            config.get("behavior", {}).get("base_file") or behavior.DEFAULT_BASE_FILE)
        self.conclude_directive = behavior.extract_conclude_directive(self.base_behavior)
        self.checkpoint_directive = behavior.extract_checkpoint_directive(self.base_behavior)
        self._engine_lock = asyncio.Lock()
        # Serialises session creation in get(). Creation spans three awaits
        # between the "is it there?" test and the insert, which is long enough
        # for a second attach to the same new name to slip through and build a
        # rival Session — see get() for what that cost.
        self._create_lock = asyncio.Lock()
        # Serialises reload() against itself — see its docstring for what two
        # overlapping runs do to the engine set and the forward table.
        self._reload_lock = asyncio.Lock()
        # Engines a session stopped using after reload()'s own cleanup had
        # already run — see retire_engine. Drained at the next reload and at
        # close(); an entry here is a connection pool nobody is reading from.
        self._retired_engines = []
        tools_cfg = config.get("tools", {})
        workdir = tools_cfg.get("workdir") or str(paths.REPO_ROOT)
        from tools import build_tools
        # Built once: Tool objects are stateless, and the schemas are identical
        # for every session, so count_tools is paid once rather than per session.
        self._tools = build_tools(tools_cfg, workdir=workdir)
        self._schemas = [t.spec for t in self._tools]
        self._tools_tok = count_tools(self._schemas)

    async def engine_for(self, model_name=None):
        name = model_name or self.config.get("models", {}).get("default_model")
        # Under a lock: two sessions racing on the same uncached model would
        # otherwise each build an AsyncOpenAI and orphan one of them.
        async with self._engine_lock:
            if name not in self._engines:
                # ensure() raises RuntimeError when the ssh path does not come
                # up. Convert it to ValueError at this seam: every caller of
                # engine_for (ws attach, /model, restore) already turns a
                # ValueError into a clean error event, and a dead helios path
                # must read as "model unusable right now", not a 500. The
                # keepalive keeps retrying in the background; a retry of the
                # same command picks the model up when the path is back.
                try:
                    fwd = await self.forwards.forward_for(name)
                except RuntimeError as e:
                    raise ValueError(
                        f"model '{name}' is unreachable — {e} — the ssh path "
                        "is down (jump host or far side); the keepalive is "
                        "retrying, try again in a moment") from e
                self._engines[name] = configlib.build_engine(self.config, name, forward=fwd)
            return self._engines[name]

    def retire_engine(self, engine):
        """Note an engine a session has stopped using, to be closed later.

        Synchronous on purpose: the only caller is Session.apply_config, which
        must not await (see its docstring for why). The close itself happens at
        the next reload or at shutdown, and only once nothing references the
        engine any more — engines are shared per model, so the session that
        dropped it is often not the last holder.
        """
        if engine is not None:
            self._retired_engines.append(engine)

    def _live_engine_ids(self):
        """id() of every engine something could still read from."""
        live = {id(e) for e, _, _ in self._engines.values()}
        live |= {id(s.engine) for s in self.sessions.values()}
        # A deferred blob's engine is not installed yet but is already spoken
        # for: the session will swap to it the moment its turn ends.
        live |= {id(b["engine"]) for s in self.sessions.values()
                 if (b := s._pending_config) and b["engine"] is not None}
        return live

    async def _close_unreferenced(self, engines):
        live = self._live_engine_ids()
        for engine in engines:
            if engine is None or id(engine) in live:
                continue
            try:
                await engine.close()
            except Exception:
                logger.debug("engine close", exc_info=True)

    async def _drain_retired_engines(self):
        if not self._retired_engines:
            return
        retired, self._retired_engines = self._retired_engines, []
        await self._close_unreferenced(retired)

    def _engine_ports(self):
        """The netlocs (host:port) every live engine points at.

        The ssh keepalive uses this to know which removed forwards may go: a
        forward is retired only when no engine — cached or session-held —
        references its local port any more.
        """
        from urllib.parse import urlsplit
        ports = {urlsplit(e.base_url).netloc for e, _, _ in self._engines.values()}
        for sess in self.sessions.values():
            if sess.engine:
                ports.add(urlsplit(sess.engine.base_url).netloc)
        return ports

    async def _new_session_id(self):
        """A session id that is not already in logs/session_ID.log.

        Random alone would already collide only about once in 4.3 billion; the
        registry check is what turns "vanishingly unlikely" into "cannot happen",
        which is worth having because a collision is silent — two conversations
        would simply share a prefix in the archive and nothing would complain.
        """
        if self._ids is None:
            self._ids = await store.known_ids(self.config)
        while True:
            sid = secrets.token_hex(4)
            if sid not in self._ids:
                self._ids.add(sid)
                return sid

    async def _adopt_session_id(self, session, payload=None):
        """Give a restored session its id, minting one if the file predates them.

        Live files written before session_id existed have none — this is the
        migration, and it runs once per such session at the next restart.
        """
        if self._ids is None:
            self._ids = await store.known_ids(self.config)
        saved = (payload or {}).get("session_id")
        if saved:
            session.session_id = saved
            self._ids.add(saved)
        else:
            session.session_id = await self._new_session_id()
            logger.info("session %s had no id — assigned %s",
                        session.name, session.session_id)
        await store.register_id(session.session_id, session.name, self.config)

    # The behavior cache moved to behavior.BehaviorStore, which owns the dict
    # behind a lock. These stay as the names the rest of the core already calls.

    def session_behavior_path(self, session_name):
        return self.behaviors.session_path(session_name)

    def behavior_keys_for(self, entry, session_name=None):
        return self.behaviors.keys_for(entry, session_name)

    async def behavior_for(self, entry, session_name=None):
        """(base, model, session) behavior, read off the event loop."""
        return await asyncio.to_thread(self.behaviors.layers_sync, entry, session_name)

    async def refresh_behaviors(self):
        """Reconcile the behavior cache with disk, off the event loop.

        Touches the cache and nothing else — no live window is rebuilt here, so
        this is safe to call from /reload as well as from the rebuild paths.
        """
        return await asyncio.to_thread(self.behaviors.refresh_sync)

    async def get(self, name, model=None):
        """The session called `name`, created on first use.

        Creation is serialised and the existence test repeated inside the lock.
        Without that, two clients attaching to the same new name at once both
        saw it missing, and three awaits later both inserted: two Session
        objects for one name, each with its own _save_lock, writing the same
        live file over each other — and the loser still attached to whichever
        client got there first, so its events went to an object the registry
        no longer listed. The WebUI reconnecting while the bot bridges the same
        channel is exactly this shape.
        """
        # The common case — the session already exists — costs no lock at all.
        s = self.sessions.get(name)
        if s is not None:
            return s
        async with self._create_lock:
            # Checked again: whoever held the lock first may have just made it.
            s = self.sessions.get(name)
            if s is not None:
                return s
            s = Session(name, self, self.config)
            s.session_id = await self._new_session_id()
            s.tools = self._tools
            s.schemas = self._schemas
            s.tools_tok = self._tools_tok
            await s.use_model(model)
            # Registered in the dict last, and only once it is fully built: a
            # half-initialised session must never be visible under its name.
            self.sessions[name] = s
            await store.register_id(s.session_id, name, self.config)
            logger.info("session %s created on model %s (id %s)",
                        name, s.model_name, s.session_id)
            return s

    async def reload(self, path=None):
        """Re-read config.yaml and apply it to everything already running.

        Nothing is committed until the new file has parsed *and* produced a
        working engine for every model in it, so a half-finished edit — the
        normal state of a file open in an editor — cannot take a running core
        down. On failure this returns the error and changes nothing at all.

        Restart-only settings are core.bind and core.port (the socket is already
        bound; rebinding would drop every client, which is the thing being
        avoided) and logging.file. Everything else is live.

        Serialised against itself. There are three ways in — POST /reload, the
        /reload command, and the end of save_webui — and roughly a dozen awaits
        between reading the file and committing the result. Two overlapping
        runs interleave at every one of them: both build an engine set, both
        swap self._engines, and the one that finishes second closes, as
        "unreferenced", the engines the first just handed to live sessions.
        Likewise forwards.plan/stage/commit, which is a three-step read-modify-
        write over shared state. Taking the lock here instead of inside keeps
        the all-or-nothing failure path below intact.
        """
        async with self._reload_lock:
            return await self._reload(path)

    async def _reload(self, path=None):
        path = path or self.config_path
        # Dictionaries, matching plan()'s return: the except block must be able
        # to iterate them even when plan() never ran (the file failed to parse).
        added, removed = {}, {}
        try:
            fresh = await asyncio.to_thread(configlib.load_config, path)
            # Plan the forward diff and make the additions visible BEFORE the
            # engine validation, so a new ssh_forward model's engine is built
            # pointing at its (already port-assigned) forward. The validator
            # does not spawn the ssh — that is lazy (first use / keepalive) —
            # so a refused reload unstages the additions and the forward set is
            # never half-changed.
            added, removed = self.forwards.plan(fresh)
            self.forwards.stage(added)
            engines = configlib.build_all_engines(fresh, self.forwards)

            old_cfg = copy.deepcopy(self.config)
            changed = configlib.diff_config(old_cfg, fresh)
            # The behavior files are not in the config, so diff_config cannot
            # see an edited base.md or sessions/<name>.md. Refreshing the cache
            # here is what makes the edit available; it is NOT applied to any
            # live window.
            #
            # history[0] is the prompt prefix every server has already
            # tokenised. Rewriting it mid-conversation discards the KV cache for
            # every live session at once, to deliver a change the session did
            # not ask for at a moment it did not choose. So a reload moves
            # settings — engine, tools, scalars, directives — and leaves the
            # window alone. The edited file lands at the next rollover, /clear
            # or /new, each of which rebuilds that prefix anyway and re-reads on
            # the way through.
            refreshed = await self.refresh_behaviors()

            # Everything below reads `fresh` and can refuse it, so all of it
            # sits above the commit. Building the tools and reading the base
            # file used to happen *after* self.config had been replaced, which
            # meant a config this code choked on was already the running one:
            # the exception escaped reload(), POST /reload answered 500, and
            # the manager was left holding settings it had just rejected, with
            # /reload unable to undo it because /reload is what set it.
            base_behavior = await asyncio.to_thread(
                self.behaviors.read,
                (fresh.get("behavior") or {}).get("base_file")
                or behavior.DEFAULT_BASE_FILE)
            tools_cfg = fresh.get("tools") or {}
            workdir = tools_cfg.get("workdir") or str(paths.REPO_ROOT)
            from tools import build_tools
            new_tools_list = build_tools(tools_cfg, workdir=workdir)
            new_schemas = [t.spec for t in new_tools_list]
            new_tools_tok = count_tools(new_schemas)
        # TypeError/AttributeError/KeyError alongside the rest: a half-finished
        # edit does not only mangle values, it mangles shapes. `tools:` left
        # with nothing under it is None, `conversation:` typed as a list is a
        # list, and either one meets a .get() somewhere above. That is a config
        # to refuse with a message, not a traceback out of a reload.
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                yaml.YAMLError, RuntimeError) as e:
            # The validator did not start the staged forwards, but the keepalive
            # may have brought one up in the meantime — close them so a refused
            # reload leaves nothing running.
            for fwd in added.values():
                await fwd.close()
            # `removed` too: on a replacement the name in `added` is covering the
            # running forward in `removed`, and unstage() has to put that one back
            # rather than delete the entry. See SSHForwardManager.unstage.
            self.forwards.unstage(added, removed)
            logger.warning("config reload refused: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        if not changed and not refreshed:
            # Nothing to install, so the engines just built are unreferenced the
            # moment this returns. Closing them is not housekeeping: each one
            # owns an httpx pool, and a page that polls /reload would otherwise
            # leak a pool per poll.
            await self._close_unreferenced(e for e, _, _ in engines.values())
            return {"ok": True, "changed": [], "updated": 0, "deferred": 0,
                    "note": "config and behavior files on disk are identical "
                            "to the running ones"}
        changed += [f"{key}: edited — applies at the next rollover, /clear or /new"
                    for key in refreshed]

        # Under the engine lock, because engine_for reads both of these and
        # holds that lock across an await of its own: a session asking for an
        # uncached model at this moment would otherwise resume after the swap
        # and write its engine into the *new* dict, displacing the one this
        # reload built and handed to the session blobs below. The displaced
        # engine is in neither old_engines nor the live set, so nothing ever
        # closes it — a leaked connection pool, and a session running on an
        # engine the reload did not give it.
        async with self._engine_lock:
            # In place, never rebound: the manager, the aiohttp app and every
            # session hold this same dict, so mutating it is what makes
            # core.token, the state directories and GET /models pick the new
            # values up with no plumbing.
            self.config.clear()
            self.config.update(fresh)
            old_engines, self._engines = self._engines, engines
        # Read above the commit, installed here. The manager's own copy is safe
        # to move immediately: the directives are re-sent as user messages
        # mid-turn, not held in the prompt prefix, so updating them costs no
        # KV cache.
        self.base_behavior = base_behavior
        self.conclude_directive = behavior.extract_conclude_directive(self.base_behavior)
        self.checkpoint_directive = behavior.extract_checkpoint_directive(
            self.base_behavior)
        self._tools = new_tools_list
        self._schemas = new_schemas
        self._tools_tok = new_tools_tok

        log_cfg = fresh.get("logging") or {}
        if log_cfg.get("level"):
            logging.getLogger().setLevel(
                getattr(logging, str(log_cfg["level"]).upper(), logging.INFO))

        # `or {}`, not a .get default: a section present but empty in the YAML
        # is None, which a bare default never replaces.
        old_conv = old_cfg.get("conversation") or {}
        new_conv = fresh.get("conversation") or {}
        old_tools, new_tools = old_cfg.get("tools") or {}, tools_cfg
        # (attribute, what the old config said, what the new one says). apply_config
        # reassigns only where the session still holds the old value.
        scalars = [
            # int_or, not int: reload() has already committed the new config by
            # the time this list is built, so a ValueError raised here would
            # leave the core running the new settings with half the sessions
            # never told about them — the one failure mode the all-or-nothing
            # validation above exists to prevent.
            ("max_tool_rounds", configlib.int_or(old_tools.get("max_iterations", 8), 8),
                                configlib.int_or(new_tools.get("max_iterations", 8), 8)),
            ("max_history", configlib.int_or(old_conv.get("max_history_messages", 100), 100),
                            configlib.int_or(new_conv.get("max_history_messages", 100), 100)),
            ("rollover_pct",
             configlib.int_or(old_conv.get("rollover_at_percent", DEFAULT_ROLLOVER_PCT) or 0,
                              DEFAULT_ROLLOVER_PCT),
             configlib.int_or(new_conv.get("rollover_at_percent", DEFAULT_ROLLOVER_PCT) or 0,
                              DEFAULT_ROLLOVER_PCT)),
            ("rollover_mode", str(old_conv.get("rollover_mode", DEFAULT_ROLLOVER_MODE)).lower(),
                              str(new_conv.get("rollover_mode", DEFAULT_ROLLOVER_MODE)).lower()),
            ("prompt_timeout", configlib.int_or(old_conv.get("prompt_timeout", 60), 60),
                               configlib.int_or(new_conv.get("prompt_timeout", 60), 60)),
            ("rollover_before_think", bool(old_conv.get("rollover_before_think", True)),
                                      bool(new_conv.get("rollover_before_think", True))),
            # Turning this off leaves the open segment where it is: it closes at
            # the next rollover, holding what was recorded before the reload.
            ("journal_on", bool(old_conv.get("journal", True)),
                           bool(new_conv.get("journal", True))),
        ]

        updated = deferred = 0
        # A list, not the live view: a client attaching mid-iteration would
        # otherwise create a session that is handed a blob built before it existed.
        for sess in list(self.sessions.values()):
            entry = engines.get(sess.model_name)
            blob = {
                "tools": self._tools, "schemas": self._schemas,
                "tools_tok": self._tools_tok, "scalars": scalars,
                "changed": changed,
                # A model dropped from the config: the session keeps the engine it
                # has, which still works. Moving a live conversation onto a
                # different model behind the user's back would be worse.
                "engine": entry[0] if entry else None,
                "lost_model": entry is None,
            }
            # busy, not lock.locked(): a turn between create_task and its first
            # await would otherwise be treated as idle and have its engine and
            # budget swapped mid-flight instead of being deferred.
            if sess.busy:
                sess._pending_config = blob
                deferred += 1
            else:
                sess.apply_config(blob)
                updated += 1

        # Now that the engines and session blobs are the new ones, the removals
        # are real. A removed forward whose port a live engine still points at
        # (a session mid-turn on the old engine, or one that lost its model)
        # stays up as a zombie: the keepalive retires it when the last engine
        # on that port is gone. The rest are closed now — nothing points at
        # them any more, and leaving them would leak the ssh child.
        ports = self._engine_ports()
        zombies = {n: f for n, f in removed.items()
                   if f.status()["local"] in ports}
        self.forwards.commit(removed)
        # set_zombies merges, so a zombie still waiting from an earlier reload
        # survives this one; what it hands back is the forwards this reload
        # displaced under a name already pending, which nothing points at any
        # more and which only this caller is in async code to close.
        for fwd in self.forwards.set_zombies(zombies):
            await fwd.close()
        for name, fwd in removed.items():
            if name not in zombies:
                await fwd.close()

        # Only the ones nothing points at any more. A session mid-stream on an old
        # engine must keep it: closing the client under it would abort the read.
        await self._close_unreferenced(e for e, _, _ in old_engines.values())
        # Engines retired by a deferred apply since the last reload. They were
        # spared by the pass above — at the time, the session still pointed at
        # them — and this is the first moment it is safe to look again.
        await self._drain_retired_engines()

        logger.info("config reloaded from %s: %s (%d session(s) updated, %d deferred)",
                    path, "; ".join(changed), updated, deferred)
        return {"ok": True, "changed": changed, "updated": updated, "deferred": deferred}

    async def save_webui(self, webui):
        """Edit the webui: section of config.yaml in place and apply it live.

        A line-level edit, not a YAML round-trip: the file is heavily
        commented, and dumping the whole config would delete every comment
        in it. Existing keys are edited in place — value replaced, inline
        comment kept — so a file the user has annotated keeps its notes.
        Keys the form sends that the file lacks are appended inside the
        block; anything else in the file, including unknown keys, is left
        exactly where it is. The finished file is parsed before it is
        written, through a temp file renamed over the original, so a save
        can never leave config.yaml half-written or unparseable.

        The apply is the ordinary reload(): it re-reads the file, refuses
        to change anything if it will not load, and re-themes every open
        tab over the reloaded event.
        """
        if not isinstance(webui, dict):
            return {"ok": False, "error": "webui must be a mapping"}
        pin = webui.get("pin")
        if pin is not None and not isinstance(pin, dict):
            return {"ok": False, "error": "webui.pin must be a mapping"}
        values = {}
        if "theme" in webui:
            # Checked the same way pin's values are, a few lines down. theme
            # never reaches the generated CSS — only PIN_KEYS do, and those are
            # re-validated at render — so this is not an injection guard; it is
            # that yaml_scalar on a dict or a list writes a line into
            # config.yaml that means something other than what was sent, into
            # the file holding the user's settings and the bot token.
            if isinstance(webui["theme"], (dict, list)):
                return {"ok": False, "error": "webui.theme must be a scalar"}
            values["theme"] = config_edit.yaml_scalar(webui["theme"])
        if isinstance(pin, dict):
            for k, v in pin.items():
                if isinstance(v, (dict, list)):
                    return {"ok": False, "error": f"webui.pin.{k} must be a scalar"}
                if not configlib.valid_pin_value(v):
                    # Refused here rather than sanitised: a value that needs
                    # characters this rejects is not a colour or a length, and
                    # quietly rewriting what the user typed would be worse than
                    # telling them it will not do.
                    return {"ok": False, "error": (
                        f"webui.pin.{k} is not a usable CSS value — letters, "
                        "digits, spaces and  # % . , ( ) + - _  only")}
                values[f"pin.{k}"] = config_edit.yaml_scalar(v)

        try:
            text = self.config_path.read_text()
        except OSError as e:
            return {"ok": False, "error": f"cannot read config: {e}"}
        try:
            finished = config_edit.edit_webui_block(text, values)
        except ValueError as e:
            logger.error("refused a webui save: %s", e)
            return {"ok": False, "error": str(e)}
        try:
            fresh = yaml.safe_load(finished) or {}
        except yaml.YAMLError as e:
            return {"ok": False, "error": f"refusing to write a config that will not parse: {e}"}
        if not isinstance(fresh.get("webui"), dict):
            return {"ok": False, "error": "webui must be a mapping"}
        # Parsing the result cannot see a duplicated block: safe_load keeps the
        # last `webui:` and reports a perfectly good mapping, while everything
        # in the first copy is gone. Counting is the only check that catches it,
        # and it is cheap enough to keep here as well as in the editor.
        if config_edit.count_blocks(finished) != max(config_edit.count_blocks(text), 1):
            return {"ok": False, "error": (
                "refusing to write a config whose webui: block was duplicated — "
                "the later copy would hide your current settings")}

        # The rename was already here; the fsync was not, so the metadata could
        # reach disk ahead of the bytes and a power cut mid-save could leave
        # config.yaml readable but empty — with the only copy of the user's
        # settings in it.
        paths.atomic_write(self.config_path, finished)
        logger.info("config saved from WebUI: webui section edited in place")
        return await self.reload()

    async def restore(self):
        """Reload every session written to state/live/. Returns how many.

        Eager, at startup, rather than lazily on attach: GET /sessions reads this
        dict and nothing else, so a lazy restore would leave the sidebar empty
        until each session was clicked — which is the thing being fixed.

        Cheap enough to do up front: engine_for() builds an AsyncOpenAI client
        without touching the network, and behavior files are read once and cached.
        """
        for payload in reversed(await store.load_live(self.config)):
            name = payload["name"]
            if name in self.sessions:
                continue
            s = Session(name, self, self.config)
            s.tools = self._tools
            s.schemas = self._schemas
            s.tools_tok = self._tools_tok
            try:
                await s.use_model(payload.get("model"))
            except (ValueError, KeyError) as e:
                logger.warning("session %s: model %r unusable (%s) — using the default",
                               name, payload.get("model"), e)
                try:
                    await s.use_model(None)
                except Exception:
                    logger.exception("session %s: no usable model, skipping", name)
                    continue
            # After use_model, never before: it rebuilds history[0] from the
            # behavior file, so assigning first would throw away the
            # === CONTINUED SESSION === block a rollover left in the system
            # message — a model-written handoff that cannot be regenerated.
            s.history = payload["messages"]
            s.load_state(payload)
            await self._adopt_session_id(s, payload)
            self.sessions[name] = s
            logger.info("session %s restored: %d messages on %s (id %s)",
                        name, len(s.history) - 1, s.model_name, s.session_id)
        return len(self.sessions)

    def describe(self):
        return [{"name": s.name, "model": s.model_name,
                 "messages": len([m for m in s.history if m["role"] != "system"]),
                 "clients": len(s.subscribers), "busy": s.busy}
                for s in self.sessions.values()]

    async def rename(self, old, new):
        """Give a session a different name. The conversation is untouched.

        Refused while a turn is running: emit() stamps every event with self.name,
        so a rename underneath a turn would split that turn's events across two
        names, and a client filtering on the name it knows would see half of them.

        Transcripts already written keep the old name in their filename — they are
        records of what the session was called when they were archived. The live
        file is the opposite: it is the session, so it moves with it.
        """
        s = self.sessions.get(old)
        if s is None:
            return {"ok": False, "error": f"no session {old!r}"}
        new = (new or "").strip()
        if not new or len(new) > 64 or "/" in new:
            return {"ok": False,
                    "error": "a name must be 1-64 characters and cannot contain '/'"}
        if new == old:
            return {"ok": True, "name": new, "was": old}
        if new in self.sessions:
            return {"ok": False, "error": f"session {new!r} already exists"}
        if s.busy:
            return {"ok": False, "error": "a turn is running in this session"}
        del self.sessions[old]
        s.name = new
        self.sessions[new] = s
        # Emitted after the rename, so emit() stamps it with the new name — that
        # stamp is what tells an attached client where it now is. Nobody is
        # disconnected: same session object, same socket, same scrollback.
        s.emit(ev("session_state", what="renamed", was=old))
        # The index is the only record that these two names are one conversation:
        # transcripts written before now keep the old name for ever, and the live
        # file is about to stop mentioning it at all. Non-fatal — a rename that
        # worked must not be reported as failed because a log line did not land.
        try:
            await store.append_index(
                {"t": "rename", "id": s.session_id, "from": old, "to": new,
                 "at": datetime.now().isoformat(timespec="seconds")}, self.config)
        except OSError as e:
            logger.warning("could not record the rename in the session index: %s", e)
        # The id log names the conversation too; an entry still showing the old
        # name would misidentify every archive written under the id from now on.
        try:
            await store.register_id(s.session_id, new, self.config)
        except OSError as e:
            logger.warning("could not update the session id log: %s", e)
        # Write the new file before unlinking the old one. Interrupted between
        # the two, the session comes back under both names — recoverable, unlike
        # coming back under neither.
        # The prompt depends on the name now: models/sessions/<name>.md is the
        # third layer, so a renamed session must pick up the file for what it
        # is called *now* or it keeps the old channel's instructions. Carryover
        # first, as every rebuild must.
        try:
            await s.reload_session_behavior()
        except Exception:
            logger.exception("session %s: could not re-resolve its behavior "
                             "file after the rename", new)
        await s.persist()
        await store.delete_live(old, self.config)
        logger.info("session %s renamed to %s", old, new)
        return {"ok": True, "name": new, "was": old}

    async def delete(self, name):
        """Forget a session, and everything it ever wrote.

        Refused while a turn is running: dropping the Session out of the registry
        would not stop the turn, it would just leave it emitting into an object
        nothing can reach, holding an engine slot until it finished.

        Everything this conversation wrote goes: the live file under state/live
        (without the unlink the session would simply reappear at the next
        restart), and every `<id>_*` transcript and journal under state/sessions.
        Those used to be kept, on the argument that an archive is a record of a
        conversation that existed and deleting the conversation is not a claim
        that it never did. The argument is fine and the behaviour was still
        wrong — a delete that leaves megabytes of transcripts behind is not the
        thing the button says it is, and there was no other way to reach them.

        Scoped by id, never by name: a renamed session's older archives still
        carry whatever it was called at the time, and the id is the only thing
        joining them.

        Two things are deliberately left, and should stay left. Nobody should
        "complete" this later:

          * models/sessions/<name>.md — authored input, not state. The name is
            reusable the moment this returns, and the next session to claim it
            is meant to inherit those instructions.
          * the handoff blocks in the long-term memory file — one document every
            session appends to, headed by the name each had at the time. No one
            session owns enough of it to rewrite it.
        """
        s = self.sessions.get(name)
        if s is None:
            return {"ok": False, "error": f"no session {name!r}"}
        if s.busy:
            return {"ok": False, "error": "a turn is running in this session"}
        messages = len([m for m in s.history if m["role"] != "system"])
        # Out of the registry before anything that awaits, and synchronously, so
        # there is no window to land in. The purge goes to a thread; a submit
        # arriving while it ran would start a turn on a condemned session, and
        # that turn's rollover would write a fresh archive *after* the sweep.
        # ws_handler sees `dead` and re-homes the socket onto a new session.
        s.dead = True
        del self.sessions[name]
        # Before the purge, not after: an open segment unlinked underneath
        # Journal.write is silently recreated by the next append, and close()
        # also writes that segment's index record, which belongs ahead of the
        # tombstone rather than after it.
        await s.close_journal()
        try:
            files, freed = await store.purge_archives(s.session_id, self.config)
        except OSError as e:
            files, freed = 0, 0
            logger.warning("session %s: could not delete its archives: %s",
                           name, e)
        # Tell the people looking at it before it stops existing; their sockets
        # stay open, so the notice reaches them and the name is theirs to reuse.
        s.emit(ev("session_state", what="deleted", messages=0,
                  files=files, bytes=freed))
        await store.delete_live(name, self.config)
        # The id leaves the registry with the session. Nothing on disk carries
        # it any more, so nothing is misidentified by letting it go.
        if self._ids is not None:
            self._ids.discard(s.session_id)
        try:
            await store.unregister_id(s.session_id, self.config)
        except OSError as e:
            logger.warning("could not drop %s from the session id log: %s",
                           s.session_id, e)
        # The index is append-only, so the archive and journal lines written
        # over this session's life cannot be taken back. Without a tombstone it
        # would go on listing files that no longer exist, with nothing to say
        # why. Non-fatal, as rename's record is.
        try:
            await store.append_index(
                {"t": "delete", "id": s.session_id, "name": name,
                 "at": datetime.now().isoformat(timespec="seconds"),
                 "messages": messages, "files": files, "bytes": freed},
                self.config)
        except OSError as e:
            logger.warning("could not record the delete in the session index: %s", e)
        logger.info("session %s deleted (id %s, %d messages discarded, "
                    "%d archive file(s), %d bytes freed)",
                    name, s.session_id, messages, files, freed)
        return {"ok": True, "name": name, "messages": messages,
                "files": files, "bytes": freed}

    async def close(self):
        # Stop the turns first, and wait for them. A turn in flight owns its
        # session's history: repairing and persisting underneath it saves a
        # half-written exchange, and the turn's own finally then persists again
        # after us, so the save below would be the one that loses. Cancelling
        # is also the only thing that kills a running exec — the tool kills its
        # process group from the CancelledError handler, and nothing else does,
        # so a shell started by the model outlives the core without this.
        turns = [t for s in self.sessions.values()
                 if (t := s._turn_task) is not None and not t.done()]
        for t in turns:
            t.cancel()
        if turns:
            logger.info("shutdown: stopping %d turn(s) in flight", len(turns))
            # Bounded: a turn that will not unwind must not hold the shutdown
            # open. Persisting over it is the lesser loss — the alternative is
            # systemd's SIGKILL arriving in the middle of the same write.
            _, pending = await asyncio.wait(turns, timeout=CLOSE_TURN_GRACE)
            if pending:
                logger.warning("shutdown: %d turn(s) still running after %ss — "
                               "saving anyway", len(pending), CLOSE_TURN_GRACE)

        for s in self.sessions.values():
            s.repair_history()
            # The other half of the durability story: the per-turn save covers a
            # crash, this covers a clean stop, and between them the only thing a
            # restart can cost is a turn that was still running.
            try:
                await s.persist()
            except Exception:
                # One unwritable session must not cost the other nine theirs,
                # nor the engine and forward cleanup below.
                logger.exception("shutdown: could not persist session %s", s.name)

        # Every engine anything still points at, not just the manager's cache.
        # After a reload a session can hold an older engine the cache has
        # already forgotten, and a retired one is waiting here precisely
        # because no earlier pass was allowed to close it — each owns a pool.
        closing, seen = [], set()
        for engine in (*(e for e, _, _ in self._engines.values()),
                       *(s.engine for s in self.sessions.values()),
                       *(b["engine"] for s in self.sessions.values()
                         if (b := s._pending_config)),
                       *self._retired_engines):
            if engine is not None and id(engine) not in seen:
                seen.add(id(engine))
                closing.append(engine)
        self._retired_engines = []
        for engine in closing:
            try:
                await engine.close()
            except Exception:
                logger.debug("engine close during shutdown", exc_info=True)
        await self.forwards.close()
