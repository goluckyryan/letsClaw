"""Token counter module — estimate token usage for messages and full context.

Counts with tiktoken's `cl100k_base`, a GPT-family encoding standing in for
Qwen's tokenizer, which lives on the server and is not a pip install. Measured
against a real server, that substitution costs about **5% on conversation
prose** — the local count runs low, consistently, in one direction. Callers
that need the server's number corrected apply `PROSE_SCALE`; see docs/tokens.md.

The 5% figure holds only for the cl100k path. There is a chars/4 fallback for
when the vocab cannot be loaded at all, and it is far cruder. The corrections
below still ride along on that path — a second set of constants fitted for
chars/4 would be the same mistake as one multiplier for two different errors,
and 5% is noise beside what chars/4 is already wrong by. What changes is the
claim, not the arithmetic: `calibrated()` says which encoding is live so the
clients can stop calling the figure accurate.

What this module does *not* see: the chat template. `count_tools()` is given
`json.dumps(specs)`, while the server renders those schemas into Qwen's own
scaffolding first. That error is a fixed offset rather than a ratio, which is
why it is corrected separately and not by a multiplier here.
"""

import json
import logging
import os

import paths

logger = logging.getLogger("letclaw.tokens")

# tiktoken caches the 1.6 MB cl100k vocab under TIKTOKEN_CACHE_DIR and defaults
# to a directory in /tmp, which the OS clears on reboot. With the cache gone
# and no network — a lab machine behind a proxy, which is the normal case here
# — get_encoding() raises, and counting silently drops to chars/4 until someone
# notices the token line went strange. Pinning the cache inside state/ makes
# fetching the vocab a once-ever cost instead of a once-per-boot one.
_CACHE_DIR = paths.REPO_ROOT / "state" / "tiktoken"

_encoder = None
_encoder_checked = False
_fallback_reason = None


def _get_encoder():
    """The cl100k encoder, or None with `_fallback_reason` set saying why."""
    global _fallback_reason
    try:
        import tiktoken
    except ImportError:
        _fallback_reason = ("tiktoken is not installed — "
                            "pip install -r requirements.txt")
        logger.warning("%s; counting with chars/4", _fallback_reason)
        return None

    # The persistent cache first, then whatever the environment was already
    # pointing at. The second attempt is what makes this a strict improvement:
    # a machine with a warm /tmp cache and no network keeps working on the day
    # state/tiktoken is still empty.
    attempts, seen = [], set()
    for cache in (_CACHE_DIR, os.environ.get("TIKTOKEN_CACHE_DIR")):
        if cache is None or str(cache) in seen:
            continue
        seen.add(str(cache))
        try:
            if cache is _CACHE_DIR:
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
            return tiktoken.get_encoding("cl100k_base")
        except Exception as e:                       # network, disk, anything
            attempts.append(f"{cache}: {e}")

    _fallback_reason = ("could not load the cl100k_base vocab — "
                        + "; ".join(attempts))
    # Loud, because the consequence is invisible: every count still returns a
    # number, just a much worse one, and the corrections calibrated on cl100k
    # stop applying. Degrading beats refusing to start, but not silently.
    logger.warning("%s; counting with chars/4, which is far cruder. Run once "
                   "with network access to populate %s", _fallback_reason,
                   _CACHE_DIR)
    return None


def _enc():
    global _encoder, _encoder_checked
    if not _encoder_checked:
        _encoder = _get_encoder()
        _encoder_checked = True
    return _encoder


def calibrated() -> bool:
    """True when counting with cl100k, False on the chars/4 fallback.

    Every correction below is fitted against cl100k, so on the fallback path
    the numbers are a guess with a correction bolted on. They are still the
    best guess available and still used — but a caller presenting one to a
    person should say so.
    """
    return _enc() is not None


def tokenizer_status() -> dict:
    """What is doing the counting, for /info and the startup log."""
    return {"encoding": "cl100k_base" if calibrated() else "chars/4",
            "calibrated": calibrated(),
            "reason": _fallback_reason}


def count_text(text: str) -> int:
    """Estimate tokens in a plain string."""
    if not text:
        return 0
    enc = _enc()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, len(text) // 4)


def count_content(msg: dict) -> int:
    """One message's own text, with none of the framing around it.

    Assistant turns that call tools carry their payload in tool_calls, not in
    content, so counting content alone scores them as bare overhead and makes a
    tool-heavy conversation look far smaller than it is.
    """
    total = count_text(msg.get("content") or "")
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        total += count_text(fn.get("name") or "") + count_text(fn.get("arguments") or "")
    return total


def count_tools(specs: list) -> int:
    """Estimate tokens for tool schemas.

    Schemas are re-sent with every request, so on a tool-enabled session they are
    a large fixed part of each prompt — invisible to a conversation count, which
    only ever sees the messages.

    This is the raw JSON cost. What the server charges is more — see
    `schema_cost()`.
    """
    if not specs:
        return 0
    return count_text(json.dumps(specs))


# --- correcting a cl100k count toward the server's own tokenizer -------------
#
# The counts above are what cl100k says about text we hold locally. What
# decides a rollover is what the *server* counts, and the two differ in two
# ways that have different shapes — which is the whole reason there are three
# constants here instead of one multiplier:
#
#   per token    the encodings disagree, proportionally: cl100k runs ~5% low
#                on the same prose. A ratio, so it scales with the text.
#   per message  the template wraps every message in role markers and
#                separators. The OpenAI cookbook's estimate, which this module
#                used to carry as a constant, guesses 4; Qwen charges about
#                11. Grows with the number of messages, not their size.
#   per prompt   count_tools() sees `json.dumps(specs)`; the server sees those
#                schemas rendered into the template's own scaffolding. A fixed
#                block plus a small per-tool cost, and it does not grow with
#                the conversation at all.
#
# One multiplier cannot correct three errors of three different shapes, and
# trying is exactly why the single x1.6 this replaced read +34% on an empty
# window and +47% on a full one: a ratio applied to errors that were not
# ratios. Fitted together against a live Qwen server over nine checkpoints
# spanning 1.8k-17k tokens, these three land within 0.6%. See docs/tokens.md.
PROSE_SCALE = 1.05
# Rounded up from a fitted 10.8. Up, because a per-message term is the one of
# the three that grows with a long conversation, and an estimate that runs
# slightly high rolls the window slightly early — the cheap direction. Running
# low means discovering the ceiling as a 400.
MESSAGE_FRAMING_TOKENS = 11
# Qwen's chat template, fitted on every subset of a 4-tool config and holding
# to ±2 tokens. Another template will differ, and nothing local can tell which
# one the server runs — so this is the fallback, used only where the engine has
# not been able to observe the real figure from a reported prompt_tokens.
SCHEMA_BASE_TOKENS = 198
SCHEMA_PER_TOOL_TOKENS = 11


def corrected_text(text: str) -> int:
    """A text count corrected toward what the server will charge for it.

    Bare text, so only the per-token correction applies — this is for things
    like a thinking pad, which is spliced into a message rather than being
    one. A list of messages goes through corrected_messages().
    """
    return int(PROSE_SCALE * count_text(text))


def corrected_messages(messages: list) -> int:
    """A message-list count corrected toward what the server will charge.

    Built from the content up rather than by scaling a plain message count,
    which would multiply the cookbook's 4-token framing guess by the prose
    ratio and arrive at 4.2 where the template charges 11. That is the error
    that makes
    a corrected estimate drift low as a conversation lengthens, since the
    framing is paid per message and conversations grow by messages.
    """
    return (int(PROSE_SCALE * sum(count_content(m) for m in messages))
            + MESSAGE_FRAMING_TOKENS * len(messages))


def schema_cost(specs: list) -> int:
    """What the tool block costs in a prompt, by formula — Qwen-calibrated.

    Prefer an observation: an engine that has seen a `prompt_tokens` for a
    small window can solve for this directly, and that number is true for
    whatever template the server actually runs. This is what to use when there
    is no such observation, which is exactly the case where the estimate is
    load-bearing — a server that withholds usage.
    """
    if not specs:
        return 0
    return SCHEMA_BASE_TOKENS + count_tools(specs) + SCHEMA_PER_TOOL_TOKENS * len(specs)
