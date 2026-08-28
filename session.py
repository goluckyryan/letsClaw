"""session.py — archive a conversation and distil it into a handoff.

When the context window fills up, chat.py rolls the session over: the full
transcript goes to disk, the model compresses what happened into an objective
plus open threads, and a fresh window is seeded from that.

This module owns the disk and summarisation side only. Assembling the new
history stays in chat.py, which owns new_history() and the system-prompt shape.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("letclaw.session")

# Anchor relative paths to the repo, not the working directory, so the app
# behaves the same however it was launched.
REPO_DIR = Path(__file__).parent

DEFAULT_SESSIONS_DIR = "state/sessions"
DEFAULT_LONG_TERM = "state/memory_store/long_term.md"

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


def save_transcript(history, model_name, config=None):
    """Write the full message list to the sessions dir. Returns the path."""
    config = config or {}
    directory = _resolve(config.get("conversation", {}).get("sessions_dir"),
                         DEFAULT_SESSIONS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_stamp()}.json"
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "model": model_name,
        "messages": history,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def append_handoff(text, model_name, transcript_path, config=None):
    """Append a handoff under a timestamped heading in the long-term memory file."""
    config = config or {}
    path = _resolve(config.get("memory", {}).get("long_term_file"), DEFAULT_LONG_TERM)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = [f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}  ({model_name})\n"]
    if transcript_path:
        entry.append(f"\nTranscript: `{transcript_path}`\n")
    entry.append(f"\n{text.strip()}\n")
    with open(path, "a") as f:
        f.write("".join(entry))
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
