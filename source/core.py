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

import paths
import session as store
from llm_engine import ChatResult, LLMEngine
from token_counter import count_messages, count_text, count_tools

logger = logging.getLogger("letclaw.core")

PROTOCOL_VERSION = 1

CONFIG_PATH = paths.REPO_ROOT / "config.yaml"

DEFAULT_CONTEXT_LENGTH = 12000
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

CARRYOVER_MARKER = "\n\n=== CONTINUED SESSION ===\n"

# Seconds held back from turn_timeout so the closing round still fits inside the
# wall after a long think. Thinking stops at turn_timeout - this.
DEFAULT_ANSWER_TIME_RESERVE = 120
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

# Marks the forced-conclusion directive in base.md. The core extracts the
# paragraph after this line and re-sends it as a user message when a cut-off
# round cannot be resumed (resume_mode: off, or a server that won't prefill) —
# the model's own words, tunable without a code change. Missing marker = a short
# built-in sentence.
CONCLUDE_MARKER = "## Forced conclusion"
CONCLUDE_FALLBACK = ("Your reasoning was cut off at the token limit. Stop "
                     "reasoning now and give your best final answer. If you "
                     "truly cannot conclude, say in one sentence what single "
                     "fact is missing.")

# Sent when a thinking pad has filled the context and is about to be compacted:
# the model writes down what it has established, and that note replaces the
# reasoning it summarises. Lives in base.md for the same reason as the above.
CHECKPOINT_MARKER = "## Checkpoint"
CHECKPOINT_FALLBACK = ("Your reasoning has filled the available room. Write "
                       "down everything you have established so far — every "
                       "intermediate value exactly as you computed it, every "
                       "assumption made, and what still has to be done. This "
                       "note replaces the reasoning it summarises, so whatever "
                       "you leave out is lost. This is not the final answer.")

# The base behavior — identity, working style and the tool notes — lives in
# plain Markdown (behavior.base_file) so it can be tuned without a code change.
# It is read once per core and shared by every session, and it is always in the
# system prompt: a missing file is a startup warning and an empty base, the
# same behavior as a missing per-model behavior file.
DEFAULT_BASE_FILE = "models/base.md"


def ev(t, **kw):
    """An event is just a tagged dict; 13 types don't warrant a class each."""
    return {"t": t, **kw}


def load_config(path=None):
    """Read config.yaml. Raises rather than exiting — a service must not sys.exit."""
    path = Path(path or CONFIG_PATH)
    if not path.exists():
        raise FileNotFoundError(f"No config.yaml at {path} "
                                "(copy config.example.yaml and customise)")
    with open(path) as f:
        return yaml.safe_load(f) or {}


def known_models(config):
    return [k for k in config.get("models", {}) if k != "default_model"]


def build_engine(config, model_name=None):
    """Construct an engine for one configured model entry.

    Synchronous: nothing here does real I/O. The behavior file is read separately
    so the engine can be cached per model while behavior stays per session.
    """
    models_cfg = config.get("models", {})
    name = model_name or models_cfg.get("default_model")
    entry = models_cfg.get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"Unknown model '{name}'. Configured: "
                         f"{', '.join(known_models(config)) or '(none)'}")
    base_url = entry.get("base_url")
    if not base_url:
        raise ValueError(f"Model '{name}' has no base_url — every entry must name "
                         "the server that serves it.")
    engine = LLMEngine(
        base_url=base_url, api_key=entry.get("api_key", ""),
        default_model=entry.get("provider_name", name),
        timeout=(int(entry.get("turn_timeout", 36000) or 0) or None),
        temperature=entry.get("temperature"),
        max_tokens=entry.get("max_tokens"),
        top_p=entry.get("top_p"),
    )
    engine.context_length = entry.get("context_length", DEFAULT_CONTEXT_LENGTH)
    # The answer sheet: the cap the closing round runs under, once thinking has
    # been shut with </think>. Every token of it buys answer, not reasoning.
    # 0 = the closing round keeps the per-round max_tokens.
    engine.max_output = int(entry.get("max_output_tokens", 32768) or 0)
    # Hard wall for one turn, in seconds — a limit on a round, thinking model or
    # not. 0 = no wall. It must reach the HTTP client too, whose 600 s read
    # timeout would otherwise kill a long round.
    engine.turn_timeout = int(entry.get("turn_timeout", 36000) or 0)
    # Whether a cut-off round is resumed at all. There is deliberately no round
    # budget: the pad grows until time or context stops it, both of which are
    # real limits. A round that reasons nothing ends it (see _think_pad).
    engine.reasoning = bool(entry.get("reasoning", True))
    # Seconds held back from turn_timeout so the answer still fits inside the
    # wall after a long think. Thinking stops at turn_timeout - this.
    engine.answer_time_reserve = int(entry.get("answer_time_reserve",
                                               DEFAULT_ANSWER_TIME_RESERVE) or 0)
    # How many times a pad that has filled the context may be distilled to a
    # checkpoint and carry on. Off by default: compaction is lossy, and for a
    # derivation that carries exact values through many steps, concluding from a
    # complete pad usually beats continuing from a summarised one.
    engine.max_pad_compactions = int(entry.get("max_pad_compactions", 0) or 0)
    mode = str(entry.get("resume_mode", "auto")).lower()
    if mode not in ("auto", "chat", "raw", "off"):
        logger.warning("model %s: bad resume_mode %r, using auto", name, mode)
        mode = "auto"
    engine.resume_mode = mode
    # Qwen3 templates turn this into a system directive; llama.cpp defaults to
    # xhigh, which is itself a large part of why rounds run out mid-reasoning.
    engine.reasoning_effort = entry.get("reasoning_effort") or None
    return engine, name, entry


def build_all_engines(config):
    """One engine per configured model, or raise.

    This is the config validator. build_engine is synchronous and opens no
    sockets, so building the lot is cheap — and building them is a far better
    check than inspecting keys, because it is the same code path that will run
    for real. Raises ValueError naming the offending model.
    """
    models_cfg = config.get("models") or {}
    if not isinstance(models_cfg, dict):
        raise ValueError("models: must be a mapping of name -> settings")
    names = known_models(config)
    if not names:
        raise ValueError("no models configured")
    default = models_cfg.get("default_model")
    if default and default not in names:
        raise ValueError(f"default_model '{default}' is not one of: {', '.join(names)}")
    return {name: build_engine(config, name) for name in names}


def diff_config(old, new, prefix="", out=None, limit=40):
    """Dotted 'a.b.c: old -> new' lines describing what changed.

    For the reply and one log line, so a reload says what it did rather than
    just that it happened. Dicts recurse; anything else is compared whole — a
    changed list is one line, not a diff of its elements.
    """
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    for key in sorted(set(old) | set(new)):
        if len(out) >= limit:
            out.append("…")
            break
        path = f"{prefix}{key}"
        a, b = old.get(key, _MISSING), new.get(key, _MISSING)
        if isinstance(a, dict) and isinstance(b, dict):
            diff_config(a, b, f"{path}.", out, limit)
        elif a != b:
            out.append(f"{path}: {_short(a)} -> {_short(b)}")
    return out


class _Missing:
    def __repr__(self):
        return "(unset)"


_MISSING = _Missing()


def _short(v, n=40):
    text = repr(v) if not isinstance(v, str) else v
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


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

    What goes to disk should be a conversation someone could pick up, and the
    process can go down at any point in a turn — the turn tasks are not joined
    on shutdown. Three shapes mean "this turn did not finish":

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


def strip_directive_sections(base_behavior):
    """The base behavior minus the two blocks the core sends on its own.

    "## Checkpoint" and "## Forced conclusion" are templates, not behavior: each
    is extracted and sent as a *user* message at the one moment it applies. They
    live in base.md so they can be tuned without a code change, but they are
    also, redundantly, part of every system prompt — and a model that reads its
    whole system prompt as standing instruction will simply obey them. Claude
    Opus 5 over Argo answers nothing at all, having been told on every turn that
    "This is not the final answer".

    Local thinking models ignore them, so stripping is opt-in per model
    (models.<name>.strip_directives) rather than done for everyone. Extraction
    is unaffected: the directives still reach the model when they are meant to.
    """
    out, skipping = [], False
    for line in base_behavior.splitlines(keepends=True):
        if line.startswith("## "):
            skipping = line.strip() in (CHECKPOINT_MARKER, CONCLUDE_MARKER)
        if not skipping:
            out.append(line)
    return "".join(out).rstrip() + "\n"


def extract_conclude_directive(base_behavior):
    """The paragraph that forces a cut-off round to conclude. See extract_directive."""
    return extract_directive(base_behavior, CONCLUDE_MARKER, CONCLUDE_FALLBACK)


def extract_checkpoint_directive(base_behavior):
    """The paragraph that asks a full pad to distil itself. See extract_directive."""
    return extract_directive(base_behavior, CHECKPOINT_MARKER, CHECKPOINT_FALLBACK)


def extract_directive(base_behavior, marker, fallback):
    """The paragraph after `marker` in the base behavior file.

    These are messages the core sends the model on its own initiative, so they
    live in base.md — tunable with /reload, no code change. Without the heading,
    a short built-in sentence.
    """
    if marker not in base_behavior:
        return fallback
    _, _, rest = base_behavior.partition(marker)
    # The paragraph ends at the next heading or end of file. HTML comments are
    # notes to whoever edits the file, not words for the model.
    para = []
    in_comment = False
    for line in rest.splitlines():
        if line.startswith("#"):
            break
        if in_comment:
            in_comment = "-->" not in line
            continue
        if line.lstrip().startswith("<!--"):
            in_comment = "-->" not in line
            continue
        para.append(line)
    text = "\n".join(para).strip()
    return text or CONCLUDE_FALLBACK


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
        self.history = []
        self.tools = []
        self.schemas = []
        self.tools_tok = 0

        self.max_tool_rounds = int(config.get("tools", {}).get("max_iterations", 8))
        self.max_history = conv.get("max_history_messages", 100)
        # Per session, never on the shared config dict: a session that disables
        # its own rollover must not disable everyone else's.
        self.rollover_pct = int(conv.get("rollover_at_percent", DEFAULT_ROLLOVER_PCT) or 0)
        self.rollover_mode = str(conv.get("rollover_mode", DEFAULT_ROLLOVER_MODE)).lower()
        if self.rollover_mode not in ("auto", "ask", "off"):
            logger.warning("session %s: bad rollover_mode %r, using auto",
                           name, self.rollover_mode)
            self.rollover_mode = "auto"
        # Roll over before a turn that has no room left to think in, rather than
        # after one whose thinking was squeezed out. Auto mode only.
        self.rollover_before_think = bool(conv.get("rollover_before_think", True))
        self.prompt_timeout = int(conv.get("prompt_timeout", 60))
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
        self._last_used = None   # measured size of the next prompt, if known
        # Serialises this session's own writes to state/live/. Not self.lock:
        # every save point below is already inside it.
        self._save_lock = asyncio.Lock()
        self._persisted = False  # has this session ever been written to disk?
        # A reload that arrived mid-turn, waiting for the turn to end.
        self._pending_config = None

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
                  busy=self.lock.locked(),
                  messages=[m for m in self.history if m.get("role") != "system"],
                  rollover={"mode": self.rollover_mode, "percent": self.rollover_pct,
                            "count": self.rollover_count},
                  missed=missed,
                  gap=bool(last_seq and missed and missed[0]["seq"] > last_seq + 1))

    # ---- state -----------------------------------------------------------

    @property
    def budget(self):
        return getattr(self.engine, "context_length", DEFAULT_CONTEXT_LENGTH)

    def estimate(self, messages=None):
        """Deliberately conservative context size, for servers that hide usage."""
        msgs = self.history if messages is None else messages
        return int((count_messages(msgs) + self.tools_tok) * ESTIMATE_SCALE)

    def fresh_history(self, carryover=""):
        """System message: base behavior, per-model behavior, then carryover.

        The base (identity + tool notes) comes from the shared base file and is
        in the prompt even when the session has no per-model file.
        """
        content = self.base_behavior
        if self.behavior:
            content += "\n=== BEHAVIOR ===\n" + self.behavior
        if carryover:
            content += CARRYOVER_MARKER + carryover
        return [{"role": "system", "content": content}]

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

        if blob["engine"] is not None:
            self.engine = blob["engine"]
        if blob["behavior"] is not None and blob["behavior"] != (self.base_behavior, self.behavior):
            self.base_behavior, self.behavior = blob["behavior"]
        # The system message is rebuilt either way: the base file may have
        # changed under a live session, and it is the only place the tool
        # notes live now.
        carried = self.carryover()
        body = [m for m in self.history if m["role"] != "system"]
        self.history = self.fresh_history(carried) + body

        if blob["lost_model"]:
            self.emit(ev("notice", level="warn",
                         text=f"model '{self.model_name}' is no longer in the config — "
                              f"this session keeps using it, but /model cannot return to it"))
        self.emit(ev("session_state", what="reloaded", model=self.model_name,
                     budget=self.budget, changed=blob["changed"]))

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
        self.last_seq = int(payload.get("last_seq") or 0)
        self._seq = itertools.count(self.last_seq + 1)
        self.turn_id = int(payload.get("turn_id") or 0)
        if payload.get("rollover_pct") is not None:
            self.rollover_pct = int(payload["rollover_pct"])
        self.declined_at = payload.get("declined_at")
        self.warned_over = bool(payload.get("warned_over"))
        self._last_used = payload.get("last_used")
        self.total_output = int(payload.get("total_output") or 0)
        self.rollover_count = int(payload.get("rollover_count") or 0)
        self._persisted = True

    # ---- the turn --------------------------------------------------------

    async def run_turn(self, text, origin=None):
        """One user turn: model rounds, tool rounds, then rollover or trim.

        Refuses rather than queues when a turn is already running — see the
        module docstring.
        """
        if self.lock.locked():
            self.emit(ev("busy", reason="a turn is already running in this session"))
            return
        async with self.lock:
            self.turn_id += 1
            self.turn_events.clear()
            self.emit(ev("turn_start", turn_id=self.turn_id, text=text, origin=origin))
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
        self.history.append({"role": "user", "content": text})
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
            out_estimated += count_text(result.text or "") + count_text(result.reasoning or "")
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

            def on_text(chunk, box=ttft_box):
                if box["v"] is None:
                    box["v"] = time.monotonic() - t0  # measured at the model, not the client
                # First words of the round: the thinking behind them is over, so
                # settle the count before this delta goes out. Clients close the
                # thinking off when text arrives, and a figure a throttle window
                # short is the one they would close it on.
                if reason_pending:
                    push_reasoning_stat(time.monotonic(), flush=True)
                self.emit(ev("text", delta=chunk))

            def on_reasoning(chunk):
                nonlocal reason_t0
                self.emit(ev("reasoning", delta=chunk))
                now = time.monotonic()
                if reason_t0 is None:
                    reason_t0 = now
                reason_pending.append(chunk)
                if now - reason_sent >= REASONING_STAT_INTERVAL:
                    push_reasoning_stat(now)

            # extra_messages ride along for this one round without entering the
            # conversation: the closing round needs the "do not call tools"
            # directive, but a turn's history must not collect scaffolding.
            msgs = self.history + list(extra_messages or [])
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
                msg = {"role": "tool", "tool_call_id": tc["id"],
                       "content": "error: cancelled"}
                slots[tc["id"]] = msg
                self.history.append(msg)

            for tc in result.tool_calls:
                # Emitted BEFORE the call: a 120-second exec used to be silent.
                self.emit(ev("tool_call", id=tc["id"], name=tc["name"],
                             arguments=tc["arguments"]))
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
                await self.jot(
                    f"{tag} tool_result {tc['name']} {tc['id']} {len(full)} chars", full)
                self.emit(ev("tool_result", id=tc["id"], name=tc["name"], size=len(out)))

        def was_cut_off(result):
            """Reasoning ate the whole budget: thought hard, said nothing."""
            return (not result.text and not result.wants_tool
                    and result.finish_reason == "length"
                    and self.engine.reasoning)

        try:
            for _ in range(self.max_tool_rounds):
                result = await call_round(tools=self.schemas)
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
                    full_response = await self.engine.chat(self.history)
                    self.emit(ev("text", delta=full_response))
                    # The one answer that never goes through call_round.
                    await self.jot(f"turn {self.turn_id} fallback text", full_response)
                    had_error = False
                except Exception as e2:
                    self.emit(ev("error", msg=f"{type(e2).__name__}: {e2}", fatal=False))
                    self.history.pop()  # drop the user message that never landed
                    return

        # Deltas from a round that died before tally() could re-sync are still
        # uncounted here. Counting them now keeps the figure below — and the last
        # one the clients were given — the same number.
        if reason_pending:
            push_reasoning_stat(time.monotonic(), flush=True)

        # The server's figure only if every round supplied one: a partial sum is
        # worse than the estimate, because it looks exact while missing rounds.
        out_exact = rounds > 0 and out_reported == rounds
        out_tok = out_measured if out_exact else out_estimated
        # Counted before the branches, so a turn that errored out after two good
        # rounds still records what those rounds generated. Rounds that never
        # returned never reached tally(), so they contribute nothing.
        self.total_output += out_tok

        if not had_error and full_response:
            self.history.append({"role": "assistant", "content": full_response})
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
        return self.budget - self.estimate() - answer - PAD_CONTEXT_MARGIN

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
        if self._pad_headroom() >= MIN_THINK_HEADROOM:
            return
        used = self.estimate()
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
        reserve = getattr(self.engine, "answer_time_reserve", DEFAULT_ANSWER_TIME_RESERVE)
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
            self.history.append({"role": "user", "content": directive})
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
        elif len(self.history) > self.max_history:
            system = [m for m in self.history if m["role"] == "system"]
            rest = [m for m in self.history if m["role"] != "system"][-self.max_history:]
            while rest and rest[0]["role"] == "tool":
                rest = rest[1:]  # never open on an orphaned tool result
            self.history = system + rest

    async def _maybe_roll(self, used, trip):
        pct = used * 100 // self.budget if self.budget else 0
        if self.rollover_mode == "auto":
            await self.roll_over(f"context at {pct}%", used, trip)
            return True
        if self.rollover_mode == "ask":
            # Don't re-ask every turn once refused; wait for real growth.
            if self.declined_at is not None and used < self.declined_at + max(1, self.budget // 20):
                return False
            if await self._ask_rollover(used, pct):
                await self.roll_over(f"context at {pct}%", used, trip)
                return True
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
        never the conversation.
        """
        self.rollover_count += 1
        used = used if used is not None else self.estimate()
        self.emit(ev("rollover_start", reason=reason, used=used,
                     budget=self.budget, count=self.rollover_count))
        try:
            transcript = await store.save_transcript(
                self.history, self.model_name, self.config, self.name,
                session_id=self.session_id)
        except OSError as e:
            transcript = None
            self.emit(ev("notice", level="warn", text=f"could not save transcript: {e}"))
        # Closed here, not at the end: the path has to exist before the
        # carryover that advertises it is built, and the next segment then
        # opens on the first turn of the fresh window.
        record = await self.close_journal()

        # Three caps: the absolute one, a share of the window so a small model
        # does not spend an eighth of its context on the summary for ever, and
        # what is actually free right now.
        summary_max = min(HANDOFF_MAX_TOKENS, self.budget // HANDOFF_WINDOW_SHARE,
                          self.budget - used - HANDOFF_MARGIN)
        handoff = ""
        if summary_max >= HANDOFF_MIN_TOKENS:
            try:
                handoff = await store.build_handoff(self.engine, self.history,
                                                      max_tokens=summary_max)
            except Exception as e:
                self.emit(ev("notice", level="warn",
                             text=f"handoff failed ({e}) — carrying the last exchange only"))
        else:
            self.emit(ev("notice", level="warn",
                         text="no headroom to summarise — carrying the last exchange only"))

        handoff_path = None
        if handoff:
            try:
                handoff_path = await store.append_handoff(
                    handoff, self.model_name, transcript, self.config, self.name,
                    journal_path=record)
            except OSError as e:
                self.emit(ev("notice", level="warn", text=f"could not append handoff: {e}"))

        fresh = self.fresh_history(
            store.format_carryover(handoff, transcript, journal_path=record))
        tail = store.last_exchange(self.history)
        if tail and (not trip or self.estimate(fresh + tail) < trip):
            fresh += tail
        elif tail:
            self.emit(ev("notice", level="warn",
                         text="dropped the carried exchange — it would not fit"))
        self.history = fresh
        self.emit(ev("rollover_done",
                     transcript=str(transcript) if transcript else None,
                     record=str(record) if record else None,
                     handoff=str(handoff_path) if handoff_path else None,
                     used=self.estimate(), budget=self.budget,
                     count=self.rollover_count))

    # ---- commands --------------------------------------------------------

    async def command(self, name, args=""):
        """Run a slash command. Returns a reply dict for the caller only.

        Read-only commands are replies, not broadcasts: /info is one client's
        view of the session and has no business appearing in everyone's stream.
        State-changing ones take the turn lock, because a /clear landing between
        a tool-call turn and its results would corrupt the history for good.
        """
        name = name.lstrip("/")
        if name == "info":
            used = self.estimate()
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
                "used": used, "budget": self.budget, "estimated": True,
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
                    "base": self.base_behavior or "",
                    "behavior": self.behavior or ""}
        if name == "models":
            return {"ok": True, "current": self.model_name,
                    "configured": known_models(self.config)}
        if name == "reload":
            # Manager-level, and deliberately outside the turn lock: reload defers
            # any session that is busy, so taking the lock here would have it defer
            # the very session that asked, or deadlock waiting on itself.
            return await self.manager.reload()

        if self.lock.locked():
            return {"ok": False, "error": "a turn is running in this session"}
        async with self.lock:
            # Each of these rewrites the history, so each has to reach disk:
            # a /clear followed by a crash would otherwise bring back the
            # conversation the user just threw away.
            if name == "clear":
                self.history = self.fresh_history()
                self.declined_at, self.warned_over = None, False
                # /clear starts a new window as surely as a rollover does, it
                # just does not summarise on the way. Leaving the segment open
                # would append the next, unrelated conversation to the end of
                # this one, with the turn numbers running straight on.
                record = await self.close_journal()
                self.emit(ev("session_state", what="cleared", messages=0,
                             record=str(record) if record else None))
                await self.persist()
                return {"ok": True}
            if name == "new":
                trip = (int(self.budget * self.rollover_pct / 100)
                        if self.rollover_pct else 0)
                await self.roll_over("on request", trip=trip)
                self.declined_at, self.warned_over = None, False
                await self.persist()
                return {"ok": True}
            if name == "model":
                target = args.strip()
                if not target:
                    return {"ok": True, "current": self.model_name,
                            "configured": known_models(self.config)}
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
        self.engine = engine
        self.model_name = name
        # The base is shared by every session; the model file is per model.
        self.base_behavior, self.behavior = await self.manager.behavior_for(entry)
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
        self.config_path = Path(config_path or CONFIG_PATH)
        self.sessions = {}
        self._engines = {}
        self._behaviors = {}
        # Session ids in use, from logs/session_ID.log. None until first needed:
        # __init__ is synchronous and this is a file read.
        self._ids = None
        # Read synchronously at startup — one small local file, the same way
        # the config file itself is read. A missing file warns and degrades to
        # an empty base; the core keeps starting.
        self.base_behavior = self._behavior_sync(
            config.get("behavior", {}).get("base_file") or DEFAULT_BASE_FILE)
        self.conclude_directive = extract_conclude_directive(self.base_behavior)
        self.checkpoint_directive = extract_checkpoint_directive(self.base_behavior)
        self._engine_lock = asyncio.Lock()
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
                self._engines[name] = build_engine(self.config, name)
            return self._engines[name]

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

    def _behavior_sync(self, path):
        """Cached read, synchronous: a missing file is a warning and ""."""
        if path not in self._behaviors:
            p = paths.resolve(path)
            self._behaviors[path] = p.read_text() if p.exists() else ""
            if not p.exists():
                logger.warning("behavior file not found: %s", p)
        return self._behaviors[path]

    async def _behavior_text(self, path):
        """Cached read off the event loop; a missing file is a warning and ""."""
        return await asyncio.to_thread(self._behavior_sync, path)

    async def behavior_for(self, entry):
        """(base, model) behavior for a session: the shared base file, then
        the model's own file. Both cached; a missing file is "" (warned)."""
        base = await self._behavior_text(
            self.config.get("behavior", {}).get("base_file") or DEFAULT_BASE_FILE)
        # Per model, because it is a property of how the model reads a prompt,
        # not of the prompt. Applied after the cached read so the cache still
        # holds the file as written.
        if entry.get("strip_directives"):
            base = strip_directive_sections(base)
        model = await self._behavior_text(entry.get("behavior_file", "models/default.md"))
        return base, model

    async def get(self, name, model=None):
        if name not in self.sessions:
            s = Session(name, self, self.config)
            s.session_id = await self._new_session_id()
            s.tools = self._tools
            s.schemas = self._schemas
            s.tools_tok = self._tools_tok
            await s.use_model(model)
            self.sessions[name] = s
            await store.register_id(s.session_id, name, self.config)
            logger.info("session %s created on model %s (id %s)",
                        name, s.model_name, s.session_id)
        return self.sessions[name]

    async def reload(self, path=None):
        """Re-read config.yaml and apply it to everything already running.

        Nothing is committed until the new file has parsed *and* produced a
        working engine for every model in it, so a half-finished edit — the
        normal state of a file open in an editor — cannot take a running core
        down. On failure this returns the error and changes nothing at all.

        Restart-only settings are core.bind and core.port (the socket is already
        bound; rebinding would drop every client, which is the thing being
        avoided) and logging.file. Everything else is live.
        """
        path = path or self.config_path
        try:
            fresh = await asyncio.to_thread(load_config, path)
            engines = build_all_engines(fresh)
        except (OSError, ValueError, yaml.YAMLError) as e:
            logger.warning("config reload refused: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        old_cfg = copy.deepcopy(self.config)
        changed = diff_config(old_cfg, fresh)
        if not changed:
            return {"ok": True, "changed": [], "updated": 0, "deferred": 0,
                    "note": "config on disk is identical to the running one"}

        # In place, never rebound: the manager, the aiohttp app and every session
        # hold this same dict, so mutating it is what makes core.token, the state
        # directories and GET /models pick the new values up with no plumbing.
        self.config.clear()
        self.config.update(fresh)

        old_engines, self._engines = self._engines, engines
        self._behaviors.clear()
        # The base file is re-read like any other behavior file: /reload is
        # how an edited base.md reaches the live sessions.
        self.base_behavior = await self._behavior_text(
            fresh.get("behavior", {}).get("base_file") or DEFAULT_BASE_FILE)
        self.conclude_directive = extract_conclude_directive(self.base_behavior)
        self.checkpoint_directive = extract_checkpoint_directive(self.base_behavior)
        tools_cfg = fresh.get("tools", {})
        workdir = tools_cfg.get("workdir") or str(paths.REPO_ROOT)
        from tools import build_tools
        self._tools = build_tools(tools_cfg, workdir=workdir)
        self._schemas = [t.spec for t in self._tools]
        self._tools_tok = count_tools(self._schemas)

        log_cfg = fresh.get("logging", {})
        if log_cfg.get("level"):
            logging.getLogger().setLevel(
                getattr(logging, str(log_cfg["level"]).upper(), logging.INFO))

        old_conv, new_conv = old_cfg.get("conversation", {}), fresh.get("conversation", {})
        old_tools, new_tools = old_cfg.get("tools", {}), tools_cfg
        # (attribute, what the old config said, what the new one says). apply_config
        # reassigns only where the session still holds the old value.
        scalars = [
            ("max_tool_rounds", int(old_tools.get("max_iterations", 8)),
                                int(new_tools.get("max_iterations", 8))),
            ("max_history", old_conv.get("max_history_messages", 100),
                            new_conv.get("max_history_messages", 100)),
            ("rollover_pct", int(old_conv.get("rollover_at_percent", DEFAULT_ROLLOVER_PCT) or 0),
                             int(new_conv.get("rollover_at_percent", DEFAULT_ROLLOVER_PCT) or 0)),
            ("rollover_mode", str(old_conv.get("rollover_mode", DEFAULT_ROLLOVER_MODE)).lower(),
                              str(new_conv.get("rollover_mode", DEFAULT_ROLLOVER_MODE)).lower()),
            ("prompt_timeout", int(old_conv.get("prompt_timeout", 60)),
                               int(new_conv.get("prompt_timeout", 60))),
            ("rollover_before_think", bool(old_conv.get("rollover_before_think", True)),
                                      bool(new_conv.get("rollover_before_think", True))),
            # Turning this off leaves the open segment where it is: it closes at
            # the next rollover, holding what was recorded before the reload.
            ("journal_on", bool(old_conv.get("journal", True)),
                           bool(new_conv.get("journal", True))),
        ]

        updated = deferred = 0
        # A list, not the live view: behavior_for awaits a file read, and a client
        # attaching during that await would create a session mid-iteration.
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
                "behavior": (await self.behavior_for(entry[2])) if entry else None,
                "lost_model": entry is None,
            }
            if sess.lock.locked():
                sess._pending_config = blob
                deferred += 1
            else:
                sess.apply_config(blob)
                updated += 1

        # Only the ones nothing points at any more. A session mid-stream on an old
        # engine must keep it: closing the client under it would abort the read.
        live = {id(s.engine) for s in self.sessions.values()}
        live |= {id(b["engine"]) for s in self.sessions.values()
                 if (b := s._pending_config) and b["engine"] is not None}
        for engine, _, _ in old_engines.values():
            if id(engine) not in live:
                try:
                    await engine.close()
                except Exception:
                    logger.debug("engine close during reload", exc_info=True)

        logger.info("config reloaded from %s: %s (%d session(s) updated, %d deferred)",
                    path, "; ".join(changed), updated, deferred)
        return {"ok": True, "changed": changed, "updated": updated, "deferred": deferred}

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
                 "clients": len(s.subscribers), "busy": s.lock.locked()}
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
        if s.lock.locked():
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
        await s.persist()
        await store.delete_live(old, self.config)
        logger.info("session %s renamed to %s", old, new)
        return {"ok": True, "name": new, "was": old}

    async def delete(self, name):
        """Forget a session. The conversation is discarded, nothing is written.

        Refused while a turn is running: dropping the Session out of the registry
        would not stop the turn, it would just leave it emitting into an object
        nothing can reach, holding an engine slot until it finished.

        Past rollover transcripts under state/sessions are archives of their own
        and are deliberately left alone — this deletes a live conversation, not a
        record. The live file under state/live is that conversation, so it goes:
        without the unlink the session would simply reappear at the next restart.
        """
        s = self.sessions.get(name)
        if s is None:
            return {"ok": False, "error": f"no session {name!r}"}
        if s.lock.locked():
            return {"ok": False, "error": "a turn is running in this session"}
        messages = len([m for m in s.history if m["role"] != "system"])
        # Tell the people looking at it before it stops existing; their sockets
        # stay open, so the notice reaches them and the name is theirs to reuse.
        s.emit(ev("session_state", what="deleted", messages=0))
        s.dead = True
        del self.sessions[name]
        await store.delete_live(name, self.config)
        # The id leaves the registry with the session. Its archives under
        # state/sessions keep it in their filenames — they are records of a
        # conversation that existed, and deleting a live session is not a claim
        # that it never did.
        if self._ids is not None:
            self._ids.discard(s.session_id)
        try:
            await store.unregister_id(s.session_id, self.config)
        except OSError as e:
            logger.warning("could not drop %s from the session id log: %s",
                           s.session_id, e)
        logger.info("session %s deleted (id %s, %d messages discarded)",
                    name, s.session_id, messages)
        return {"ok": True, "name": name, "messages": messages}

    async def close(self):
        for s in self.sessions.values():
            s.repair_history()
            # The other half of the durability story: the per-turn save covers a
            # crash, this covers a clean stop, and between them the only thing a
            # restart can cost is a turn that was still running.
            await s.persist()
        for engine, _, _ in self._engines.values():
            await engine.close()
