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
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import paths

logger = logging.getLogger("letclaw.session")

# The shared memory file must not interleave two half-written handoffs, and two
# sessions archiving at once must not interleave two half-written index lines.
_handoff_lock = asyncio.Lock()
_index_lock = asyncio.Lock()

# The session index, alongside the transcripts it indexes.
INDEX_NAME = "index.jsonl"

# The registry of session ids in use, beside the main log. Rewritten in full on
# every change rather than appended to, because an id has to be able to *leave*
# it when its session is deleted — and the file holds one short line per live
# session, so there is nothing to gain by being cleverer.
SESSION_ID_LOG = "session_ID.log"
DEFAULT_LOG_DIR = "logs"
_ID_LOG_HEADER = "# session_id\tname\tcreated"
_id_log_lock = asyncio.Lock()

# Anchor relative paths to the repo, not the working directory, so the app
# behaves the same however it was launched. This module lives in source/, so the
# root is a level up — paths.REPO_ROOT is the one place that knows it.
REPO_DIR = paths.REPO_ROOT

DEFAULT_SESSIONS_DIR = "state/sessions"
DEFAULT_LONG_TERM = "state/memory_store/long_term.md"
DEFAULT_LIVE_DIR = "state/live"

LIVE_SCHEMA = 1     # bumped if the payload shape changes; an older file is skipped

_SUMMARISER_SYSTEM = (
    "You compress a conversation so it can continue in a fresh context window. "
    "Be terse and factual. Keep concrete details — file paths, names, numbers, "
    "decisions already made. Invent nothing. Add no sections beyond the four asked for."
)

# RULED OUT earns its place: without it a dead end is neither "settled" nor
# "still to do", so it survives nowhere and the next window walks straight back
# into it. Losing a fact costs a re-derivation; losing a rejection costs a loop.
# The same reasoning is why base.md's checkpoint asks for "every approach you
# ruled out and why" — this is that rule applied one level up.
_HANDOFF_FORMAT = """OBJECTIVE: the user's current goal, one sentence
ESTABLISHED: bullet list of decisions and facts already settled
RULED OUT: bullet list of approaches tried and rejected, each with why — so they are not tried again
OPEN: bullet list of what still needs doing"""


def _resolve(path, default):
    """Config paths are repo-relative unless absolute."""
    return paths.resolve(path, default)


# How much of each tool call's arguments reaches the summariser. Enough to
# identify what was run — the path, the pattern, the command — without one long
# argument blob crowding out the conversation around it.
_CALL_ARGS_CHARS = 200


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


def log_dir(config=None):
    """Where log files live — the directory holding logging.file."""
    config = config or {}
    configured = (config.get("logging") or {}).get("file")
    return _resolve(str(Path(configured).parent) if configured else None,
                    DEFAULT_LOG_DIR)


def id_log_path(config=None):
    return log_dir(config) / SESSION_ID_LOG


def _read_id_log(path):
    """[(id, name, created)] — header and unparseable lines skipped.

    Tab-separated, not whitespace: session names may contain spaces ("solaris
    daq"), so splitting on runs of whitespace would cut them in half.
    """
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            out.append((parts[0].strip(), parts[1], parts[2].strip()))
    return out


def _write_id_log(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([_ID_LOG_HEADER] + ["\t".join(r) for r in records])
                    + "\n", encoding="utf-8")


async def known_ids(config=None):
    """Every session id currently registered — what a new id must avoid."""
    path = id_log_path(config)
    async with _id_log_lock:
        return {r[0] for r in await asyncio.to_thread(_read_id_log, path)}


async def register_id(session_id, name, config=None):
    """Record a session id, or update the name recorded against it.

    An upsert rather than an append, because a rename has to be reflected here:
    an entry still naming the old session is worse than no entry, since the
    registry's other job is telling you which conversation an id belongs to.
    The original creation time survives an update.
    """
    field = " ".join(str(name).split())   # no tabs or newlines inside a field
    path = id_log_path(config)

    def _upsert():
        records = _read_id_log(path)
        created = next((r[2] for r in records if r[0] == session_id), None)
        kept = [r for r in records if r[0] != session_id]
        kept.append((session_id, field,
                     created or datetime.now().isoformat(timespec="seconds")))
        _write_id_log(path, kept)

    async with _id_log_lock:
        await asyncio.to_thread(_upsert)


async def unregister_id(session_id, config=None):
    """Drop a session id from the registry. True if it was there.

    Called when a session is deleted. The id returns to the pool — with 32 bits
    the chance of it ever being drawn again is ~1 in 4.3 billion, so the
    archives the deleted session left behind are in no practical danger of being
    joined by an unrelated conversation's.
    """
    path = id_log_path(config)

    def _remove():
        records = _read_id_log(path)
        kept = [r for r in records if r[0] != session_id]
        if len(kept) == len(records):
            return False
        _write_id_log(path, kept)
        return True

    async with _id_log_lock:
        return await asyncio.to_thread(_remove)


def sessions_dir(config=None):
    """Where archived transcripts and the index live."""
    config = config or {}
    return _resolve(config.get("conversation", {}).get("sessions_dir"),
                    DEFAULT_SESSIONS_DIR)


async def append_index(record, config=None):
    """Append one record to the session index. Returns its path.

    JSON Lines rather than a JSON document, for three reasons: appending never
    rewrites what is already there, so two sessions archiving at the same moment
    cannot lose each other's entry; a torn write costs one line instead of the
    file; and it is greppable, which is the point — this is meant to be searched
    from the shell as much as read by the code.

    The index exists because a filename is not an identity. A session can be
    renamed, and transcripts written before the rename keep the old name for
    ever — correct, since they record what it was called at the time, but it
    leaves the archives of one conversation scattered under two names with
    nothing joining them. `id` is what joins them.
    """
    line = json.dumps(record, ensure_ascii=False) + "\n"
    directory = sessions_dir(config)
    path = directory / INDEX_NAME

    def _append():
        directory.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

    async with _index_lock:
        await asyncio.to_thread(_append)
    return path


async def save_transcript(history, model_name, config=None, session_name="session",
                          session_id=None):
    """Write the full message list to the sessions dir. Returns the path.

    Named <session_id>_<session>_<timestamp>.json, stamped at the moment of the
    rollover. The id leads because it is the half that cannot change: a session
    can be renamed, so grouping by name scatters one conversation across two
    prefixes, while `ls <id>_*` finds every archive of it whatever it was called
    at the time. The name is still there because an id alone tells you nothing
    at a glance, and the timestamp sorts within a conversation.

    The name is not disambiguated: two rollovers of one session inside the same
    second would write the same file twice and the second would win. That takes
    a rollover whose handoff never runs — the model server unreachable, or no
    headroom left to summarise — and is accepted as not worth guarding.

    Serialising a full history can be megabytes at a 262144-token window, so the
    encode and the write both happen off the event loop — otherwise one rollover
    stalls every other session's token stream.
    """
    directory = sessions_dir(config)
    saved_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "saved_at": saved_at,
        "session": session_name,
        "session_id": session_id,
        "model": model_name,
        "messages": history,
    }
    # A caller with no id at all still has to produce a sortable, id-shaped
    # prefix rather than a ragged name — in practice only roll_over writes here,
    # and it always passes one.
    sid = _safe(str(session_id)) if session_id else "0" * 8
    path = directory / f"{sid}_{_safe(session_name)}_{_stamp()}.json"

    def _write():
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        return path

    final = await asyncio.to_thread(_write)
    # The transcript is the artifact; the index is a convenience over it. Losing
    # the index must never cost the archive, so this failure only warns.
    try:
        await append_index({"t": "archive", "id": session_id, "name": session_name,
                            "file": final.name, "at": saved_at, "model": model_name,
                            "messages": len(history)}, config)
    except OSError as e:
        logger.warning("could not update the session index: %s", e)
    return final


# ---- the journal: everything the session generated, as it happened ---------

# One block cannot be allowed to fill the disk: `cat` on a 500 MB file is one
# tool call. Generous enough that nothing a person would actually run gets
# clipped, small enough that a runaway costs a megabyte rather than the volume.
DEFAULT_JOURNAL_BLOCK_CHARS = 1_000_000


class Journal:
    """An append-only Markdown record of one session segment.

    The JSON archive beside it is a snapshot of the *message list*, and two
    things are missing from it that cannot be recovered afterwards: the model's
    reasoning, which core.py never puts in history at all, and the untruncated
    tool output, clipped to max_output_chars before it becomes a role="tool"
    message. A window that has rolled over can therefore see what the previous
    one concluded but not what it tried — so it tries it again.

    This file is the other half. It is never read back into a conversation; its
    readers are a person at a shell and the model itself, through exec. That is
    why it is line-oriented Markdown and not JSON: `read_file` has no offset
    argument and truncates at 8000 characters, so a 400 KB JSON archive is
    unreachable to the model whatever it is told about it, while a file with one
    header per line is reachable at any size through grep and sed.

    Every header is a single line beginning "## ", naming turn, round and kind:

        ## turn 12 round 3 tool_call exec call_a1b2  [16:04:11]

    so `grep -n '^## turn'` is a table of contents, `grep -n 'tool_call exec'`
    is every command the session ran, and sed pages a block under the cap.

    The file is stamped at the first write, not at construction: a session
    nobody speaks in leaves nothing behind. Writes never raise — losing the
    record is worth a log line, never a turn — and one failure closes the
    segment rather than warning once per block for the rest of the session.
    """

    def __init__(self, session_id, session_name, model_name, config=None):
        self.session_id = session_id
        self.session_name = session_name
        self.model_name = model_name
        self.config = config or {}
        self.path = None       # opened lazily; see the class docstring
        self.blocks = 0
        self.max_block = int((self.config.get("conversation", {})
                              .get("journal_max_block_chars")
                              or DEFAULT_JOURNAL_BLOCK_CHARS))
        # Serialises this journal's own appends. Not the index lock: a block
        # write must not queue behind another session archiving.
        self._lock = asyncio.Lock()
        self._broken = False

    def _open(self):
        """Create the segment file and write its header. Returns the path.

        Named like save_transcript's archive and for the same reasons — id
        first because it survives a rename, name for legibility, timestamp to
        sort — but .md, because this one is meant to be grepped.
        """
        directory = sessions_dir(self.config)
        directory.mkdir(parents=True, exist_ok=True)
        sid = _safe(str(self.session_id)) if self.session_id else "0" * 8
        stem = f"{sid}_{_safe(self.session_name)}_{_stamp()}"
        # Unlike save_transcript, this one must disambiguate. The stamp has
        # second resolution and a rollover closes one segment and opens the next
        # immediately, so two of them inside one second is ordinary rather than
        # a failure — and colliding here would not overwrite the closed segment
        # but silently *append* the new window to the end of it.
        path = directory / f"{stem}.md"
        n = 2
        while path.exists():
            path = directory / f"{stem}-{n}.md"
            n += 1
        path.write_text(
            f"# {self.session_name} — {self.model_name}\n\n"
            f"session_id: `{self.session_id}`  \n"
            f"opened: {datetime.now().isoformat(timespec='seconds')}\n\n"
            "Every model round, every tool call and every tool result, in full.\n"
            "Search with `grep -n '^## turn'` for the contents.\n",
            encoding="utf-8")
        return path

    async def write(self, header, body=""):
        """Append one block. Never raises."""
        if self._broken:
            return
        body = (body or "").strip("\n")
        if len(body) > self.max_block:
            half = self.max_block // 2
            body = (f"{body[:half]}\n...[journal: {len(body) - self.max_block} "
                    f"chars omitted]...\n{body[-half:]}")
        stamp = datetime.now().strftime("%H:%M:%S")
        blob = f"\n## {header}  [{stamp}]\n"
        if body:
            blob += f"\n{body}\n"

        def _append():
            if self.path is None:
                self.path = self._open()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(blob)

        try:
            async with self._lock:
                await asyncio.to_thread(_append)
            self.blocks += 1
        except OSError as e:
            self._broken = True
            logger.warning("journal for %s: closing this segment early: %s",
                           self.session_name, e)

    async def close(self):
        """Finish the segment and index it. Returns its path, or None.

        Resets, so the same Journal opens a fresh file on its next write — a
        rollover ends one segment and begins the next without the caller having
        to rebuild anything.
        """
        path, blocks = self.path, self.blocks
        if path is None:
            return None
        await self.write("segment closed", f"{blocks} blocks recorded")
        self.path, self.blocks, self._broken = None, 0, False
        # As in save_transcript: the record is the artifact and the index is a
        # convenience over it, so failing to index only warns.
        try:
            await append_index({"t": "journal", "id": self.session_id,
                                "name": self.session_name, "file": path.name,
                                "at": datetime.now().isoformat(timespec="seconds"),
                                "model": self.model_name, "blocks": blocks},
                               self.config)
        except OSError as e:
            logger.warning("could not index the journal: %s", e)
        return path


async def append_handoff(text, model_name, transcript_path, config=None,
                         session_name="session", journal_path=None):
    """Append a handoff under a timestamped heading in the long-term memory file.

    Every session appends to the same file, so the heading names the session —
    otherwise a reader cannot tell which conversation a block came from — and a
    lock keeps two concurrent handoffs from interleaving mid-write.
    """
    config = config or {}
    path = _resolve(config.get("memory", {}).get("long_term_file"), DEFAULT_LONG_TERM)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = [f"\n## {stamp}  [{session_name}]  ({model_name})\n"]
    if journal_path:
        entry.append(f"\nRecord: `{journal_path}`\n")
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


def _render_call(call):
    """`name(arguments)` for the summariser, arguments clipped."""
    fn = call.get("function") or {}
    name = fn.get("name", "?")
    args = (fn.get("arguments") or "").strip()
    if not args or args == "{}":
        return f"{name}()"
    if len(args) > _CALL_ARGS_CHARS:
        args = args[:_CALL_ARGS_CHARS - 1] + "…"
    return f"{name}({args})"


def render_transcript(history):
    """Flatten a history to plain text for the summariser.

    Tool calls keep their arguments, clipped. A result without the call that
    produced it cannot be summarised into a RULED OUT line: "no matches found"
    says nothing unless you also know what was searched for. Names alone were
    enough when the handoff only had to say what happened, not what was tried.
    """
    lines = []
    for msg in history:
        content = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []
        if calls:
            rendered = ", ".join(_render_call(c) for c in calls)
            content = f"{content}\n[called tools: {rendered}]".strip()
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


def format_carryover(handoff, transcript_path, journal_path=None):
    """The block appended to the new session's system prompt.

    The handoff says what the last window concluded; this says where to find
    what it actually did. The two are not interchangeable — a four-section
    summary cannot carry the command that produced a result, and it is the
    commands that stop the next window repeating the work.

    The instruction is to grep, not to read_file, because read_file cannot do
    it: no offset argument, and a hard truncation at max_output_chars (8000 by
    default), against records that run to hundreds of kilobytes. Telling the
    model to read_file a file it can only ever see 2% of is worse than telling
    it nothing — it looks, sees a middle replaced by an elision marker, and
    concludes the detail is not there.
    """
    parts = []
    if handoff:
        parts.append(handoff)
    if journal_path:
        parts.append(
            f"The previous window's full record is at {journal_path} — every round's "
            "reasoning, every tool call and every tool result, untruncated.\n"
            "It is far too large for read_file (that tool truncates at "
            "max_output_chars and has no offset). Search it with exec:\n"
            f"  grep -n '^## turn' {journal_path}          # contents\n"
            f"  grep -n 'tool_call exec' {journal_path}    # every command run\n"
            f"  grep -n -i PATTERN {journal_path}          # then sed -n 'A,Bp' to read\n"
            "Before starting any investigation this handoff does not already answer, "
            "search it for what was tried. Do not repeat work it shows was done.")
    elif transcript_path:
        parts.append(f"The previous session's full transcript is at {transcript_path} — "
                     "read_file it if you need a detail this summary left out.")
    return "\n\n".join(parts)


async def build_handoff(engine, history, max_tokens=1500):
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
            f"Write a handoff using exactly these four sections:\n\n{_HANDOFF_FORMAT}")},
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
