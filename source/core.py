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
import openai
import re
import secrets
import time
from collections import deque
from pathlib import Path

import yaml

import behavior
import config as configlib
import config_edit
import paths
import session as store
from llm_engine import ChatResult, RoundInterrupted
from ssh_forward import SSHForwardManager
from token_counter import (count_text, count_tools, corrected_messages,
                           corrected_text, schema_cost, tokenizer_status)
from version import __version__

logger = logging.getLogger("letclaw.core")

PROTOCOL_VERSION = 1

DEFAULT_ROLLOVER_PCT = 90
DEFAULT_ROLLOVER_MODE = "auto"
ROLLOVER_MODES = ("auto", "ask", "off")


# ---- the conversation scalars, in one place -------------------------------
#
# Each of these settings is read on two paths — Session.__init__ at startup and
# _reload when the config changes under a running core — and for a long time
# each path spelled out its own key, default and coercion. Nothing tied the two
# spellings together, so they were free to disagree, and they did: the
# auto|ask|off check below lived only on the __init__ copy, which meant a typo
# in rollover_mode warned and fell back at startup but on /reload passed
# straight through to every session, matched none of the three readers, and
# silently disabled automatic rollover everywhere.
#
# So the fact lives once and both paths read it. The validation travels with
# the key rather than with one of its readers, which is the part that matters:
# there is no second place left for a new reader to forget it.
#
# `where` names whoever is asking, for the log line; None asks silently, which
# _reload uses for the old-config side of its before/after pair — that value
# exists only to be compared against, and warning twice about one typo is noise.

def _as_int(raw, default, where):
    # int() because YAML gives a string for `max_history_messages: "50"`, and
    # `len(history) > "50"` raises on 3.x rather than comparing. int_or, not
    # int: a bad number in a config file should cost a log line, not the turn.
    return configlib.int_or(raw, default)


def _as_pct(raw, default, where):
    # `or 0` before the coercion: rollover_at_percent documents `0 = never`,
    # and an explicit null in YAML means the same thing — without it int_or
    # would see None, hand back the default, and switch rollover back on.
    return configlib.int_or(raw or 0, default)


def _as_bool(raw, default, where):
    return bool(raw)


def _as_mode(raw, default, where):
    mode = str(raw).lower()
    if mode not in ROLLOVER_MODES:
        if where:
            logger.warning("%s: bad rollover_mode %r, using %s", where, mode, default)
        return default
    return mode


class _Scalar:
    """One conversation setting: where it comes from and what it has to be."""

    __slots__ = ("attr", "key", "default", "coerce", "note")

    def __init__(self, attr, key, default, coerce, note=""):
        self.attr, self.key, self.default = attr, key, default
        self.coerce, self.note = coerce, note

    def read(self, conv, where):
        return self.coerce(conv.get(self.key, self.default), self.default, where)


CONV_SCALARS = (
    _Scalar("max_history", "max_history_messages", 100, _as_int,
            # 0 — or a negative, or junk — means "never trim", matching
            # rollover_at_percent's documented `0 = never`, and matching what
            # the code accidentally did before: history[-0:] is the whole list,
            # not the empty one the number literally asks for.
            note="0 means never trim"),
    _Scalar("rollover_pct", "rollover_at_percent", DEFAULT_ROLLOVER_PCT, _as_pct,
            # Held per session, never written back to the shared config dict: a
            # session that disables its own rollover must not disable everyone
            # else's. Also a runtime brake — see _configured_rollover_pct.
            note="0 means never roll over"),
    _Scalar("rollover_mode", "rollover_mode", DEFAULT_ROLLOVER_MODE, _as_mode,
            note="auto | ask | off"),
    _Scalar("prompt_timeout", "prompt_timeout", 60, _as_int,
            note="seconds to wait for an answer to a rollover prompt"),
    _Scalar("rollover_before_think", "rollover_before_think", True, _as_bool,
            # Roll over before a turn that has no room left to think in, rather
            # than after one whose thinking was squeezed out. Auto mode only.
            note="auto mode only"),
    _Scalar("journal_on", "journal", True, _as_bool,
            # Turning this off mid-session leaves the open segment where it is:
            # it closes at the next rollover, holding what was recorded before.
            note="the append-only record of what this session generates"),
    _Scalar("turn_retries", "turn_retries", 5, _as_int,
            # How many times a turn may re-run after a model-server error (a
            # 400/429/500, a timeout, a dropped connection). 0 = never: the
            # pre-retry behaviour, where the turn ends and the question is
            # re-asked by hand. Each retry repairs the history first, so the
            # failure cannot poison the re-run — see the retry branch in _turn.
            note="re-runs of a turn after a model server error; 0 = never"),
)

# digest_turns is deliberately absent, and has to be: it is resolved per session
# against that session's model entry, so there is no single config-wide value
# this table could carry and no single old/new pair _reload could compare. Both
# paths ask _configured_digest_turns() instead. If you add a setting here that
# a model entry can override, it belongs with digest_turns, not in the table.

_SCALAR_BY_ATTR = {sc.attr: sc for sc in CONV_SCALARS}
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
# A turn digest replaces one turn's working-out, not a whole window, and the
# conversation keeps the question and the answer verbatim either side of it —
# so it needs far less room than a handoff, and unlike a handoff there is one
# per turn, so every token is paid for again on every later turn.
DIGEST_MAX_TOKENS = 600
DIGEST_MIN_TOKENS = 128
DIGEST_MARGIN = 200
DIGEST_WINDOW_SHARE = 50
# How much of a turn's reasoning is shown to the digester. The pad is the only
# unbounded input here — a long turn's thinking can exceed the whole window —
# and it is already on disk in full, so the tail is taken: the reasoning that
# led to the answer, rather than the exploring that preceded it.
DIGEST_PAD_TAIL_CHARS = 20_000
# The line every digest ends with. Appended by build_turn_digest, not written
# by the model — the model cannot be trusted to copy a path accurately, and
# does not need to — which is also why /digest expand can parse it back out
# with a regex instead of asking a model to point the way.
DIGEST_POINTER_RX = re.compile(
    r"\[full record: grep -n '\^## turn (\d+) ' (\S+)\]")
SESSION_MARKER = "\n=== SESSION ===\n"
CARRYOVER_MARKER = "\n\n=== CONTINUED SESSION ===\n"

# Seconds held back from turn_timeout so the closing round still fits inside the
# wall after a long think. Thinking stops at turn_timeout - this.
# Slack left between the pad and the context ceiling. Not an allowance for a
# bad token count — the margin is the same on a server that reports usage
# exactly, and it is needed there too. It is the only slack in the closing
# invariant: because the pad is sized as `free - reserve - this`, the worst
# case the design permits is `used + pad + reserve == budget - this`. At zero,
# the worst case the design permits is the server's hard limit, where being
# one token out is a 400 instead of a slightly shorter think. And `used_now()`
# describes the prompt of the round *before* this one, so even an exact
# measurement is one round stale by the time the pad is sized against it.
PAD_CONTEXT_MARGIN = 2000
# How many times a server that cannot resume is asked to conclude before we give
# up. Only the fallback path uses it; resumable servers never come here.
FORCED_CONCLUSION_TRIES = 3
# Below this much free context there is no room to think in, so the pad is not
# worth starting — the pre-turn check rolls over instead where it may.
MIN_THINK_HEADROOM = 4000
# Floor for the room one more tool round needs; the real figure is derived
# per session by Session._min_tool_round_room(), because the dominant term is
# configurable. Below that much free context the loop stops asking for work
# and asks for words — a round admitted here must be able to *finish*, and its
# result lands in the history whether or not there was room for it.
MIN_TOOL_ROUND_ROOM = 4000
# What else a tool round carries besides the result: the assistant message
# announcing the call, its arguments (a write_file call carries its whole
# payload here), and the framing on both messages.
TOOL_ROUND_OVERHEAD = 400
# The answer reserve, as a share of the context still free. See
# Session._answer_reserve: a share rather than a fixed block, floored so a
# nearly-full window still has room for a real answer and ceilinged by what
# the server will actually emit.
ANSWER_RESERVE_SHARE = 0.10
MIN_ANSWER_ROOM = 4096
# What is asked for when even the room clamp has run out. Past this point the
# window is full and the request will be refused whatever we send — which,
# with context shift off, is the loud failure we want rather than a silent
# truncation. Never 0: that reads as "no limit" to some servers.
ABSOLUTE_ANSWER_FLOOR = 256
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

    Re-exported rather than defined, so a message's stamp and the stamps on
    everything `session` writes to disk cannot come to disagree about the
    format. It lives there because `session` cannot import `core` back.
    """
    return store.now_stamp()


def api_messages(history):
    """History as the model sees it, display-only keys stripped.

    The `at` stamp is for the clients' clocks, not the prompt: it would ride
    in every request as invisible overhead, and a server that echoes unknown
    keys could surface it in the answer. `turn` and `round` are the same kind
    of thing — where the record sits in the session, drawn in the clients'
    gutters. `digest` marks a message as a turn summary rather than something
    the assistant said, which the clients draw differently and the model has
    no use for. Anything added here must be added to this filter too — the
    default is to send it.
    """
    drop = ("at", "digest", "turn", "round", "tokens")
    return [{k: v for k, v in m.items() if k not in drop} for m in history]


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


def stored_tool_args(tc):
    """One tool call's arguments as history has to carry them: a JSON object.

    The server re-validates every assistant message on every later request.
    SGLang answers one whose `arguments` is not JSON with a 400 — "Assistant
    tool call function.arguments must be valid JSON" — and one holding a valid
    JSON *scalar* with another, "must be a JSON object". Both were checked
    against the live server; `{}` is accepted.

    A rejected message is not a lost turn, it is every turn after it: the
    message rides in every prompt until the window rolls over, so the next 400
    arrives on the user's very next question and `settled()` then unwinds the
    whole unfinished turn around it. That is what happened at round 73 of
    session b9aec0f9 — 180 messages, one turn, gone.

    Two shapes reach here invalid. A round cut off mid-JSON is the one
    `truncated_tool_calls` exists for. The other is a call that streamed no
    argument deltas at all, leaving `arguments` as `""`: that call is
    deliberately *not* flagged, because a tool that takes no arguments is
    legitimately called with none and must still run. Both are still unfit to
    store, so history gets `{}` and the turn carries on.

    Valid JSON passes through byte for byte rather than being re-serialised:
    the journal, the token counts and the server's prefix cache all describe
    the string the model actually generated.
    """
    args = tc.get("arguments") or ""
    try:
        if isinstance(json.loads(args), dict):
            return args
    except (ValueError, TypeError):
        pass
    return "{}"


def _read_turn_blocks(path, turn_id):
    """One turn's journal blocks, in file order — or None when the segment is gone.

    Each block is (head, body): head is the header minus the `turn N` prefix
    (`user`, `round 3 tool_call exec call_1`, ...) and body the text under the
    header. Only a line in the strict header form ends a block, because tool
    output and reasoning are raw text that may itself contain `## ` lines, and
    those are body, not structure. The turn number matches exactly, so a
    search for turn 1 never touches turn 12's blocks.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    own = re.compile(rf"^## turn {turn_id} (.+?)  \[\d{{2}}:\d{{2}}:\d{{2}}\]$")
    other = re.compile(r"^## turn \d+ .+  \[\d{2}:\d{2}:\d{2}\]$")
    blocks = []
    head, body = None, []
    for line in raw.split("\n"):
        m = own.match(line)
        if m:
            if head is not None:
                blocks.append((head, "\n".join(body).strip("\n")))
            head, body = m.group(1), []
        elif other.match(line):
            if head is not None:
                blocks.append((head, "\n".join(body).strip("\n")))
            head, body = None, []
        elif head is not None:
            body.append(line)
    if head is not None:
        blocks.append((head, "\n".join(body).strip("\n")))
    return blocks or None


def _reconstruct_turn(region, turn_id, blocks):
    """A digested turn, rebuilt as the history messages the collapse removed.

    Returns (messages, reason): messages is None when the blocks do not line
    up with what a digested turn leaves behind, and reason is the words a
    /digest reply can carry. `region` is the turn as it stands after the
    collapse, digest removed: [user, ...steering, answer?].

    The user messages and, when there is one, the answer stay verbatim from
    the region — the journal only fixes their positions, and the region's
    copies are the ones with the original stamps. What is rebuilt is the
    working-out: one assistant message per round that called tools, carrying
    the round's text (if any) and its calls, followed by that round's
    results in issue order, not the completion order the journal recorded
    them in — the same invariant the pre-reserved result slots exist for.

    Text needs a rule, because a text-only round can be either an answer the
    region already keeps or an intermediate answer the collapse ate (a
    /steering that arrived as the answer finished reopens the turn, and the
    real answer comes later): a text-only round whose blocks are not the
    turn's last is appended, the last one is not. `fallback text` is the
    answer too — the region keeps it — so it is never appended. Reasoning
    blocks are skipped everywhere: reasoning never enters history, and the
    journal remains its record.
    """
    if not region or region[0].get("role") != "user":
        return None, "the region does not open on the turn's question"
    users = [m for m in region if m.get("role") == "user"]
    journal_users = [b for b in blocks if b[0] in ("user", "steering")]
    if len(journal_users) != len(users):
        return None, ("the journal's user blocks do not line up with "
                      "the history's — the segment may have stopped recording")

    messages = []
    ui = 0
    cur = None          # the round being collected
    cur_round = None
    last_round = 0
    answer = (region[-1] if region[-1].get("role") == "assistant"
              and not region[-1].get("tool_calls")
              and (region[-1].get("content") or "").strip() else None)

    def flush(append_text):
        """One collected round into messages. False when a call's result is missing.

        append_text: a round that made no calls is not rebuilt at all when it
        is the turn's answer — the region keeps that — but a text-only round
        that is *not* the turn's last is an intermediate answer the collapse
        removed, and only the journal has it, so it is rebuilt.
        """
        nonlocal cur, cur_round
        if cur is None:
            return True
        ok = True
        if cur["calls"]:
            messages.append({
                "role": "assistant",
                "content": "\n".join(cur["text"]) or "",
                "turn": turn_id, "round": cur_round, "at": now_stamp(),
                "tool_calls": [
                    {"id": cid, "type": "function",
                     "function": {"name": nm,
                                  "arguments":
                                      stored_tool_args({"arguments": args})}}
                    for cid, nm, args in cur["calls"]
                ],
            })
            for cid, nm, _args in cur["calls"]:
                if cid not in cur["results"]:
                    ok = False
                    break
                messages.append({"role": "tool", "tool_call_id": cid,
                                 "content": cur["results"][cid],
                                 "turn": turn_id, "round": cur_round,
                                 "at": now_stamp()})
        elif append_text and (cur["text"] or []):
            messages.append({"role": "assistant",
                             "content": "\n".join(cur["text"]),
                             "turn": turn_id, "round": cur_round,
                             "at": now_stamp()})
        cur, cur_round = None, None
        return ok

    for head, body in blocks:
        if head in ("user", "steering"):
            # drain_steering runs at the top of the next loop iteration, so the
            # journal's steering block sits after the round it followed. Flush
            # that round first, or its assistant and tool messages would land
            # after the steering and the history would not be the one the model
            # saw. A round flushed here is not the turn's last, so a text-only
            # one is an intermediate answer and is kept.
            if not flush(True):
                return None, "a tool call has no recorded result in this segment"
            messages.append(users[ui])
            ui += 1
            continue
        # The phase is round, resume or closing — the journal tags them the
        # way the model saw them, and the round number runs on across all
        # three, which is the ordering below.
        # tool_call and tool_result carry a tail (name, id, …); reasoning and
        # text do not, so it is optional — a regex that made it mandatory
        # dropped every text-only round, which is exactly the intermediate
        # answer this function exists to recover.
        m = re.match(r"^(?:round|resume|closing) (\d+) "
                     r"(reasoning|text|tool_call|tool_result)(?: (.+))?$", head)
        if m:
            rnd = int(m.group(1))
            if cur is None:
                if rnd <= last_round:
                    return None, "the segment's rounds are out of order"
                cur, cur_round = {"text": [], "calls": [], "results": {}}, rnd
                last_round = rnd
            kind, rest = m.group(2), m.group(3)
            if kind == "reasoning":
                continue
            if kind == "text":
                cur["text"].append(body)
            elif kind == "tool_call":
                nm, cid = rest.split(" ", 1)
                cur["calls"].append((cid, nm, body))
            else:  # tool_result: "name id N chars"
                # Off the end, where the count and its unit are fixed words;
                # the id is the second-to-last token, the name everything
                # before it (it cannot contain spaces — it is a registry key).
                nm, cid = rest.rsplit(" ", 3)[:-2]
                cur["results"][cid] = body
            continue
        if head == "partial (steering)":
            # The interrupted round itself has no phase blocks — the jot sits
            # after a model round returns, and this one never did — so its
            # number is the last completed round plus one. It is never the
            # turn's answer: the steering and the re-planning that followed it
            # still have to produce one, and a turn that errored instead was
            # never digested at all.
            if not flush(True):
                return None, "a tool call has no recorded result in this segment"
            messages.append({"role": "assistant", "content": body,
                             "turn": turn_id, "round": last_round + 1,
                             "at": now_stamp()})
            continue
        if head == "fallback text":
            # The one answer that never goes through call_round; it is the
            # turn's answer, so the region keeps it and this block is the
            # flush, not an append.
            if not flush(True):
                return None, "a tool call has no recorded result in this segment"
            continue
        # Anything else is a block of this turn that history never held
        # (interrupted reasoning: the journal keeps it, and only the journal).
    if not flush(False):
        return None, "a tool call has no recorded result in this segment"
    if answer is not None:
        messages.append(answer)
    return messages, ""


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

        # Every conversation scalar, read through the one table that owns the
        # key, the default and the coercion — see CONV_SCALARS. /reload reads
        # the same table, which is what keeps the two paths from drifting.
        for sc in CONV_SCALARS:
            setattr(self, sc.attr, sc.read(conv, f"session {name}"))
        # Collapse each finished turn's tool traffic to a digest, leaving the
        # question and the answer verbatim. Off by default: it is the first
        # thing here that discards context on purpose, and it is only safe
        # while the journal is keeping up — see _digest_turn.
        #
        # Resolved rather than read, because a model entry may override the
        # conversation default. This early call always lands on the default:
        # model_name is still None here and use_model() sets it, which is why
        # use_model() asks again once it has.
        #
        # digest_override is the session's own answer to the same question,
        # made by a person with `/digest off|collapse` rather than by a config
        # file. None until it is made; it then outranks both config layers for
        # the life of the session, and persist() carries it across a restart.
        self.digest_override = None
        self.digest_turns = self._effective_digest_turns()
        self.declined_at = None
        self.warned_over = False
        # Said once per window when the pre-turn gate declines to act on a
        # window with no thinking room left. See _headroom_check.
        self._no_room_warned = False
        # Whether this session has already said what the server probe found.
        # Once is the point: the findings are about the engine, not the turn,
        # and repeating them every turn is how a warning becomes wallpaper.
        self._server_warned = False
        # Everything this session has ever asked the model to generate. An
        # odometer, not a gauge: /clear empties the conversation but does not
        # rewind what it cost, and it carries across a restart with the session.
        self.total_output = 0
        # And everything it has ever sent. The counterpart nobody had, and the
        # larger number by far: `used` is the size of the window *once*, while
        # a turn re-sends the whole of it on every round, so a three-round turn
        # on a 10k window bills ~30k of input and the gauge still reads 10k.
        # Cost is quadratic in the length of a conversation and until now it
        # was reported nowhere — which also made the cost of a rollover firing
        # too early invisible, the very thing the estimator audit was about.
        #
        # Measured rounds only. A round whose server withheld usage adds
        # nothing rather than an estimate, and input_exact goes false so the
        # figure is labelled as the floor it is.
        self.total_input = 0
        self.input_exact = True
        # Of that input, what the server served from its KV cache. The one
        # direct diagnostic for prefix-cache invalidation: a hit rate that
        # falls off a cliff means something rewrote the prompt prefix and
        # every session on that server is re-reading its whole history.
        #
        # Its own denominator, not total_input, because the two have different
        # domains: only rounds that came with a cache figure belong in a cache
        # hit rate. Dividing by total_input would read 0% on a server that
        # never reports one, which is the alarming end of a number that
        # actually means nothing.
        self.total_cached = 0
        self.cached_input = 0
        # Latched on the first round that reports a figure. SGLang sends
        # prompt_tokens_details: null on a cold prefix and a real number once
        # warm, so a bare None is ambiguous — unreported, or reported zero? It
        # is read as zero only after this engine has proved it reports at all,
        # which keeps cold rounds in the denominator where they belong without
        # inventing hits for a server that is simply silent. Reset by
        # use_model: the next engine is a different server with its own habits.
        self._cache_reported = False
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
        # Which model round inside the current turn: 1 for the first, bumped by
        # every round the tool loop, a thinking-pad resume or the conclusion
        # adds. 0 between turns and before the first round, which is where the
        # user's own question sits — so a prompt is "turn 47" and the work
        # under it is "turn 47, round 3". Not persisted: a restart kills any
        # turn in flight, so there is no round to come back to.
        self.round_id = 0
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

        # journal_on — the append-only record of what this session generates,
        # reasoning included, tool output untruncated — is set with the rest of
        # the conversation scalars above. Opened on the first thing worth
        # recording and closed by a rollover; see store.Journal.
        self._journal = None

    # ---- events ----------------------------------------------------------

    def mark(self):
        """Where a record sits in the session: which turn, which round.

        The same pair the journal tags its blocks with (`## turn 47 round 3`),
        so a line a client draws in its gutter is greppable in the record.
        Display-only, like the `at` beside it — api_messages() drops all three.
        """
        return {"turn": self.turn_id, "round": self.round_id}

    def emit(self, event):
        """Fan out one event. Synchronous and non-blocking, by contract."""
        self.last_seq = next(self._seq)
        event["seq"] = self.last_seq
        event["session"] = self.name
        # Where in the session this happened, stamped here so every event
        # carries it by construction rather than at each of the thirty-odd
        # call sites. setdefault, not assignment, so a call site that knows
        # better than "whatever is in flight" still wins. turn_start and
        # turn_end also carry turn_id: the older key, same number, kept
        # because it is part of the wire contract clients were written to.
        event.setdefault("turn", self.turn_id)
        event.setdefault("round", self.round_id)
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
        # Digest messages written before tokens were stored on the message get
        # it back from the content, so every replayed marker carries the full
        # wording without a data migration.
        messages = [{**m, "tokens": m.get("tokens") or count_text(m.get("content") or "")}
                    if m.get("digest") else m
                    for m in self.history if m.get("role") != "system"]
        # Built directly rather than via emit(), so it must stamp these itself.
        return ev("hello",
                  session=self.name,
                  seq=self.last_seq,
                  proto=PROTOCOL_VERSION,
                  version=__version__,
                  model=self.model_name,
                  budget=self.budget,
                  busy=self.busy,
                  theme=configlib.config_theme(self.config),
                  pin=configlib.config_pin(self.config),
                  messages=messages,
                  rollover={"mode": self.rollover_mode, "percent": self.rollover_pct,
                            "count": self.rollover_count},
                  missed=missed,
                  gap=bool(last_seq and missed and missed[0]["seq"] > last_seq + 1))

    # ---- state -----------------------------------------------------------

    @property
    def budget(self):
        return getattr(self.engine, "context_length", configlib.DEFAULT_CONTEXT_LENGTH)

    @property
    def trip(self):
        """Used-token count at which this window rolls over. 0 means never.

        One derived fact, and it used to be spelled out at four sites under
        three different guard policies — one guarding both operands, two
        guarding only rollover_pct, one guarding neither. All three agree:
        a falsy budget makes the bare expression `int(0 * pct / 100)`, which
        is 0, the same answer the guarded forms give. So the guard belongs
        here once, and the callers ask.
        """
        return (int(self.budget * self.rollover_pct / 100)
                if self.budget and self.rollover_pct else 0)

    @property
    def busy(self):
        """Is a turn in flight?

        The lock alone is not enough: start_turn creates a task that does not
        reach `async with self.lock` until the loop next runs it, and two
        submits arriving in one batch would both pass a lock-only test.
        """
        return self.lock.locked() or bool(self._turn_task
                                          and not self._turn_task.done())

    def _min_tool_round_room(self):
        """Free context below which another tool round is not worth starting.

        The constant this replaces hard-coded tools.py's 8,000-char default
        and read 4000. `tools.max_output_chars` is configuration: set it to
        32,000 and the old figure was 4x short, admitting rounds that could
        not finish and landing the overflow in the history anyway.

        Two results, not one: `used_now()` at the check is already a round
        stale — the assistant message with the tool_calls and the result it
        returned are both in the history and neither is in the measurement —
        and the round being admitted has to fit as well.
        """
        from tools import DEFAULT_MAX_OUTPUT     # local, as build_tools is
        chars = configlib.int_or(
            (self.config.get("tools") or {}).get("max_output_chars",
                                                 DEFAULT_MAX_OUTPUT),
            DEFAULT_MAX_OUTPUT)
        # chars // 4 rather than a real count: there is no text to count yet.
        # The rounding is deliberate on the safe side — tool output is paths,
        # JSON and log lines, which tokenize *worse* than 4 chars each.
        return max(MIN_TOOL_ROUND_ROOM, 2 * (chars // 4) + TOOL_ROUND_OVERHEAD)

    def schema_tok(self):
        """What the tool block costs in this session's prompts.

        Measured where the server reports usage: the engine solves for it from
        a real prompt_tokens, and that answer is true for whatever chat
        template the server actually runs. Otherwise the Qwen-calibrated
        formula — which is the case the estimate exists for, and also the case
        where it is least certain, so the startup warning says which template
        it was fitted on.
        """
        observed = getattr(self.engine, "schema_tok", None) if self.engine else None
        return observed if observed is not None else schema_cost(self.schemas)

    def estimate(self, messages=None, tools=True):
        """Context size in server tokens, for rounds no measurement covers.

        Two terms, because what we hold locally is wrong in two unrelated
        ways: cl100k disagrees with the server's encoding by a *ratio* on
        prose, and the tool schemas cost a *fixed* block more than their JSON
        does. Correcting both with one multiplier — which is what this did for
        a long time, at 1.6 — cannot work: the result reads 34% high on an
        empty window and 52% high on a full one, because a scale applied to a
        constant error grows with everything around it. See docs/tokens.md.

        `tools=False` for a request sent without a `tools=` argument. The
        digest is the one such caller, and its whole prompt is smaller than
        the schema block it would otherwise be charged for.
        """
        msgs = self.history if messages is None else messages
        prose = corrected_messages(msgs)
        return prose + self.schema_tok() if tools else prose

    def used_now(self):
        """How full the window is: measured where the server told us, else estimated.

        A measurement is still preferred over an accurate estimate. The
        estimate is calibrated, not exact — it is blind to anything the
        template does that we have not fitted, and on a server that withholds
        usage there is nothing to check it against.

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
        return _SCALAR_BY_ATTR["rollover_pct"].read(
            self.config.get("conversation", {}), f"session {self.name}")

    def _configured_digest_turns(self):
        """Whether this session digests its turns: the model may override.

        The only conversation setting a model entry can speak for, and it
        earns the exception because a digest is paid for by whichever model
        the session is on. On a window large enough that nothing will ever be
        collapsed, every turn would still spend a whole extra round
        summarising for no benefit — so the model that owns the window is the
        right place to say no.

        `is None`, not a falsy test: `digest_turns: false` in a model entry
        has to mean "off for this one" rather than "not specified", which is
        the whole point of having the key.

        Read back from the live config like the rollover threshold above,
        rather than remembered, so a reload and a `/model` switch both land
        without either having to know about the other.
        """
        entry = (self.config.get("models", {}) or {}).get(self.model_name) or {}
        override = entry.get("digest_turns")
        if override is not None:
            return bool(override)
        return bool((self.config.get("conversation") or {}).get("digest_turns", False))

    def _effective_digest_turns(self):
        """The session's actual mode: a `/digest` choice, else the config.

        The override outranks both config layers, which is why the three
        sites that resolve this (init, the reload path, use_model) all ask
        here rather than _configured_digest_turns() directly: a `/clear`, a
        `/model` switch and a reload all re-ask, and the choice has to ride
        through all three. A reload with the override set keeps the override
        and does not re-resolve the config under it — that would quietly
        overrule a choice the person made in this session, and the config
        change is still one `/digest` away from being read.
        """
        if self.digest_override is not None:
            return self.digest_override
        return self._configured_digest_turns()

    def _digest_state(self):
        """The digest answer as /info and bare /digest both return it.

        One shape, because the WebUI button paints from the /info it fetches
        on attach and from the reply to its own clicks alike — two shapes
        would have to agree with each other forever, and would not.
        """
        return {"on": self.digest_turns,
                "set_by": ("you" if self.digest_override is not None
                           else "config"),
                "expandable": sum(1 for m in self.history if m.get("digest"))}

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

    def _rebuild_system_message(self):
        """Replace history[0] from the behavior layers, keeping everything else.

        carryover() is read before the old system message goes and handed
        back in: switching models after a rollover used to drop the handoff
        on the floor.

        An empty history is not a special case. `body` is then `[]` and
        `fresh_history(carried) + []` is `fresh_history(carried)` — which is
        what use_model's `if self.history:` branch computed the long way
        round, both arms arriving at the same list.
        """
        carried = self.carryover()
        body = [m for m in self.history if m["role"] != "system"]
        self.history = self.fresh_history(carried) + body

    async def reload_session_behavior(self):
        """Re-read models/sessions/<name>.md and rebuild the system message.

        Called when the name changes, since that is what picks the file.
        """
        entry = (self.config.get("models", {}) or {}).get(self.model_name) or {}
        _, _, self.session_behavior = await self.manager.behavior_for(
            entry, self.name)
        self._rebuild_system_message()

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
            # The window just shrank, so the measured prompt size describes a
            # history that no longer exists — the same reset roll_over, the
            # max_history trim and _digest_turn all do after reshaping. It
            # matters here because tally() now publishes a measurement mid-turn:
            # a turn that died after its tool calls would otherwise leave the
            # next headroom check reading a window several thousand tokens
            # fuller than the one it just rolled back to, and roll over to
            # relieve pressure this call had already relieved.
            self._last_used = None

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
        # Compared before the assignment, and against the schemas rather than
        # their token count: two different tool sets can coincidentally cost
        # the same, and the question here is whether the block changed at all.
        schemas_changed = self.schemas != blob["schemas"]
        self.tools = blob["tools"]
        self.schemas = blob["schemas"]
        self.tools_tok = blob["tools_tok"]
        # A reload that adds or drops a tool changes the schema block every
        # request carries, so a measurement taken before it describes a prompt
        # with the old block. The history is untouched here, which is why this
        # is the one invalidation that is not about history: the *other* half
        # of the prompt moved. Gated, because most reloads change neither, and
        # throwing the measurement away would put the next turn's headroom
        # check back on the estimate for nothing.
        if schemas_changed:
            self._last_used = None

        for attr, was, now in blob["scalars"]:
            if getattr(self, attr) == was:
                setattr(self, attr, now)

        # Recomputed outright rather than diffed like the scalars above, and
        # the divergence rule does not apply to the config half: nothing
        # changes the configured value for a single session at runtime, and
        # it is derived from two config layers — the model entry over the
        # conversation default — so re-resolving is the only way to pick up a
        # change in either. Through _effective_digest_turns(), because a
        # /digest choice outranks both layers and has to survive the reload
        # that is this very path. Turning it on affects the next turn to
        # finish; turns already collapsed stay collapsed, which is what the
        # journal is for.
        self.digest_turns = self._effective_digest_turns()

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

    def journal_ok(self):
        """Whether the journal is actually recording this session right now.

        The gate for anything that drops context on the grounds that the
        journal holds it. Three ways the answer is no, and only the first is
        obvious: journalling is switched off, no segment has been opened yet,
        or a write failed earlier and closed the segment early. `jot` reports
        none of these — it never raises, which is right for a redundant second
        copy and dangerous for a sole one.
        """
        return bool(self.journal_on and self._journal is not None
                    and self._journal.healthy)

    def journal_blocks(self):
        """How many blocks have reached this segment. 0 when there is none."""
        return self._journal.blocks if self._journal else 0

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
            "saved_at": now_stamp(),
            # Restored so a client reconnecting with an old last_seq is told it
            # missed something, instead of being silently handed a numbering
            # that restarted at 1 underneath it.
            "last_seq": self.last_seq,
            "turn_id": self.turn_id,
            # Runtime-mutated policy. rollover_pct in particular is zeroed when
            # even a fresh window trips the threshold; re-reading it from config
            # would put the session straight back into that loop.
            "rollover_pct": self.rollover_pct,
            # The /digest choice: None while the session follows the config,
            # True/False once a person has spoken. Additive like the odometers
            # below — a file written before the key existed reads back as
            # None, which is exactly "never chose".
            "digest": self.digest_override,
            "declined_at": self.declined_at,
            "warned_over": self.warned_over,
            "last_used": self._last_used,
            "total_output": self.total_output,
            # Added without touching LIVE_SCHEMA. A bump makes load_live
            # discard every existing conversation, and these are odometers: a
            # restored session that predates them starts its input count at
            # zero and under-reports its own history, which is a far smaller
            # harm than throwing the history away to avoid it.
            "total_input": self.total_input,
            "input_exact": self.input_exact,
            "total_cached": self.total_cached,
            "cached_input": self.cached_input,
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
        self.total_input = configlib.int_or(payload.get("total_input") or 0, 0)
        # Defaults true for a file written before the key existed, matching a
        # fresh session. Such a file also has total_input 0, so the claim it
        # makes is "nothing counted, nothing missed" — which is accurate.
        self.input_exact = bool(payload.get("input_exact", True))
        self.total_cached = configlib.int_or(payload.get("total_cached") or 0, 0)
        self.cached_input = configlib.int_or(payload.get("cached_input") or 0, 0)
        self.rollover_count = configlib.int_or(payload.get("rollover_count") or 0, 0)
        # The /digest choice. isinstance, not a truthy test: 1 and True are
        # equal in Python, and a torn write or hand edit that leaves a 1 in
        # the file is not a person who chose collapse.
        digest = payload.get("digest")
        self.digest_override = digest if isinstance(digest, bool) else None
        # use_model already ran before load_state on the restore path and
        # resolved this from the config; the override, if the file carried
        # one, outranks that resolution.
        self.digest_turns = self._effective_digest_turns()
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
            self.round_id = 0      # the question is under no round yet
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
                # Nothing after this belongs to a round — a rollover, a trim or
                # a reload applied below would otherwise be stamped with
                # whatever the last round happened to be.
                self.round_id = 0
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
        await self._warn_about_server()
        # Before the user message lands, so a roll starts the fresh session with
        # this turn's question in it rather than one turn late.
        await self._headroom_check()
        # Held by identity, not by index: _digest_turn needs to find where this
        # turn begins after the tool loop and repair_history have both reshaped
        # the list around it.
        user_msg = {"role": "user", "content": text,
                    "at": getattr(self, "_turn_at", None) or now_stamp(),
                    **self.mark()}
        self.history.append(user_msg)
        # The journal's high-water mark before this turn wrote anything, so the
        # digest can tell "recorded" from "the write failed silently".
        blocks_before = self.journal_blocks()
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
        # /steering's already moved into the history. Kept here because a
        # model-server retry unwinds the turn and would take them with it: a
        # correction is the user's word and must ride on every re-run, so this
        # is not cleared.
        steering_drained = []
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
        # Input accounting, and nothing like the output kind above. There is no
        # local estimate to fall back on and no attempt at one: the prompt a
        # round actually sent is not reconstructable here — a resumed round
        # carries a thinking pad that never reaches history, and the schema
        # block is the server's rendering, not ours. So this sums what was
        # reported and says so, rather than filling the gaps with a guess.
        #
        # Every round counts, resumed ones included. last_prompt deliberately
        # ignores those because they describe a window that will not exist by
        # the end of the turn — but they were sent, and they were billed.
        in_tok = in_reported = 0
        cache_tok = cache_base = 0
        # The server's own count of the reasoning share, kept beside the local
        # tiktoken count rather than replacing it. The local one is of the text
        # that arrived and updates live during the stream; this one is of what
        # the server generated, which on a truncated round is more. Carried so
        # the two can be compared at all — measured against SGLang, a round
        # reporting completion_tokens 5 also reported reasoning_tokens 10, and
        # a usage block that contradicts itself is worth being able to see.
        srv_reason = None

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
            nonlocal in_tok, in_reported, cache_tok, cache_base, srv_reason
            nonlocal reason_tok   # re-synced below, on the round boundary
            rounds += 1
            # Mirrored onto the session so emit() and the history appends can
            # reach it; `rounds` itself is a local the accounting below owns.
            self.round_id = rounds
            # Before the history_shaped gate below, deliberately: what this
            # round cost does not depend on whether its prompt resembles the
            # window afterwards.
            if result.prompt_tokens is not None:
                in_reported += 1
                in_tok += result.prompt_tokens
                # A round belongs in the cache rate only once this engine has
                # shown it reports one — see _cache_reported. After that a
                # None is a cold prefix (SGLang's spelling of zero) and the
                # round counts against the rate rather than vanishing from it.
                if result.cached_tokens is not None:
                    self._cache_reported = True
                if self._cache_reported:
                    cache_tok += result.cached_tokens or 0
                    cache_base += result.prompt_tokens
            if history_shaped and result.prompt_tokens is not None:
                last_prompt = result.prompt_tokens
                # Publish it immediately, not only at turn end. history_shaped
                # means the prompt just sent *was* the current history, so this
                # is a measurement of the window as it stands. The pad wall
                # reads used_now() on every round and a tool result can land
                # between two of them; without this it spends the whole turn
                # reading the previous turn's figure and allows a pad sized for
                # a window that no longer exists. The comment on answer_cap in
                # _think_pad already assumed this was happening.
                self._last_used = last_prompt
            if result.completion_tokens is not None:
                out_reported += 1
                out_measured += result.completion_tokens
            if result.reasoning_tokens is not None:
                # Stays None until something is reported, so "no server said"
                # and "the server said zero" remain distinguishable.
                srv_reason = (srv_reason or 0) + result.reasoning_tokens
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

            In the order the model issued them, with one exception: a run of
            consecutive read-only calls (`tools.PARALLEL_SAFE`) runs together,
            so three two-second reads take the two of the slowest. A write and
            an exec always run alone, in their issued position — a read after
            the write of the same path, or the second of two writes of it,
            keeps the order the model meant, and a read never reaches across
            the command that may have made the file it reads. The result slots
            were reserved in issue order before anything ran, so the history
            stays in issue order whatever the completion order is; the
            tool_result events, by contrast, land in completion order.
            """
            # A call cut off mid-JSON is never executed: `rm -rf /tmp/scratch`
            # truncated to `rm -rf /` is the shape of accident that stops. What
            # goes into history is a separate question and `stored_tool_args`
            # answers it for every call, not just the cut-off ones — an empty
            # `arguments` runs, and must still not be stored as it came.
            cut = {id(tc) for tc in truncated_tool_calls(result)}
            self.history.append({
                "role": "assistant",
                "content": result.text or "",
                "at": now_stamp(),
                **self.mark(),
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"],
                                  "arguments": stored_tool_args(tc)}}
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
                       "content": "error: cancelled", "at": now_stamp(),
                       **self.mark()}
                slots[tc["id"]] = msg
                self.history.append(msg)

            tag = f"turn {self.turn_id} round {rounds}"
            from tools import PARALLEL_SAFE   # local, as build_tools is

            async def execute(tc):
                """One call end to end: announce it, run it, land its result.

                May run inside a gather, so nothing here may assume it began
                or ended before any other call: it fills the one slot reserved
                for it up above, and the reservation is what keeps the history
                in issue order.
                """
                nonlocal tools_used
                # Emitted BEFORE the call: a 120-second exec used to be silent.
                self.emit(ev("tool_call", id=tc["id"], name=tc["name"],
                             arguments=tc["arguments"], at=now_stamp()))
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

            calls = result.tool_calls
            i = 0
            while i < len(calls):
                if calls[i]["name"] not in PARALLEL_SAFE:
                    await execute(calls[i])
                    i += 1
                    continue
                j = i + 1
                while j < len(calls) and calls[j]["name"] in PARALLEL_SAFE:
                    j += 1
                group = calls[i:j]
                if len(group) > 1:
                    # A /stop or the turn wall here cancels the gather, which
                    # cancels every call still running, and the reserved slots
                    # keep the history well-formed whatever did land.
                    await asyncio.gather(*(execute(tc) for tc in group))
                else:
                    # One read alone is a run of one: the direct await is the
                    # same behaviour without the gather frame.
                    await execute(group[0])
                i = j

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
                steering_drained.append(text)
                at = now_stamp()
                self.history.append({"role": "user", "content": text, "at": at,
                                     **self.mark()})
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
                                     "at": now_stamp(), **self.mark()})
                await self.jot(f"turn {self.turn_id} partial (steering)", partial)

        attempt = 0
        while True:
            try:
                # Unbounded in rounds, walled by room — the same rule as _think_pad,
                # for the same reason: a count is not a limit the machine has. This
                # loop used to stop at tools.max_iterations, which knew nothing
                # about the window it was filling and so was either in the way of
                # honest work or (set high) not a limit at all. What actually runs
                # out is context: every round re-sends a history one tool result
                # longer, nothing mid-turn can roll over, and with context shift off
                # the overflow is a 400 that discards the whole turn.
                while True:
                    # _pad_headroom and not a wall of its own, on purpose: it is
                    # already "what is free once the answer is paid for", which is
                    # exactly what a tool round has to fit inside, and a third name
                    # for one number is how the gate and the wall drifted apart
                    # before (see docs/reasoning.md, invariant 1).
                    if self._pad_headroom() < self._min_tool_round_room():
                        self.emit(ev("notice", level="warn",
                                     text="no room left for another tool round — "
                                          "asking for a final answer"))
                        result = await call_round()
                        if was_cut_off(result):
                            # No room for another tool round, so this conclusion
                            # must be words.
                            result = await self._think_pad(call_round, turn_t0,
                                                           result.reasoning,
                                                           allow_tools=False)
                        full_response = result.text
                        break
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
                                     "at": now_stamp(), **self.mark()})
                            await drain_steering()
                            continue
                        full_response = result.text
                        break

                    await run_tools(result)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if isinstance(e, openai.APIError) and attempt < self.turn_retries:
                    # A model-server error — a 400/429/500, a timeout, a dropped
                    # connection — is not a bug in the turn: what is in history is
                    # still sound and the question is still the question, so unwind
                    # whatever the failed round left half-written and run the turn
                    # again. Its first round re-sends the same history the model
                    # already saw, so this resumes the work rather than re-asking
                    # the question. Non-API errors are not retried: a KeyError is
                    # the same failure on every attempt.
                    attempt += 1
                    # The client has already seen the dead round's output and the
                    # journal is the only durable copy of it. record_partial never
                    # ran — the round died instead of being interrupted — so keep
                    # what was emitted before the unwind takes it.
                    if reason_emitted:
                        await self.jot(f"turn {self.turn_id} interrupted reasoning",
                                       "".join(reason_emitted))
                    partial = "".join(emitted)
                    if partial.strip():
                        await self.jot(f"turn {self.turn_id} partial (retry)",
                                       partial)
                    self.repair_history()
                    # settled() unwinds the whole unfinished turn — the question
                    # included, that is what makes it unfinished — so hand the
                    # same dict back: _digest_turn finds this turn's start by
                    # identity, and the retried rounds must see the question.
                    if self.history[-1] is not user_msg:
                        self.history.append(user_msg)
                    for text in steering_drained:
                        # Unwound as well, being user messages: a retry may not
                        # silently discard a correction, so it rides on the
                        # re-run. Re-appended without a new steering event (the
                        # clients already saw it) and without a new journal
                        # block, which the drain already wrote.
                        self.history.append({"role": "user", "content": text,
                                             "at": now_stamp(), **self.mark()})
                    self.emit(ev("notice", level="warn",
                                 text=f"model server error ({type(e).__name__}) — "
                                      f"retrying the turn ({attempt}/{self.turn_retries})"))
                    continue
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
                break
            break

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
        # Same placement, same reason — and the odometer is a floor, not an
        # estimate: input_exact latches false the moment any round in the
        # session's life went unreported, and never recovers. A total that was
        # once incomplete stays labelled incomplete, because there is no later
        # round that could fill in what the earlier one never said.
        self.total_input += in_tok
        if rounds > 0 and in_reported != rounds:
            self.input_exact = False
        self.total_cached += cache_tok
        self.cached_input += cache_base

        if not had_error and full_response:
            self.history.append({"role": "assistant", "content": full_response,
                                 "at": now_stamp(), **self.mark()})
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
                         # The server's figure for the same quantity, or None
                         # where it offers none. Beside the local count, not
                         # instead of it: see srv_reason.
                         reasoning_server=srv_reason,
                         tool_calls=tools_used,
                         # Everything generated this turn, across every round:
                         # the answer, the reasoning, and the tool-call arguments.
                         # llm_tokens above is the final answer alone, which
                         # is the smaller and more useful number to read; this is
                         # the one that was actually paid for.
                         output_tokens=out_tok,
                         output_measured=out_exact,
                         output_total=self.total_output,
                         # Sent, not held: what the whole turn cost in input
                         # across every round, and the session odometer behind
                         # it. Deliberately not rendered on the ⚡ line — the
                         # per-turn figure is a number you can do nothing
                         # about, where the total is what a long conversation
                         # is actually spending. /info reads the totals.
                         input_tokens=in_tok,
                         input_measured=rounds > 0 and in_reported == rounds,
                         input_total=self.total_input,
                         cached_tokens=cache_tok,
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
                                  "raise max_tokens and try again, or turn "
                                  "reasoning on so the round is resumed "
                                  "instead of lost"))
            else:
                self.emit(ev("notice", level="info",
                             text=f"no answer — the model likely spent its budget on "
                                  f"reasoning ({'' if out_exact else '~'}{out_tok} output tok)"))

        # Rollover is evaluated only here: the tool loop has fully drained, so no
        # tool_call/tool_result pair can be split. A thinking pad never reaches
        # history — only the answer does — so a long think cannot drag the
        # rollover trip point forward. Keep it that way.
        if not had_error:
            # Before _after_turn, not after: _after_turn is where rollover is
            # evaluated, so digesting afterwards would roll the un-collapsed
            # window away and leave this doing nothing at all.
            await self._digest_turn(user_msg, "".join(reasoning_parts), blocks_before)
            await self._after_turn()

    def _answer_reserve(self):
        """Context tokens held back so the closing round can still be written.

        A share of what is actually free, not a fixed block. The fixed block
        was `max_output_tokens`, and on a 100k window with a 32,768 cap that
        reserved a third of the context permanently: the pre-turn gate fired
        at 61% while the UI drew its first warning at 80% and the rollover
        tick at 90%, so the rollover that actually happened happened 29 points
        before anything said it would. That is the "it rolled over at 60%"
        report this replaces.

        `max_output_tokens` keeps a job here, but a different one. It stops
        being the reservation and becomes the ceiling — a real property of the
        server rather than a policy knob. On a 1M window 10% of free is
        ~100k tokens, and a model that emits at most 8,192 will not write
        them: you cannot reserve room for an answer the server will not
        produce.
        """
        free = max(0, self.budget - self.used_now())
        ceiling = self.engine.max_output or (
            self.engine.extra_params.get("max_tokens") or 0)
        want = max(int(ANSWER_RESERVE_SHARE * free), MIN_ANSWER_ROOM)
        # The ceiling wins even against the floor: a server capped below
        # MIN_ANSWER_ROOM cannot be talked into writing more by reserving it.
        if ceiling:
            want = min(want, ceiling)
        # And the room wins over both. On a window smaller than the floor's
        # 4096 — qwen36's 12k once it is two-thirds full — reserving the floor
        # reserves space that is not there, and the closing round then asks
        # for a prompt plus an answer that together overflow. Capped here, the
        # pre-turn gate closes instead and the session rolls over.
        return max(ABSOLUTE_ANSWER_FLOOR,
                   min(want, free - PAD_CONTEXT_MARGIN))

    def _pad_headroom(self):
        """Context tokens a thinking pad may still grow into.

        The pre-turn gate is not independent policy — it is a *prediction of
        what the pad wall will do*, and both read this. They were once allowed
        to differ, which opened a band where the gate waved a turn through,
        the wall then allowed zero thinking rounds, and the closing round
        asked for more than the window held: a 400 that discarded the turn.
        Any change here has to keep them reading the same number.
        """
        return (self.budget - self.used_now() - self._answer_reserve()
                - PAD_CONTEXT_MARGIN)

    async def _server_caps(self):
        """LLMEngine.capabilities(), or None — never an exception.

        Its two callers are a diagnostic and a notice. Neither is worth
        failing a turn or a /info over, and an engine that cannot be probed
        (a stub, a server that is down) is exactly the case where someone is
        most likely to be reading /info.
        """
        if not self.engine:
            return None
        try:
            return await self.engine.capabilities()
        except Exception as e:
            logger.debug(f"capability probe failed: {e}")
            return None

    async def _warn_about_server(self):
        """Say once, out loud, where the server disagrees with the config.

        A misconfigured `--reasoning-format` or a `context_length` larger than
        the window the server was started with are both silent: the session
        keeps working and keeps being wrong, and the symptom surfaces turns
        later as a context figure nobody can explain. The probe is cached on
        the engine, so this costs one round per core per model, once.

        Never fatal, and never blocking: a server that cannot answer the probe
        is a server that may still answer the turn.
        """
        if self._server_warned or not self.engine:
            return
        self._server_warned = True
        caps = await self._server_caps()
        for w in (caps or {}).get("warnings", []):
            self.emit(ev("notice", level="warn", text=w))

    async def _headroom_check(self):
        """Roll over *before* a turn when there is no room left to think in.

        A session sitting just under the rollover threshold has a full window
        and no headroom, so the pad's context wall would trip on the first round —
        thinking would be off exactly where a hard question needs it. Rolling
        first gives the turn a fresh window. Only in auto mode: `ask` must not
        put a question before the turn has even started, and `off` means off.

        When it declines for one of those reasons it still *says* so, once per
        window. Every early return below is a turn that will start with no room
        to think in, and the symptom — a hard question answered as if thinking
        were switched off — looks nothing like its cause. A session can reach
        the `rollover_pct == 0` case on its own, via the auto-disable in
        _after_turn, so this is not only a misconfiguration warning.
        """
        if not (self.engine and self.engine.reasoning and self.budget):
            return
        if self.rollover_mode != "auto" or not self.rollover_before_think:
            return self._warn_no_thinking_room()
        # rollover_at_percent: 0 means never, and _after_turn already honours it.
        # This path has to as well, or a session that switched rollover off — by
        # config, or by the auto-disable when even a fresh window trips the
        # threshold — would still be rolled over from here, which is both a
        # surprise and, in the auto-disable case, the loop that guard prevents.
        if not self.rollover_pct:
            return self._warn_no_thinking_room()
        # Same guard as _maybe_roll, for the same reason: this fires before
        # every turn, so an unarchivable window would error on every one.
        if self.rollover_blocked:
            return self._warn_no_thinking_room()
        if self._pad_headroom() >= MIN_THINK_HEADROOM:
            return
        used = self.used_now()
        self.emit(ev("notice", level="info",
                     text=f"only {max(0, self._pad_headroom())} tok of thinking room "
                          f"left — rolling over before the turn"))
        await self.roll_over("no thinking headroom", used, self.trip)

    def _warn_no_thinking_room(self):
        """Say once that this window has no room left to think in.

        Policy is unchanged — `ask` still must not interrupt before a turn and
        `off` still means off. This only ends the silence, which was the part
        that made a full window indistinguishable from a model that had stopped
        reasoning.

        Two different conditions arrive here and they need different words. A
        session with rollover switched off is doing as it was told, and
        `rollover_mode: auto` is the answer. A session whose rollover *failed*
        is not: it tried, the archive could not be written, and this is the
        only message that person gets afterwards — `_maybe_roll` returns False
        in silence from then on. Sending them to turn on a setting that is
        already on, while the reason sits unread in `rollover_blocked`, was
        telling someone the wrong thing at the one moment they were listening.
        """
        if self._no_room_warned:
            return
        room = self._pad_headroom()
        if room >= MIN_THINK_HEADROOM:
            return
        self._no_room_warned = True
        head = f"only {max(0, room)} tok of thinking room left"
        if self.rollover_blocked:
            self.emit(ev("notice", level="warn",
                         text=f"{head} and this window could not be archived "
                              f"({self.rollover_blocked}) — the conversation is "
                              "kept and will not roll over until that is fixed. "
                              "Then /rollover."))
            return
        self.emit(ev("notice", level="warn",
                     text=f"{head} and this session will not roll over by itself "
                          "— /rollover, or set rollover_mode: auto"))

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
        # Thinking must stop early enough that the closing round still fits
        # inside the turn wall that run_turn already holds us to.
        wall = self.engine.turn_timeout
        reserve = getattr(self.engine, "answer_time_reserve", configlib.DEFAULT_ANSWER_TIME_RESERVE)
        think_deadline = (turn_t0 + max(0, wall - reserve)) if wall else None

        transport = await self.engine.resume_transport()
        if transport == "off":
            return await self._forced_conclusion(call_round, self._answer_reserve())
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
            # corrected_text, not count_text: this is the one place a local
            # count is subtracted from a figure in the server's own tokens,
            # and the pad is the largest term in the whole window. The ~5%
            # cl100k runs low is proportional while PAD_CONTEXT_MARGIN is
            # fixed, so on a big window the error eats the margin whole — a
            # 200k pad on a 262k window is ~10,000 tokens past the wall this
            # line exists to stop short of. The count_text calls below stay
            # raw on purpose: they size a tail against PAD_TAIL_TOKENS, which
            # is a local target, not the server's budget.
            if self._pad_headroom() - corrected_text(pad) <= 0:
                # Negative is "no limit" — see config.py. The loop still cannot
                # spin: every cycle has to shrink the pad or it breaks below.
                if (self.engine.max_pad_compactions >= 0
                        and compactions >= self.engine.max_pad_compactions):
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
                compacted = await self._compact_pad(call_round, pad,
                                                    self._answer_reserve())
                compactions += 1
                if not compacted:
                    # The model could not distil its own pad. Falling straight
                    # to the answer here spends the whole compaction budget on
                    # one bad note, so drop the older reasoning mechanically
                    # instead: the tail is the resume point, which is the part
                    # that has to survive verbatim anyway. Lossier than a real
                    # checkpoint, and still better than stopping.
                    tail = self._pad_tail(pad)
                    if count_text(tail) < count_text(pad):
                        self.emit(ev("notice", level="info",
                                     text="the checkpoint came back empty — keeping "
                                          f"the last ~{count_text(tail)} tok and "
                                          "thinking on"))
                        pad = tail
                        continue
                    # Nothing to gain; concluding from a complete pad is the
                    # fallback, losing it is never.
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
        # Read here rather than at the top of the function, so it is the same
        # number the wall above just stopped the pad on. A tool can run during
        # the pad loop, which moves used_now() and therefore the reserve; a cap
        # captured before that would be the stale one, and the gate and the
        # request would once again be describing different windows.
        answer_cap = self._answer_reserve()
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
                         text="the answer phase came back empty — the window is "
                              "full; roll over, or lower rollover_at_percent"))
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
                                  "at": now_stamp(), **self.mark()})
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
                     text="forced conclusions came back empty — the window is "
                          "full; roll over, or lower rollover_at_percent"))
        return ChatResult("")

    async def _digest_turn(self, user_msg, reasoning, blocks_before):
        """Replace one finished turn's working-out with a digest of it.

        The question and the answer stay word for word. What goes is the part
        in between — the assistant messages carrying tool_calls, their
        arguments, and the role="tool" results they produced — which across
        four measured sessions was 91-95% of the window. The journal already
        holds every byte of it verbatim, written as the turn ran, so this
        shortens what the model is shown and nothing else.

        That last sentence is a guarantee, so it is checked rather than
        assumed. `jot` never raises: a journal that filled the disk on the
        first tool call of this turn looks exactly like one that recorded it
        perfectly, and dropping the only copy on that evidence would be
        silent, total loss. Hence `blocks_before` — the journal must be
        healthy *and* demonstrably larger than when the turn started.

        Everything else here fails toward keeping the context: no engine, no
        room, a digest that comes back empty, an exception anywhere — all of
        them leave the history exactly as the turn left it.
        """
        if not (self.digest_turns and self.engine):
            return
        # Identity, not an index: repair_history and the tool loop both reshape
        # the list during a turn, and an index captured before that is a
        # different message by now.
        start = next((i for i, m in enumerate(self.history) if m is user_msg), None)
        if start is None:
            return
        work = self.history[start + 1:]
        if not any(m.get("tool_calls") for m in work) and not reasoning.strip():
            return   # a plain chat turn has no working-out; spend no round

        if not (self.journal_ok() and self.journal_blocks() > blocks_before):
            self.emit(ev("notice", level="warn",
                         text="not collapsing this turn — the journal did not "
                              "record it, and the context is the only copy"))
            return

        # The same three caps as the handoff, for the same reasons: an absolute
        # ceiling, a share of the window so the digests do not become the tax
        # they exist to avoid, and what is actually free in the call about to
        # be made.
        #
        # That third cap is measured against this call's own prompt, not
        # against the session's current usage, and the difference matters. The
        # handoff summarises the whole history, so there `budget - used` is
        # the room in its own prompt. This call sends only the turn, and the
        # window is at its fullest precisely because of the turn being
        # collapsed — budgeting it against `used` made the digest decline on
        # every turn big enough to be worth digesting, rolling the window over
        # instead.
        #
        # The prompt is built here rather than estimated from `work`, because
        # it is not `work`: it carries the pad, which is in no history at all
        # and is the largest of the three terms, and it carries no tool block.
        # Estimating from the slice got both wrong in opposite directions, and
        # an estimator that read 60% high happened to cover the difference.
        # It no longer does.
        pad_tail = reasoning[-DIGEST_PAD_TAIL_CHARS:]
        request = store.digest_messages(work, pad_tail)
        cap = min(DIGEST_MAX_TOKENS,
                  self.budget // DIGEST_WINDOW_SHARE,
                  self.budget - self.estimate(request, tools=False) - DIGEST_MARGIN
                  ) if self.budget else DIGEST_MAX_TOKENS
        if cap < DIGEST_MIN_TOKENS:
            self.emit(ev("notice", level="info",
                         text="no headroom to digest this turn — keeping it in full"))
            return

        # The answer is the last plain assistant message of the turn. There may
        # be none: a turn can end on an error or produce nothing but tool calls.
        answer = next((m for m in reversed(work)
                       if m.get("role") == "assistant" and not m.get("tool_calls")
                       and (m.get("content") or "").strip()), None)
        # Every user message survives, not just the one that opened the turn:
        # a /steering interjection lands in the middle of the working set and
        # is something a person typed. Those are the one thing here that exists
        # nowhere else in a recoverable form, so they are never summarised.
        spoken = [m for m in work if m.get("role") == "user"]
        # Inside the try with the call it belongs to, not above it. The turn
        # has already produced its answer by the time any of this runs, so
        # nothing here may cost it — and this line reaches two attributes
        # deep through a gate thirty lines up.
        try:
            path = getattr(self._journal, "path", None)
            record = (f"[full record: grep -n '^## turn {self.turn_id} ' "
                      f"{path}]") if path else ""
            digest = await store.build_turn_digest(
                self.engine, work, pad=pad_tail, max_tokens=cap,
                record=record)
        except Exception as e:
            self.emit(ev("notice", level="warn",
                         text=f"could not digest this turn ({e}) — keeping it in full"))
            return
        if not digest:
            return

        # Assigned in one statement, never mutated in place: a cancellation
        # between two mutations would leave a history that settled() no longer
        # recognises, and this runs inside the turn's own timeout wall.
        self.history = (
            self.history[:start + 1]
            + spoken
            + ([answer] if answer else [])
            + [{"role": "assistant", "content": digest,
                "at": now_stamp(), **self.mark(), "digest": True,
                # The same figure digest_done shows: replayed markers carry
                # the wording without recomputing.
                "tokens": count_text(digest)}])
        # The window just got much smaller, so the measured prompt size
        # describes a history that no longer exists — same reason as the
        # max_history trim below and roll_over's own reset. Leaving it set
        # would have the very next check roll over to relieve pressure this
        # just relieved.
        self._last_used = None
        # digest_text is the summary itself and not merely its size, for the
        # same reason rollover_done carries handoff_text: from here on this is
        # what the model is shown in place of the turn, and a token count
        # cannot be checked against the work it replaced. A reader who can see
        # the digest can tell a faithful one from a lossy one while the turn it
        # describes is still in mind; a reader who cannot find out at the point
        # the model acts on the gap.
        #
        # Its own event rather than the `info` notice this replaces, on both
        # counts that made that notice the wrong carrier: a notice is one line
        # of text, and Discord drops every notice that is not a warning
        # (discord_client.handle), so the one client with no other window into
        # the session was told nothing at all.
        self.emit(ev("digest_done", turn_id=self.turn_id,
                     tokens=count_text(digest), digest_text=digest))

    async def _expand_digests(self, all_=False):
        """Restore turns _digest_turn collapsed, from the journal they left.

        The inverse of a collapse, with the same discipline: anything that
        cannot be verified fails toward keeping the digest, because the
        journal is the only other copy, and a segment that stopped recording
        part-way through would make a partial expansion a partial loss.

        A collapse leaves the turn's region as [user, ...steering, answer?,
        digest] — the answer before the digest, so this cannot splice the
        rebuild where the digest sat. It finds the region by turn id instead:
        every message of the turn carries it in its mark, and the digest's
        record pointer (code-appended, not model-written) names the turn and
        the segment to read.
        """
        digests = [i for i, m in enumerate(self.history) if m.get("digest")]
        if not digests:
            # A report, not an error: the WebUI button's cycle
            # (off → collapse → expand → off) routes every on-collapse click
            # through here, where nothing is collapsed by definition, and a
            # click that surfaces an error would make the cycle read as a bug.
            # A typed /digest expand still hears back — the clients print the
            # empty report, since there is no digest_expanded broadcast for it.
            return {"ok": True, "expanded": {"turns": [], "messages": 0,
                                             "skipped": []}}
        targets = digests if all_ else [digests[-1]]
        plan = []       # (start, digest_idx, turn_id, rebuilt)
        skipped = []
        for i in targets:
            pointer = DIGEST_POINTER_RX.search(
                self.history[i].get("content") or "")
            if not pointer:
                skipped.append("one digest carries no record pointer")
                continue
            turn_id = int(pointer.group(1))
            path = pointer.group(2)
            blocks = await asyncio.to_thread(_read_turn_blocks, path,
                                             turn_id)
            if not blocks:
                # Covers both ways the read comes back empty: the file itself
                # is gone, or it is there but holds no record of this turn —
                # either way there is nothing to verify, so the digest stays.
                skipped.append(f"turn {turn_id}: no record of it in the "
                               f"segment it points at ({path})")
                continue
            # The region starts where the turn number begins: everything back
            # from the digest stamped with it is this turn's, and the first
            # message with a different number ends the walk. The system
            # message has none, so a turn at the top of the window stops there.
            start = i
            while start > 0 and self.history[start - 1].get("turn") == turn_id:
                start -= 1
            rebuilt, reason = _reconstruct_turn(self.history[start:i],
                                                turn_id, blocks)
            if rebuilt is None:
                skipped.append(f"turn {turn_id}: kept — {reason}")
                continue
            plan.append((start, i, turn_id, rebuilt))
        if not plan:
            return {"ok": False,
                    "error": "nothing was restored — " + "; ".join(skipped)}
        # Splice from the tail, so an earlier region's indices are not moved
        # by a later one, and assign the whole list in one statement — a
        # cancellation between two mutations would leave a history settled()
        # no longer recognises, the same reason _digest_turn works that way.
        new_history = list(self.history)
        turns = []
        restored = 0
        for start, i, turn_id, rebuilt in sorted(plan, key=lambda r: -r[1]):
            # i + 1: the digest is inside the replacement. Splicing to i
            # would leave the summary in the window with the full working
            # out re-inserted around it — paying for both.
            new_history[start:i + 1] = rebuilt
            turns.append(turn_id)
            restored += len(rebuilt) - (i - start)
        self.history = new_history
        turns.sort()
        # The window just grew, so the measured prompt size describes a
        # history that no longer exists — the same reset _digest_turn makes
        # when it shrinks one.
        self._last_used = None
        # The state rides with the event, read after the splice: a client
        # that repainted from the pre-expansion count would still be pointing
        # at a digest this call just removed.
        self.emit(ev("session_state", what="digest_expanded",
                     turns=turns, messages=restored,
                     skipped=skipped, **self._digest_state()))
        await self.persist()
        return {"ok": True, "expanded": {"turns": turns,
                                         "messages": restored,
                                         "skipped": skipped}}

    async def _after_turn(self):
        trip = self.trip
        rolled = False
        if trip:
            # used_now(), not a measurement passed in: the one caller handed us
            # `self._last_used` and this line then re-derived used_now() from
            # it, character for character. Nothing can change it in between —
            # the argument was evaluated in the statement that awaits this, and
            # there is no suspension point before here.
            used = self.used_now()
            if used >= trip:
                rolled = await self._maybe_roll(used, trip)
        if rolled:
            self.declined_at, self.warned_over = None, False
            self._no_room_warned = False
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
        except Exception as e:
            # error, not warn: nothing was rolled, the window is still full, and
            # the next turn will be refused the same way until this is fixed.
            #
            # Exception, not OSError. A disk that will not take the write is the
            # failure this path was written for, but json.dumps runs inside the
            # same call, so a history holding something unserialisable raised
            # TypeError straight out of roll_over and the person was told
            # nothing whatsoever. Whatever arrives here has already cost them
            # the rollover; silence must not be on top of that.
            self.rollover_count -= 1
            self.rollover_blocked = str(e) or e.__class__.__name__
            if not isinstance(e, OSError):
                # A full disk is the environment and the text says it all. This
                # is a bug in our own payload, and nobody will find it from one
                # line of notice.
                logger.exception("rollover failed on a non-OSError")
            # The error itself, not a guess at its remedy. "Free some disk" was
            # right for ENOSPC and wrong for the other three measured causes —
            # a read-only sessions dir, a bad sessions_dir, a dropped mount —
            # where it sends someone to do a thing that cannot help.
            self.emit(ev("notice", level="error",
                         text=f"could not save the transcript ({e}) — rollover "
                              "abandoned, the conversation is kept. Fix that, "
                              "then /rollover."))
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
            # honestly: `estimated` is only true when the server withheld
            # usage and the local figure is all we have.
            used = self.used_now()
            return {"ok": True, "info": {
                "session": self.name, "session_id": self.session_id,
                # The build that is answering, which on a long-lived core is
                # not necessarily the one checked out in the working tree.
                "version": __version__,
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
                # A different axis from `estimated`, and the reason both are
                # here. `estimated` says the server withheld usage, so this
                # figure is ours; `tokenizer` says what "ours" was counted
                # with. A chars/4 count is wrong by far more than the 5% the
                # corrections are fitted for, and until this field existed a
                # user on that path read output identical to a user on
                # cl100k — the module promised to say which was live and
                # nothing asked it.
                "tokenizer": tokenizer_status(),
                "tools_tokens": self.tools_tok,
                # The WebUI's digest button paints from here, on attach:
                # whether collapsing is on, who turned it on, and how many
                # collapsed turns the window still holds (its expand position
                # has work to do).
                "digest": self._digest_state(),
                # Since the session was created, surviving /clear and restarts.
                "output_total": self.total_output,
                # The bill, as opposed to the gauge. input_total counts every
                # round of every turn, so on a long conversation it runs an
                # order of magnitude above `used` — that gap is the cost of
                # re-sending the window, and it is the number a rollover
                # firing too early inflates.
                "input_total": self.total_input,
                "input_exact": self.input_exact,
                # None, not 0, when no round has ever reported a cache figure:
                # Argo's shim withholds it entirely, and a 0% hit rate there
                # would be a sincere-looking claim about a server that said
                # nothing. See Session.total_cached.
                "cache_hit_pct": (round(100 * self.total_cached / self.cached_input, 1)
                                  if self.cached_input else None),
                "cached_total": self.total_cached,
                "rollover": {"mode": self.rollover_mode, "percent": self.rollover_pct,
                             "trip": self.trip, "count": self.rollover_count},
                # What the server actually does, not what the config assumes.
                # Probed once per engine and cached, so this is free after the
                # first /info; see LLMEngine.capabilities.
                "server": await self._server_caps(),
            }}
        if name == "behavior":
            return {"ok": True, "model": self.model_name,
                    "session": self.name,
                    "base": self.base_behavior or "",
                    "behavior": self.behavior or "",
                    "session_behavior": self.session_behavior or ""}
        if name == "model" and not args.strip():
            # The listing half of /model, up here with /info and /behavior
            # because it reads two fields and changes nothing — and because
            # the moment you most want to know what you could switch to is
            # while a turn is running. The switching half stays below the
            # busy guard, where it belongs: it swaps the engine under a
            # live conversation.
            #
            # There was a second command, /models, returning this exact dict
            # from this exact spot. Two spellings of one answer: the help in
            # both chat.py and app.js already described /model as "list
            # models, or /model <name> to switch", neither listed /models,
            # and the WebUI refused it outright. All it really bought over
            # bare /model was being readable mid-turn, which is now just
            # where this branch sits.
            return {"ok": True, "current": self.model_name,
                    "configured": configlib.known_models(self.config)}
        if name == "digest" and not args.strip():
            # The reporting half of /digest, up here for the same reason the
            # model listing is: it reads history and changes nothing, and the
            # moment a person wants to know the state is while a turn runs.
            # The halves that change anything stay below the busy guard.
            return {"ok": True, "digest": self._digest_state()}
        if name == "reload":
            # Manager-level, and deliberately outside the turn lock: reload defers
            # any session that is busy, so taking the lock here would have it defer
            # the very session that asked, or deadlock waiting on itself.
            return await self.manager.reload()
        if name == "restart":
            # Also manager-level, and above the busy guard for the same reason
            # plus one of its own: the whole point of the refusal below is to
            # name which sessions are busy, and from inside a busy session the
            # guard would answer "a turn is running in this session" and never
            # get there. request_restart does its own busy check across every
            # session, which is the one that matters here.
            force = any(a.lstrip("-").lower() == "force" for a in args.split())
            return await self.manager.request_restart(force=force)

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
                self._no_room_warned = False
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
                trip = self.trip
                # Asked for by a person, so the guard an earlier disk failure
                # set is cleared first: they have usually just freed the space
                # and this is how they say so. If it fails again it is set again.
                self.rollover_blocked = None
                if not await self.roll_over("on request", trip=trip):
                    # The reason, not "see the error above": this reply is what
                    # a /rollover typed on Discord or in the terminal prints,
                    # and the notice it refers to may not have been rendered
                    # anywhere the person is looking.
                    return {"ok": False,
                            "error": f"could not archive this window "
                                     f"({self.rollover_blocked}) — the "
                                     "conversation is untouched."}
                self.declined_at, self.warned_over = None, False
                self._no_room_warned = False
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
                self._no_room_warned = False
                self.rollover_count = 0
                self.rollover_pct = self._configured_rollover_pct()
                self._last_used = None
                # Turn numbering starts over with the rest of it. It is not
                # part of the history, so wiping the history left turn 214
                # sitting on a session with no turns in it — and the number is
                # load-bearing twice over: it labels the journal blocks and the
                # `[full record: grep -n '^## turn N ' ...]` pointer, so a
                # surviving high count makes the first turn of the new window
                # point at a grep that matches nothing. /clear keeps it on
                # purpose: those archives still exist, and their numbering has
                # to keep matching. last_seq deliberately does not reset —
                # restoring it is how a client with an old cursor learns it
                # missed something rather than being handed a sequence that
                # quietly restarted underneath it.
                self.turn_id = 0
                self.round_id = 0
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
                # A bare /model was answered above the busy guard, so there is
                # a name here and this is a switch.
                target = args.strip()
                try:
                    await self.use_model(target)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                self.emit(ev("session_state", what="model", model=self.model_name,
                             budget=self.budget))
                await self.persist()
                return {"ok": True, "model": self.model_name, "budget": self.budget}
            if name == "digest":
                choice = args.strip().lower()
                if choice in ("off", "collapse"):
                    # A person's answer to the digest question. It outranks
                    # both config layers until this session dies, and it is
                    # written to the live file so a restart does not answer
                    # "config" to a question the person already decided.
                    self.digest_override = choice == "collapse"
                    self.digest_turns = self._effective_digest_turns()
                    # The broadcast carries the state, the reply is just ok:
                    # /clear and /model do it this way, and a reply that also
                    # carried the state would have the asking client print the
                    # same choice twice.
                    self.emit(ev("session_state", what="digest",
                                 **self._digest_state()))
                    await self.persist()
                    return {"ok": True}
                if choice in ("expand", "expand all"):
                    # The undo of a collapse, not a state: off/collapse decide
                    # what happens to the next turn, expand re-reads the past
                    # from the journal. Re-inserted turns are bigger than the
                    # digest they replace, which is the whole of the risk —
                    # so all_ is opt-in, and a bare expand is one turn.
                    return await self._expand_digests(all_=choice == "expand all")
                return {"ok": False,
                        "error": "usage: /digest off | /digest collapse | "
                                 "/digest expand [all] "
                                 "(or bare /digest to report the mode)"}
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
        # Resolved after model_name moves, because the model entry is allowed
        # to override it — a session switched onto a model whose window makes
        # digesting pointless must stop digesting, and one switched back must
        # start again. Reading the live config means this needs no cooperation
        # from reload, which may have rewritten either layer since. Through
        # _effective_digest_turns(): a /digest choice outranks the model entry
        # it was made on, and a switch must not silently overrule it.
        self.digest_turns = self._effective_digest_turns()
        # A different engine is a different server with different answers, so
        # the findings this session has already reported are about somebody else.
        self._server_warned = False
        # Same reason, and it matters more than the warning does: the latch
        # says "this server reports a cache figure, so read a missing one as
        # zero". Carried onto a server that reports nothing, it would read
        # every silent round as a cache miss and show a hit rate collapsing to
        # zero — the exact alarm this number exists to raise honestly.
        self._cache_reported = False
        # The base is shared by every session, the model file is per model,
        # and the session file is named for this session.
        (self.base_behavior, self.behavior,
         self.session_behavior) = await self.manager.behavior_for(entry, self.name)
        self._rebuild_system_message()
        # Two things just moved under the measurement, not one. history[0] was
        # rebuilt from a different model's behavior layers, so _last_used sizes
        # a system prompt that is gone — and self.engine changed, so budget is
        # a different number too. Switching from a 262k window to a 12k one
        # would otherwise compare a figure measured against the big model's
        # prompt with the small model's budget, in _pad_headroom, in
        # _answer_reserve and in the next _after_turn.
        self._last_used = None


class SessionManager:
    """Sessions by name, and one engine per model shared between them."""

    def __init__(self, config, config_path=None):
        self.config = config
        # Where config came from, so /reload re-reads the file actually in use
        # rather than the default when --config pointed somewhere else.
        self.config_path = Path(config_path or configlib.CONFIG_PATH)
        self.sessions = {}
        self._engines = {}
        # Set by server.build_app to the thing that can actually re-exec this
        # process. None when nobody installed one — a manager built by a test or
        # an embedder has no process to restart, and request_restart says so
        # rather than raising AttributeError at whoever asked.
        self.restart_hook = None
        # A restart is armed and the process is on its way out. Read by the
        # server to turn new submits away: the busy check in request_restart is
        # a snapshot, and without this a turn can start inside the grace window
        # and be cancelled by the very restart that just reported none running.
        self.restarting = False
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
        self._tools = build_tools(tools_cfg, workdir=workdir,
                                  files_dir=paths.files_root(config))
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

    def _all_engines(self):
        """Every engine something could still read from, in first-seen order.

        Three sources: the manager's per-model cache, the engine each session
        is using, and the engine a deferred config blob has already built but
        not installed — the session swaps to that one the moment its turn
        ends, so it is spoken for.

        Retired engines are deliberately not here. Nothing reads from them,
        which is why they were retired; only close() wants them, and it
        chains them on itself.
        """
        yield from (e for e, _, _ in self._engines.values())
        yield from (s.engine for s in self.sessions.values())
        yield from (b["engine"] for s in self.sessions.values()
                    if (b := s._pending_config))

    def _live_engine_ids(self):
        """id() of every engine something could still read from."""
        return {id(e) for e in self._all_engines() if e is not None}

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

    async def request_restart(self, force=False):
        """Arm a restart of the core process. Returns before anything happens.

        The counterpart to reload(): where that one re-reads config.yaml into a
        running core, this one brings the core back, which is the only way to
        move the handful of keys reload cannot touch — core.bind, core.port and
        the three the log handlers read once when they are installed.

        Returning early is the point. The caller's reply has to reach its client
        before the sockets close under it, so all this does is validate, say so,
        and hand off to the hook; the hook arms a timer and the process leaves
        on its own a moment later. See server._arm_restart for the other half.

        Only the *core* restarts. The Discord bot is a sibling process started
        by serve.sh, untouched by the re-exec and reconnecting on its own — so
        someone who edited discord_client.py and typed /restart has not yet run
        the code they just wrote, and the reply says so.
        """
        if self.restart_hook is None:
            return {"ok": False,
                    "error": "this core cannot restart itself — it was not "
                             "started through server.main(), so nothing here "
                             "owns the process"}
        if self.restarting:
            # A double-click, or two clients racing. Not an error — the thing
            # asked for is already happening — but it must not broadcast a
            # second notice or arm a second timer.
            return {"ok": True, "already": True,
                    "note": "a restart is already under way"}

        # reload() is a three-step read-modify-write over the engine set and the
        # forward table, and _on_shutdown does not cancel the handler parked in
        # `await session.command(...)` — it closes sockets, which that handler
        # is not reading from. So a restart landing mid-reload runs close()
        # concurrently with plan/stage/commit: engines closed as the reload is
        # about to hand them to sessions, forwards torn down half-committed.
        # Cheaper to refuse for the second it takes than to make close() safe
        # against it.
        if self._reload_lock.locked():
            return {"ok": False,
                    "error": "a config reload is in progress — try again in a "
                             "moment"}

        # The config is validated BEFORE anything is armed, because the new
        # image reads it with no one to tell. main() exits 1 on a file that will
        # not parse, on a bad core.port and on a non-loopback bind with no
        # token; serve.sh hands that status up; and the unit is Restart=on-
        # failure with RestartSec=3 and no StartLimitBurst. So a /restart over a
        # broken config is not a failed restart, it is a core that disappears
        # into a restart loop — and /restart is exactly what someone reaches for
        # just after editing config.yaml. Refusing here costs one file read.
        try:
            fresh = await asyncio.to_thread(configlib.load_config, self.config_path)
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                yaml.YAMLError, RuntimeError) as e:
            return {"ok": False,
                    "error": f"not restarting: {self.config_path} would not "
                             f"load, and the core that came back would exit on "
                             f"it — {type(e).__name__}: {e}"}
        bad = configlib.boot_blockers(fresh)
        if bad:
            return {"ok": False,
                    "error": f"not restarting: the core that came back would "
                             f"exit — {bad}"}

        # s.busy, not the lock, for the reason the property documents: a turn
        # created by start_turn has not reached the lock yet.
        busy = sorted(s.name for s in self.sessions.values() if s.busy)
        if busy and not force:
            return {"ok": False, "busy": busy,
                    "error": "a turn is running in " + ", ".join(busy) +
                             " — /restart force restarts anyway, and those "
                             "turns are lost"}

        self.restarting = True
        logger.warning("restart requested%s%s", " (forced)" if force else "",
                       f", cancelling turns in {', '.join(busy)}" if busy else "")
        # Every attached client in every session, not just the asker's: the
        # terminal windows are about to be closed on people who did not type
        # this. They all render `notice` already, so this needs no new event.
        for s in self.sessions.values():
            s.emit(ev("notice", level="warn",
                      text="♻️ the core is restarting — it will be back in a "
                           "moment (the Discord bot is a separate process and "
                           "is not restarted)"))
        self.restart_hook()
        return {"ok": True, "restarting": True, "forced": bool(busy)}

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
            new_tools_list = build_tools(tools_cfg, workdir=workdir,
                                         files_dir=paths.files_root(fresh))
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
        # One (attr, old, new) triple per conversation scalar, read through
        # the same CONV_SCALARS table __init__ uses. apply_config applies a
        # value only where the session still agrees with the old one, so the
        # old side is there to be compared against and nothing else — which is
        # why it reads silently: one typo should warn once, not twice.
        #
        # The coercions cannot raise. reload() has already committed the new
        # config by the time this list is built, so a ValueError here would
        # leave the core running the new settings with half the sessions never
        # told about them — the one failure mode the all-or-nothing validation
        # above exists to prevent. That is int_or's job inside the table.
        scalars = [(sc.attr, sc.read(old_conv, None), sc.read(new_conv, "reload"))
                   for sc in CONV_SCALARS]

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
                 # The window's turns and rounds, separately: user messages and
                 # assistant messages. The sidebar shows them as turns.rounds
                 # (tool results are neither), and both stay scoped to the
                 # current window, as messages does — turns rolled into the
                 # archives are not counted twice.
                 "turns": sum(1 for m in s.history if m["role"] == "user"),
                 "rounds": sum(1 for m in s.history if m["role"] == "assistant"),
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
                {"t": "rename", "id": s.session_id, "from": old, "to": new},
                self.config)
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
        closing = {id(e): e for e in (*self._all_engines(), *self._retired_engines)
                   if e is not None}
        self._retired_engines = []
        for engine in closing.values():
            try:
                await engine.close()
            except Exception:
                logger.debug("engine close during shutdown", exc_info=True)
        await self.forwards.close()
