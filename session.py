"""session.py — everything letsClaw keeps on disk under state/.

Two stores, deliberately separate:

* **live** (state/live/) — one file per session, holding the conversation as it
  stands right now. Rewritten after every turn and reloaded when the core
  restarts, so closing the service is not the same as losing the work.
* **archive** (state/sessions/) — a snapshot taken when a context window fills
  up, plus the handoff the model distils from it. Write-once records of a
  session that ended, never read back into a live conversation.

Assembling a history is core.py's job (Session.fresh_history); this module only
moves bytes and runs the summariser.
"""

import asyncio
import hashlib
import itertools
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("letclaw.session")

# One core process now holds many conversations. Two of them rolling over in the
# same second must not land on the same filename, and the shared memory file must
# not interleave two half-written handoffs.
_seq = itertools.count(1)
_handoff_lock = asyncio.Lock()

# Anchor relative paths to the repo, not the working directory, so the app
# behaves the same however it was launched.
REPO_DIR = Path(__file__).parent

DEFAULT_SESSIONS_DIR = "state/sessions"
DEFAULT_LONG_TERM = "state/memory_store/long_term.md"
DEFAULT_LIVE_DIR = "state/live"

LIVE_SCHEMA = 1     # bumped if the payload shape changes; an older file is skipped

_SUMMARISER_SYSTEM = (
    "You compress a conversation so it can continue in a fresh context window. "
    "Be terse and factual. Keep concrete details — file paths, names, numbers, "
    "decisions already made. Invent nothing. Add no sections beyond the three asked for."
)

_HANDOFF_FORMAT = """OBJECTIVE: the user's current goal, one sentence
ESTABLISHED: bullet list of decisions and facts already settled
OPEN: bullet list of what still needs doing"""


def _resolve(path, default):
    """Config paths are repo-relative unless absolute."""
    p = Path(path or default).expanduser()
    return p if p.is_absolute() else REPO_DIR / p


def _stamp():
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _safe(name):
    """Make a session name usable in a filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name))[:40] or "session"


# ---- the live store: conversations as they stand right now -----------------


def live_dir(config=None):
    return _resolve((config or {}).get("conversation", {}).get("live_dir"),
                    DEFAULT_LIVE_DIR)


def live_path(name, config=None):
    """Where session `name` lives on disk.

    _safe() is lossy — 'my session', 'my/session' and 'my+session' all flatten to
    'my_session', and it truncates at 40 characters — so it cannot be the key.
    The digest is the key; the readable half is there so `ls state/live` tells you
    something. The real name is stored inside the file, and that is what is read back.
    """
    digest = hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:8]
    return live_dir(config) / f"{_safe(name)}-{digest}.json"


async def save_live(payload, config=None):
    """Write one session's state, replacing what was there. Returns the path.

    Atomic by rename: the bytes land in a sibling .tmp, get fsync'd, and only then
    replace the real file. A crash — or a kill -9 mid-write, which is exactly what
    this store exists to survive — leaves the previous good file, never a truncated
    one. Compact JSON, unlike the indented archives: this runs after every turn.
    """
    path = live_path(payload["name"], config)
    tmp = path.with_suffix(".tmp")

    def _write():
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with open(tmp, "w") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    await asyncio.to_thread(_write)
    return path


async def load_live(config=None):
    """Every session on disk, newest first. Never raises.

    A file that will not parse is logged and skipped, not deleted: one bad file
    must not stop the core from starting, and it may still be worth looking at.
    """
    directory = live_dir(config)

    def _read():
        out = []
        if not directory.is_dir():
            return out
        for f in sorted(directory.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError) as e:
                logger.warning("ignoring unreadable session file %s: %s", f, e)
                continue
            if not isinstance(data, dict) or not data.get("name") \
                    or not isinstance(data.get("messages"), list):
                logger.warning("ignoring malformed session file %s", f)
                continue
            if data.get("v") != LIVE_SCHEMA:
                logger.warning("ignoring session file %s: schema %r, expected %r",
                               f, data.get("v"), LIVE_SCHEMA)
                continue
            out.append(data)
        out.sort(key=lambda d: d.get("saved_at") or "", reverse=True)
        return out

    return await asyncio.to_thread(_read)


async def delete_live(name, config=None):
    """Forget a session on disk. Silent if it was never written."""
    path = live_path(name, config)

    def _unlink():
        path.unlink(missing_ok=True)
        path.with_suffix(".tmp").unlink(missing_ok=True)

    await asyncio.to_thread(_unlink)


async def save_transcript(history, model_name, config=None, session_name="session"):
    """Write the full message list to the sessions dir. Returns the path.

    The filename carries the session name and a counter: a bare timestamp has
    one-second resolution, which collides both across sessions and when one
    session rolls over twice quickly.

    Serialising a full history can be megabytes at a 262144-token window, so the
    encode and the write both happen off the event loop — otherwise one rollover
    stalls every other session's token stream.
    """
    config = config or {}
    directory = _resolve(config.get("conversation", {}).get("sessions_dir"),
                         DEFAULT_SESSIONS_DIR)
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "session": session_name,
        "model": model_name,
        "messages": history,
    }
    path = directory / f"{_stamp()}-{_safe(session_name)}-{next(_seq)}.json"

    def _write():
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    await asyncio.to_thread(_write)
    return path


async def append_handoff(text, model_name, transcript_path, config=None,
                         session_name="session"):
    """Append a handoff under a timestamped heading in the long-term memory file.

    Every session appends to the same file, so the heading names the session —
    otherwise a reader cannot tell which conversation a block came from — and a
    lock keeps two concurrent handoffs from interleaving mid-write.
    """
    config = config or {}
    path = _resolve(config.get("memory", {}).get("long_term_file"), DEFAULT_LONG_TERM)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = [f"\n## {stamp}  [{session_name}]  ({model_name})\n"]
    if transcript_path:
        entry.append(f"\nTranscript: `{transcript_path}`\n")
    entry.append(f"\n{text.strip()}\n")
    blob = "".join(entry)

    def _append():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(blob)

    async with _handoff_lock:
        await asyncio.to_thread(_append)
    return path


def render_transcript(history):
    """Flatten a history to plain text for the summariser."""
    lines = []
    for msg in history:
        content = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []
        if calls:
            names = ", ".join((c.get("function") or {}).get("name", "?") for c in calls)
            content = f"{content}\n[called tools: {names}]".strip()
        if content:
            lines.append(f"{msg.get('role', '?')}: {content}")
    return "\n\n".join(lines)


def last_exchange(history):
    """The last plain user+assistant pair, safe to carry into a fresh window.

    Messages carrying tool_calls and every role="tool" message are skipped: half
    of a tool-call pair in a new history is a 400 from the server.
    """
    def plain(msg, role):
        return (msg.get("role") == role and not msg.get("tool_calls")
                and (msg.get("content") or "").strip())

    assistant = next((i for i in range(len(history) - 1, -1, -1)
                      if plain(history[i], "assistant")), None)
    if assistant is None:
        return []
    user = next((i for i in range(assistant - 1, -1, -1)
                 if plain(history[i], "user")), None)
    out = []
    if user is not None:
        out.append({"role": "user", "content": history[user]["content"]})
    out.append({"role": "assistant", "content": history[assistant]["content"]})
    return out


def format_carryover(handoff, transcript_path):
    """The block appended to the new session's system prompt."""
    parts = []
    if handoff:
        parts.append(handoff)
    if transcript_path:
        parts.append(f"The previous session's full transcript is at {transcript_path} — "
                     "read_file it if you need a detail this summary left out.")
    return "\n\n".join(parts)


async def build_handoff(engine, history, max_tokens=800):
    """Ask the model to compress the conversation. Returns "" if it produced nothing.

    Runs on its own message list: the real history is never touched, and the
    request never becomes part of the conversation.
    """
    body = render_transcript(history)
    if not body.strip():
        return ""
    messages = [
        {"role": "system", "content": _SUMMARISER_SYSTEM},
        {"role": "user", "content": (
            f"Conversation so far:\n\n{body}\n\n"
            f"Write a handoff using exactly these three sections:\n\n{_HANDOFF_FORMAT}")},
    ]
    # A thinking model will spend the entire budget reasoning and hand back an
    # empty string, so ask the chat template to turn thinking off. Servers that
    # don't understand the hint get a plain retry.
    try:
        text = await engine.chat(
            messages, max_tokens=max_tokens, temperature=0.2,
            # extra_body, not a bare kwarg — the OpenAI SDK rejects unknown arguments.
            extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    except Exception as e:
        logger.warning(f"no-thinking hint rejected ({e}); retrying without it")
        text = await engine.chat(messages, max_tokens=max_tokens, temperature=0.2)
    return (text or "").strip()
