"""tools.py — tool registry for the agent loop.

Each tool is a name + description + JSON parameter schema + async handler.
Handlers return strings, which are fed back to the model as role="tool"
messages. Output is truncated so one tool can't blow up the context.

Truncation happens in Tool.run, not in the handlers, so there is exactly one
place that knows the cap and exactly one place holding the full output before
it is thrown away. run_full() hands both halves back: the clipped string for
the model, the whole thing for the journal. A 300 KB grep result clipped to
8000 chars is a fine tool message and a useless record — the next window
searching the journal needs the matches that fell in the middle.
"""

import asyncio
import fnmatch
import json
import os
import re
import shutil
import signal
import time
from pathlib import Path

import config as config_lib
import paths

DEFAULT_MAX_OUTPUT = 8000

# Which tool calls one round may run at once, by name. A round's calls are
# issued together, before any of them has run, so none can depend on another's
# *output* — but some depend on another's *effect*, and that is the order that
# must survive: a read after the write that made the file, the second of two
# writes of one path, a read after the command that created what it reads.
# A read has no effect a later call can observe, so reads share a moment and
# three two-second reads take the two of the slowest rather than six.
# Everything not named here runs one at a time, in the order issued: `exec`
# because its effects are whatever the command does, `write_file` because two
# writes of one path have a winner, `show_image` because it copies a file even
# though it mostly looks like a read, `edit_file` because it is a write with
# better manners, `run_and_capture` because it is a shell command under a
# different name. A new read-only tool earns a name here; the loop in core.py
# never decides for itself.
PARALLEL_SAFE = frozenset({"read_file", "list_dir", "search_and_inspect",
                           "inspect_file", "inspect_symbol"})

# Every tool takes this, and no handler ever sees it: it is there so a client
# can say what the agent is doing while it does it. The Discord bridge is the
# one that needs it — it posts a turn once, at the end, so a tool round that
# the model did not introduce arrives as a blank gap between two paragraphs
# and no sign of what happened in between.
#
# Four words, because this one is paid once per tool and every tool schema goes
# into every prompt of every session, so a description that reads nicely here
# costs more in a week than the whole feature is worth. What a good reason
# looks like is said once, in models/base.md, which is sent once — spelling it
# out in the field as well would pay it per tool again. Most of the field's
# cost is not the wording at all — it is the key, the type and the `required`
# entry, once per tool, and no phrasing gets it back. The absolute figures
# live in docs/tokens.md and nowhere else, because they move with
# `exec_timeout` and with every tool added.
REASON_KEY = "reason"
REASON_PARAM = {"type": "string", "description": "Why, in one short phrase"}

# A picture bigger than this is not a plot, and state/ is not where it belongs.
MAX_IMAGE_BYTES = 25 * 1024 * 1024

# The name show_image hands back has to be one the WebUI's renderer will accept,
# and that renderer (IMG_OK in web/app.js) takes word characters, dots and
# hyphens only. Keeping the same shape here means a name this tool invents can
# never be a name the browser then refuses to draw.
_SAFE_NAME = re.compile(r"[^\w.-]+")

# Directories the search tools never enter: the dependency and generated trees
# that would drown any result, on top of the dot-directory rule that keeps
# .git, .venv, .tox, ... out already.
_SEARCH_SKIP = frozenset({"node_modules", "__pycache__", "venv", "target",
                          "dist", "build"})

# Two budgets so one search cannot become a whole-disk scan: a file bigger
# than this is not worth searching, and a tree bigger than this many files
# is reported as stopped rather than crawled to the end.
MAX_FILE_BYTES = 10_000_000
MAX_SEARCH_FILES = 20_000

# run_and_capture bounds each stream separately, so a 50 KB stderr cannot push
# every useful byte of stdout past the max_output_chars clip that would apply
# to the combined text.
CAPTURE_STREAM_CAP = 4000

# What a file's structure map is made of: top-level-ish definitions, imports
# and constants across the languages a working tree usually holds. A map for
# the model to navigate by, not a parse — a miss costs a follow-up read, a
# false positive costs one line of output.
_STRUCT_RX = re.compile(
    r"^\s{0,3}(?:"
    r"async\s+def\s+\w+|def\s+\w+|class\s+\w+"
    r"|function\s+\w+|(?:const|let|var|fn|func|struct|enum|impl|type|interface)\s+\w+"
    r"|package\s+[\w.]+|module\s+[\w.]+|namespace\s+\w+"
    r"|import\s+\w+|from\s+[\w.]+\s+import"
    r"|@[\w.]+|#include\s+|#define\s+\w+"
    r"|[A-Z_][A-Z0-9_]*\s*="
    r")"
)


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    half = limit // 2
    return f"{s[:half]}\n...[truncated {len(s) - limit} chars]...\n{s[-half:]}"


def _with_reason(parameters):
    """`parameters` plus the reason field, without touching the original.

    Required, which is the only lever there is: nothing validates a tool call's
    arguments, so listing it is a request to the model rather than a rule. A
    model that ignores it costs nothing — the clients fall back to the tool's
    name — which is why a missing reason is not an error anywhere.
    """
    out = dict(parameters)
    out["properties"] = {**(parameters.get("properties") or {}),
                         REASON_KEY: REASON_PARAM}
    out["required"] = [*(parameters.get("required") or []), REASON_KEY]
    return out


class Tool:
    def __init__(self, name, description, parameters, handler,
                 max_output=DEFAULT_MAX_OUTPUT):
        self.name = name
        self.handler = handler
        self.max_output = int(max_output)
        # A tool that declares its own `reason` means something by it and keeps
        # it; for every other tool this is the presentation-only one above, and
        # run_full drops it before the handler is called.
        self.drops_reason = REASON_KEY not in (parameters.get("properties") or {})
        self.spec = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": _with_reason(parameters) if self.drops_reason
                              else parameters,
            },
        }

    async def run_full(self, arguments: str) -> tuple[str, str]:
        """Execute with a JSON string of args. Returns (full, clipped).

        Both are always strings, and `clipped` is what the model is given.
        A caller that has nowhere to put the full text should use run().
        """
        try:
            kwargs = json.loads(arguments) if arguments else {}
            if not isinstance(kwargs, dict):
                raise ValueError("arguments must be a JSON object")
            if self.drops_reason:
                # Presentation only. The handler has no parameter for it, so
                # leaving it in would turn every call into a TypeError.
                kwargs.pop(REASON_KEY, None)
            out = str(await self.handler(**kwargs))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            out = f"invalid arguments: {e}"
        except Exception as e:
            out = f"error: {type(e).__name__}: {e}"
        return out, _truncate(out, self.max_output)

    async def run(self, arguments: str) -> str:
        """Execute with a JSON string of args. Always returns a string."""
        return (await self.run_full(arguments))[1]


def _kill_group(proc):
    """Kill the whole process group; fall back to the process if that fails."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _coerce_timeout(timeout, default: int) -> int:
    try:
        return int(timeout) if timeout else default
    except (TypeError, ValueError):
        return default


async def _run_shell(command: str, workdir: str, timeout: int):
    """One shell command, fully reaped.

    Shared by `exec` and `run_and_capture`: the process group and the two
    kills (timeout, cancel) are the part that must exist exactly once.
    Returns (timed_out, code, out, err), where timed_out is None when the
    command could not be started at all and `out` carries the error instead.
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group, so we can kill children too
        )
    except (OSError, ValueError) as e:
        return None, None, f"could not start command: {e}", ""
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return True, None, "", ""
    except asyncio.CancelledError:
        # Shutdown or interrupt: without this the shell keeps running, detached.
        _kill_group(proc)
        raise
    return (False, proc.returncode,
            out.decode(errors="replace").strip(),
            err.decode(errors="replace").strip())


def _collect_files(root: Path) -> tuple[list, bool]:
    """A tree's regular files, dot-directories and _SEARCH_SKIP dirs skipped.

    Returns the list and whether it stopped at MAX_SEARCH_FILES. Directory
    symlinks are not followed: a link loop would otherwise be an infinite
    crawl. Unreadable directories are skipped, not fatal — a search that dies
    on one permission error is a search that found nothing.
    """
    files: list[Path] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if (not e.name.startswith(".") and e.name not in _SEARCH_SKIP
                        and not e.is_symlink()):
                    stack.append(e)
            elif e.is_file():
                files.append(e)
                if len(files) >= MAX_SEARCH_FILES:
                    return files, True
    return files, False


def _read_lines(p: Path):
    """A file's lines, or None when it is not worth searching.

    The NUL check is on the head only: a binary that starts clean and ends
    dirty is rare, and reading every file to the end to find out would make
    the skip cost what it exists to avoid.
    """
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = p.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace").split("\n")


def _rel(p: Path, root: Path) -> str:
    """A path the model can act on: relative to the searched root if it can be."""
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def _line_count(s: str) -> int:
    """Lines the way an editor counts them: a trailing newline ends a line, it
    does not start an empty one."""
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def build_tools(cfg: dict | None = None, workdir: str | None = None,
                files_dir=None) -> list:
    """Create the enabled tools from the config's tools: section.

    workdir is the default working directory for exec and the base the model is
    told about. It matters in the core: the daemon's own cwd is not the shell cwd
    the user was in, so relative paths would otherwise resolve somewhere else.

    files_dir is webui.files_dir, resolved. It arrives as a parameter rather
    than being read here because this function is handed only the tools:
    section, and the one caller that has the whole config is the core.
    """
    cfg = cfg or {}
    base = str(Path(workdir).expanduser()) if workdir else "."
    pics = Path(files_dir) if files_dir else paths.resolve(paths.DEFAULT_FILES_DIR)
    if not cfg.get("enabled", False):
        return []
    allow = cfg.get("allow", ["exec", "read_file", "write_file", "list_dir",
                              "show_image", "search_and_inspect", "inspect_file",
                              "inspect_symbol", "edit_file", "run_and_capture"])
    # `allow: exec` is the natural way to write a one-item list and YAML hands
    # it back as a string, which set() would spell out into {'e','x','c'} —
    # four tool names that do not exist, and every real tool quietly gone.
    allow = {allow} if isinstance(allow, str) else set(allow or ())
    max_out = config_lib.int_or(cfg.get("max_output_chars", DEFAULT_MAX_OUTPUT),
                                DEFAULT_MAX_OUTPUT)
    exec_timeout = config_lib.int_or(cfg.get("exec_timeout", 120), 120)

    async def exec_(command: str, workdir: str | None = None, timeout: int | None = None) -> str:
        workdir = workdir or base
        timeout = _coerce_timeout(timeout, exec_timeout)
        timed_out, code, out_s, err_s = await _run_shell(command, workdir, timeout)
        if timed_out is None:
            return out_s
        if timed_out:
            return f"timed out after {timeout}s (command killed)"
        parts = [f"exit code: {code}"]
        if out_s:
            parts.append(f"stdout:\n{out_s}")
        if err_s:
            parts.append(f"stderr:\n{err_s}")
        return "\n\n".join(parts)

    # These three touch the disk. In the shared core one slow read would stall every
    # session's token stream, so the blocking part runs off the event loop.
    def _read_file(path: str) -> str:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"error: not a file: {path}"
        return p.read_text(encoding="utf-8", errors="replace")

    async def read_file(path: str) -> str:
        return await asyncio.to_thread(_read_file, path)

    def _write_file(path: str, content: str) -> str:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {p}"

    async def write_file(path: str, content: str) -> str:
        return await asyncio.to_thread(_write_file, path, content)

    def _list_dir(path: str = ".") -> str:
        p = Path(path).expanduser()
        if not p.is_dir():
            return f"error: not a directory: {path}"
        lines = []
        for e in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if e.is_dir():
                lines.append(f"{e.name}/")
            else:
                lines.append(f"{e.name}  ({e.stat().st_size} bytes)")
        return "\n".join(lines) or "(empty)"

    async def list_dir(path: str = ".") -> str:
        return await asyncio.to_thread(_list_dir, path)

    def _show_image(path: str, name: str | None = None) -> str:
        """Copy an image into files_dir so the WebUI can serve it.

        This is the boundary. `exec` and `read_file` already reach any path this
        process can; this is the only tool that moves a file from that
        unconfined side to the side reachable over HTTP by anyone holding
        core.token. That is the whole reason it copies rather than, say,
        registering a path: what is servable stays exactly "what is in one
        directory", which is the invariant /files is built on.
        """
        src = Path(path).expanduser()
        try:
            src = src.resolve()
        except (OSError, ValueError):
            return f"error: cannot resolve {path}"
        if not src.is_file():
            return f"error: not a file: {path}"
        ext = src.suffix.lower()
        if ext not in paths.IMAGE_TYPES:
            # Refused here rather than at the route, where it would surface as
            # a tile that will not load and no reason why.
            return (f"error: {src.name} is not a servable image. "
                    f"Convert it first — these work: "
                    f"{', '.join(sorted(paths.IMAGE_TYPES))}")
        size = src.stat().st_size
        if size > MAX_IMAGE_BYTES:
            return (f"error: {src.name} is {size // 1048576} MB, over the "
                    f"{MAX_IMAGE_BYTES // 1048576} MB limit. Downsample it first.")

        stem = _SAFE_NAME.sub("-", Path(name).name if name else src.name).strip(".-")
        if not stem:
            stem = "image"
        if not stem.lower().endswith(ext):
            stem += ext
        pics.mkdir(parents=True, exist_ok=True)
        dest = pics / stem
        # files_dir is shared by every session, so overwriting a name that is
        # already taken would let one conversation silently replace a picture
        # another is still showing. Suffix instead -- unless it is byte-identical,
        # in which case the copy that is already there is the answer.
        if dest.exists():
            if dest.stat().st_size == size and dest.read_bytes() == src.read_bytes():
                return f"{dest.name} (already there) — now write ![…]({dest.name}) to show it"
            base, n = dest.stem, 2
            while dest.exists():
                dest = pics / f"{base}-{n}{ext}"
                n += 1
        shutil.copyfile(src, dest)
        return f"{dest.name} — now write ![a caption]({dest.name}) to show it"

    async def show_image(path: str, name: str | None = None) -> str:
        return await asyncio.to_thread(_show_image, path, name)

    def _search_and_inspect(pattern: str | None = None, root: str = ".",
                            file_pattern: str | None = None, literal: bool = False,
                            max_matches: int = 50, inspect: int = 3,
                            context: int = 2) -> str:
        if not pattern and not file_pattern:
            return "error: give a pattern (file contents) or a file_pattern (file names)"
        try:
            max_matches = max(1, int(max_matches))
            inspect = max(0, int(inspect))
            context = max(0, int(context))
        except (TypeError, ValueError):
            return "error: max_matches, inspect and context must be integers"
        root_p = Path(root).expanduser()
        if not root_p.is_dir():
            return f"error: not a directory: {root}"
        files, files_stopped = _collect_files(root_p)
        candidates = [f for f in files
                      if not file_pattern or fnmatch.fnmatch(f.name, file_pattern)]
        if not pattern:
            # Name search: the glob is the query.
            hits = sorted(candidates, key=lambda f: _rel(f, root_p))[:max_matches]
            out = [f"{len(hits)} file(s) named like '{file_pattern}' "
                   f"(scanned {len(files)} files)"]
            out += [f"{_rel(f, root_p)}  ({f.stat().st_size} bytes)" for f in hits]
            if len(candidates) > max_matches:
                out.append(f"[{len(candidates) - max_matches} more — narrow the file_pattern]")
            if files_stopped:
                out.append(f"[stopped after {MAX_SEARCH_FILES} files — search a narrower root]")
            return "\n".join(out)
        if not literal:
            try:
                rx = re.compile(pattern)
            except re.error as e:
                return f"error: invalid regex: {e}"
        matches: list[tuple] = []
        for f in candidates:
            lines = _read_lines(f)
            if lines is None:
                continue
            for i, line in enumerate(lines, 1):
                if (line.count(pattern) if literal else rx.search(line)):
                    matches.append((f, i, line.strip()[:200]))
                    if len(matches) >= max_matches:
                        break
            if len(matches) >= max_matches:
                break
        if not matches:
            # Say so when the walk hit its ceiling: "no matches" over a tree
            # that was only partly read is a wrong answer, and it is the kind
            # an agent will act on. The cap is reported on the match path
            # above; it has to be reported here too, where it matters most.
            return (f"no matches in {len(candidates)} files "
                    f"(scanned {len(files)} files)"
                    + (f" — [stopped after {MAX_SEARCH_FILES} files, the rest "
                       f"was never read; search a narrower root]" if files_stopped
                       else ""))
        matches.sort(key=lambda m: (_rel(m[0], root_p), m[1]))
        out = [f"{len(matches)} match(es) in {len({m[0] for m in matches})} file(s) "
               f"(scanned {len(files)} files)"]
        for k, (f, ln, snippet) in enumerate(matches):
            out.append(f"{_rel(f, root_p)}:{ln}: {snippet}")
            # The first `inspect` matches earn context, re-reading that file.
            if context and k < min(inspect, len(matches)):
                lines = _read_lines(f)
                if lines:
                    for j in range(max(1, ln - context), min(len(lines), ln + context) + 1):
                        if j != ln:
                            out.append(f"    {j}: {lines[j - 1][:200]}")
        if len(matches) >= max_matches:
            out.append(f"[stopped at max_matches={max_matches} — narrow the pattern]")
        if files_stopped:
            out.append(f"[stopped after {MAX_SEARCH_FILES} files — search a narrower root]")
        return "\n".join(out)

    async def search_and_inspect(pattern: str | None = None, root: str = ".",
                                 file_pattern: str | None = None,
                                 literal: bool = False, max_matches: int = 50,
                                 inspect: int = 3, context: int = 2) -> str:
        return await asyncio.to_thread(_search_and_inspect, pattern, root,
                                       file_pattern, literal, max_matches,
                                       inspect, context)

    def _inspect_file(path: str, lines: str | None = None,
                      pattern: str | None = None, context: int = 2) -> str:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"error: not a file: {path}"
        try:
            raw = p.read_bytes()
        except OSError as e:
            return f"error: cannot read {path}: {e}"
        text = raw.decode("utf-8", errors="replace")
        all_lines = text.split("\n")
        if all_lines and all_lines[-1] == "":
            all_lines.pop()
        n = len(all_lines)
        if n == 0:
            return f"{p} — 0 lines (empty file)"
        out = [f"{p} — {n} lines, {len(raw) // 1024} KB"]
        struct = [f"{i}: {ln.strip()[:120]}"
                  for i, ln in enumerate(all_lines, 1) if _STRUCT_RX.match(ln)]
        out.append("structure:")
        out += struct[:150]
        if len(struct) > 150:
            out.append(f"… ({len(struct) - 150} more — read the range you need)")
        if lines is not None:
            m = re.fullmatch(r"\s*(\d+)\s*(?:,\s*(\d+)\s*)?", str(lines))
            if not m:
                return f"error: lines is like '120' or '120,180', not {lines!r}"
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            if a > b:
                a, b = b, a
            a, b = max(1, a), min(n, b)
            if b - a + 1 > 1000:
                out.append(f"lines {a}-{a + 999} (capped — ask for a narrower range):")
                b = a + 999
            else:
                out.append(f"lines {a}-{b}:")
            out += [f"{i}: {all_lines[i - 1]}" for i in range(a, b + 1)]
        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error as e:
                return f"error: invalid regex: {e}"
            try:
                ctx = max(0, int(context))
            except (TypeError, ValueError):
                ctx = 2
            hits = [i for i, ln in enumerate(all_lines, 1) if rx.search(ln)][:50]
            if not hits:
                out.append(f"no lines match {pattern!r}")
            else:
                out.append(f"pattern matches ({len(hits)} of {n} lines):")
                shown = set()
                for i in hits:
                    for j in range(max(1, i - ctx), min(n, i + ctx) + 1):
                        if j in shown:
                            continue
                        shown.add(j)
                        mark = ">" if j == i else " "
                        out.append(f"{mark} {j}: {all_lines[j - 1][:300]}")
        return "\n".join(out)

    async def inspect_file(path: str, lines: str | None = None,
                           pattern: str | None = None, context: int = 2) -> str:
        return await asyncio.to_thread(_inspect_file, path, lines, pattern, context)

    def _edit_file(path: str, old_string: str, new_string: str,
                   replace_all: bool = False) -> str:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"error: not a file: {path}"
        if not old_string:
            return "error: old_string is empty — name the exact block to replace"
        try:
            text = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return f"error: {p} is not valid UTF-8 text — use exec"
        except OSError as e:
            return f"error: cannot read {path}: {e}"
        n = text.count(old_string)
        if n == 0:
            return ("error: no match — the file may have changed since you last "
                    "read it. Re-read it and try again.")
        if n > 1 and not replace_all:
            return (f"error: {n} matches — add surrounding context to old_string, "
                    "or pass replace_all=true to change every one")
        new = (text.replace(old_string, new_string) if replace_all
               else text.replace(old_string, new_string, 1))
        # The check above is the safety; this write is the atomics — a crash
        # between them leaves the old file, never a half-edited one.
        paths.atomic_write(p, new)
        return (f"replaced {n if replace_all else 1} occurrence(s) in {p} "
                f"({_line_count(text)} → {_line_count(new)} lines)")

    async def edit_file(path: str, old_string: str, new_string: str,
                        replace_all: bool = False) -> str:
        return await asyncio.to_thread(_edit_file, path, old_string, new_string,
                                       replace_all)

    def _inspect_symbol(symbol: str, root: str = ".",
                        file_pattern: str | None = None, max_refs: int = 25,
                        case_sensitive: bool = True) -> str:
        symbol = str(symbol).strip()
        if not symbol:
            return "error: symbol is empty"
        try:
            max_refs = max(1, int(max_refs))
        except (TypeError, ValueError):
            return "error: max_refs must be an integer"
        root_p = Path(root).expanduser()
        if not root_p.is_dir():
            return f"error: not a directory: {root}"
        files, files_stopped = _collect_files(root_p)
        candidates = [f for f in files
                      if not file_pattern or fnmatch.fnmatch(f.name, file_pattern)]
        # A symbol is a spelling: FooBar is not foobar, and conflating the two
        # would list a different name's definition and references under the
        # one asked for. case_sensitive=false is the opt-out for a name the
        # caller is not sure how to spell.
        flags = 0 if case_sensitive else re.IGNORECASE
        def_rx = re.compile(
            r"^\s*(?:(?:async\s+)?def\s+|class\s+|function\s+"
            r"|(?:const|let|var|fn|func|type)\s+)" + re.escape(symbol) + r"\b",
            flags)
        ref_rx = re.compile(r"\b" + re.escape(symbol) + r"\b", flags)
        defs: list[str] = []
        refs: list[str] = []
        for f in candidates:
            lines = _read_lines(f)
            if lines is None:
                continue
            rel = _rel(f, root_p)
            for i, line in enumerate(lines, 1):
                if def_rx.match(line):
                    defs.append(f"{rel}:{i}: {line.strip()[:200]}")
                elif ref_rx.search(line) and len(refs) < max_refs:
                    refs.append(f"{rel}:{i}: {line.strip()[:200]}")
        if not defs and not refs:
            return (f"no definition or references for '{symbol}' in "
                    f"{len(candidates)} files (scanned {len(files)} files)")
        out = []
        if defs:
            out.append(f"definition ({len(defs)}):")
            out += defs
        else:
            out.append("definition: none found (references only — it may be imported)")
        out.append(f"references ({len(refs)}):")
        out += refs
        if files_stopped:
            out.append(f"[stopped after {MAX_SEARCH_FILES} files — search a narrower root]")
        return "\n".join(out)

    async def inspect_symbol(symbol: str, root: str = ".",
                             file_pattern: str | None = None, max_refs: int = 25,
                             case_sensitive: bool = True) -> str:
        return await asyncio.to_thread(_inspect_symbol, symbol, root,
                                       file_pattern, max_refs, case_sensitive)

    async def run_and_capture(command: str, workdir: str | None = None,
                              timeout: int | None = None) -> str:
        workdir = workdir or base
        timeout = _coerce_timeout(timeout, exec_timeout)
        t0 = time.monotonic()
        timed_out, code, out_s, err_s = await _run_shell(command, workdir, timeout)
        if timed_out is None:
            return out_s
        if timed_out:
            return f"timed out after {timeout}s (command killed)"
        parts = [f"exit: {code} ({time.monotonic() - t0:.1f}s)"]
        for label, s in (("stdout", out_s), ("stderr", err_s)):
            if s:
                parts.append(f"{label} ({s.count(chr(10)) + 1} lines):\n"
                             f"{_truncate(s, CAPTURE_STREAM_CAP)}")
        return "\n\n".join(parts)

    tools = {
        "exec": Tool(
            "exec",
            "Run a shell command and return its exit code, stdout and stderr.",
            {"type": "object",
             "properties": {
                 "command": {"type": "string", "description": "Shell command to run"},
                 "workdir": {"type": "string",
                             "description": "Working directory (default: current dir)"},
                 "timeout": {"type": "integer",
                             "description": f"Max seconds to wait (default: {exec_timeout})"}},
             "required": ["command"]},
            exec_, max_out,
        ),
        "read_file": Tool(
            "read_file",
            "Read a text file and return its content.",
            {"type": "object",
             "properties": {"path": {"type": "string", "description": "File path"}},
             "required": ["path"]},
            read_file, max_out,
        ),
        "write_file": Tool(
            "write_file",
            "Create or overwrite a text file. Parent directories are created automatically.",
            {"type": "object",
             "properties": {
                 "path": {"type": "string", "description": "File path"},
                 "content": {"type": "string", "description": "Full file content"}},
             "required": ["path", "content"]},
            write_file, max_out,
        ),
        "list_dir": Tool(
            "list_dir",
            "List directory entries: directories first (with /), then files with sizes.",
            {"type": "object",
             "properties": {"path": {"type": "string",
                                     "description": "Directory path (default: current dir)"}},
             "required": []},
            list_dir, max_out,
        ),
        # Terse on purpose: every tool schema is paid by every session on every
        # turn, so a sentence here costs more over a week than the handler does
        # in a year. See docs/tokens.md.
        "show_image": Tool(
            "show_image",
            "Copy an image file where the user can see it. Returns the name to "
            "put in ![caption](name) in your answer.",
            {"type": "object",
             "properties": {
                 "path": {"type": "string", "description": "Path to the image"},
                 "name": {"type": "string",
                          "description": "Name to show it under (default: its own)"}},
             "required": ["path"]},
            show_image, max_out,
        ),
        # Descriptions follow show_image's discipline: every word here is in
        # every prompt of every session. docs/tokens.md holds the measured cost.
        "search_and_inspect": Tool(
            "search_and_inspect",
            "Search a directory: matching lines in file contents (regex, or "
            "exact with literal) and matching file names (file_pattern). "
            "Returns path:line: snippets; the first matches get context.",
            {"type": "object",
             "properties": {
                 "pattern": {"type": "string",
                             "description": "Regex to match in file contents"},
                 "root": {"type": "string",
                          "description": "Directory to search (default: current dir)"},
                 "file_pattern": {"type": "string",
                                  "description": "File-name glob; alone, it searches names"},
                 "literal": {"type": "boolean",
                             "description": "Exact text, not regex (default: false)"},
                 "max_matches": {"type": "integer",
                                 "description": "Stop after this many (default: 50)"},
                 "inspect": {"type": "integer",
                             "description": "Matches shown with context (default: 3)"},
                 "context": {"type": "integer",
                             "description": "Context lines (default: 2)"}},
             "required": []},
            search_and_inspect, max_out,
        ),
        "inspect_file": Tool(
            "inspect_file",
            "Understand a file without reading all of it: its structure "
            "(top-level defs, classes, constants) plus a line range and/or "
            "pattern matches with context.",
            {"type": "object",
             "properties": {
                 "path": {"type": "string", "description": "File path"},
                 "lines": {"type": "string",
                           "description": "Line or range, e.g. '120' or '120,180'"},
                 "pattern": {"type": "string",
                             "description": "Regex; matching lines get context"},
                 "context": {"type": "integer",
                             "description": "Context lines (default: 2)"}},
             "required": ["path"]},
            inspect_file, max_out,
        ),
        "inspect_symbol": Tool(
            "inspect_symbol",
            "Find a function, class or variable: its definition line and its "
            "references, across a directory.",
            {"type": "object",
             "properties": {
                 "symbol": {"type": "string", "description": "The name to find"},
                 "root": {"type": "string",
                          "description": "Directory to search (default: current dir)"},
                 "file_pattern": {"type": "string",
                                  "description": "File-name glob to restrict the search"},
                 "max_refs": {"type": "integer",
                              "description": "Maximum references listed (default: 25)"},
                 "case_sensitive": {"type": "boolean",
                                    "description": "Exact case (default: true)"}},
             "required": ["symbol"]},
            inspect_symbol, max_out,
        ),
        "edit_file": Tool(
            "edit_file",
            "Replace an exact text block in an existing file. Refuses unless "
            "old_string matches exactly once (or replace_all). Safer than "
            "write_file for a small change.",
            {"type": "object",
             "properties": {
                 "path": {"type": "string", "description": "File path (must exist)"},
                 "old_string": {"type": "string",
                                "description": "Exact text to replace, whitespace-sensitive"},
                 "new_string": {"type": "string",
                                "description": "Replacement (empty to delete)"},
                 "replace_all": {"type": "boolean",
                                 "description": "Change every match (default: false)"}},
             "required": ["path", "old_string", "new_string"]},
            edit_file, max_out,
        ),
        "run_and_capture": Tool(
            "run_and_capture",
            "Run a shell command, returning exit code, stdout and stderr "
            "separately, each bounded — for output too large for exec.",
            {"type": "object",
             "properties": {
                 "command": {"type": "string", "description": "Shell command to run"},
                 "workdir": {"type": "string",
                             "description": "Working directory (default: current dir)"},
                 "timeout": {"type": "integer",
                             "description": f"Max seconds to wait (default: {exec_timeout})"}},
             "required": ["command"]},
            run_and_capture, max_out,
        ),
    }
    return [tools[k] for k in tools if k in allow]
