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
import json
import os
import re
import shutil
import signal
from pathlib import Path

import config as config_lib
import paths

DEFAULT_MAX_OUTPUT = 8000

# Every tool takes this, and no handler ever sees it: it is there so a client
# can say what the agent is doing while it does it. The Discord bridge is the
# one that needs it — it posts a turn once, at the end, so a tool round that
# the model did not introduce arrives as a blank gap between two paragraphs
# and no sign of what happened in between.
#
# Four words, because this one is paid five times over: every tool schema goes
# into every prompt of every session, so a description that reads nicely here
# costs more in a week than the whole feature is worth. What a good reason
# looks like is said once, in models/base.md, which is sent once. Spelling it
# out here as well cost 174 tok/turn against the 114 this does, across the five
# default tools. Most of that 114 is not the wording at all — it is the key, the
# type and the `required` entry, five times over, and no phrasing gets it back.
# The absolute figures live in docs/tokens.md and nowhere else, because they
# move with `exec_timeout` and with every tool added.
REASON_KEY = "reason"
REASON_PARAM = {"type": "string", "description": "Why, in one short phrase"}

# A picture bigger than this is not a plot, and state/ is not where it belongs.
MAX_IMAGE_BYTES = 25 * 1024 * 1024

# The name show_image hands back has to be one the WebUI's renderer will accept,
# and that renderer (IMG_OK in web/app.js) takes word characters, dots and
# hyphens only. Keeping the same shape here means a name this tool invents can
# never be a name the browser then refuses to draw.
_SAFE_NAME = re.compile(r"[^\w.-]+")


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
                              "show_image"])
    # `allow: exec` is the natural way to write a one-item list and YAML hands
    # it back as a string, which set() would spell out into {'e','x','c'} —
    # four tool names that do not exist, and every real tool quietly gone.
    allow = {allow} if isinstance(allow, str) else set(allow or ())
    max_out = config_lib.int_or(cfg.get("max_output_chars", DEFAULT_MAX_OUTPUT),
                                DEFAULT_MAX_OUTPUT)
    exec_timeout = config_lib.int_or(cfg.get("exec_timeout", 120), 120)

    async def exec_(command: str, workdir: str | None = None, timeout: int | None = None) -> str:
        workdir = workdir or base
        try:
            timeout = int(timeout) if timeout else exec_timeout
        except (TypeError, ValueError):
            timeout = exec_timeout
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group, so we can kill children too
            )
        except (OSError, ValueError) as e:
            return f"could not start command: {e}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_group(proc)
            await proc.wait()
            return f"timed out after {timeout}s (command killed)"
        except asyncio.CancelledError:
            # Shutdown or interrupt: without this the shell keeps running, detached.
            _kill_group(proc)
            raise
        out_s = out.decode(errors="replace").strip()
        err_s = err.decode(errors="replace").strip()
        parts = [f"exit code: {proc.returncode}"]
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
    }
    return [tools[k] for k in tools if k in allow]
