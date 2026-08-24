"""tools.py — tool registry for the agent loop.

Each tool is a name + description + JSON parameter schema + async handler.
Handlers return strings, which are fed back to the model as role="tool"
messages. Output is truncated so one tool can't blow up the context.
"""

import asyncio
import json
from pathlib import Path

DEFAULT_MAX_OUTPUT = 8000


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    half = limit // 2
    return f"{s[:half]}\n...[truncated {len(s) - limit} chars]...\n{s[-half:]}"


class Tool:
    def __init__(self, name, description, parameters, handler):
        self.name = name
        self.handler = handler
        self.spec = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }

    async def run(self, arguments: str) -> str:
        """Execute with a JSON string of args. Always returns a string."""
        try:
            kwargs = json.loads(arguments) if arguments else {}
            if not isinstance(kwargs, dict):
                raise ValueError("arguments must be a JSON object")
            return str(await self.handler(**kwargs))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return f"invalid arguments: {e}"
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"


def build_tools(cfg: dict | None = None) -> list:
    """Create the enabled tools from the config's tools: section."""
    cfg = cfg or {}
    if not cfg.get("enabled", False):
        return []
    allow = set(cfg.get("allow", ["exec", "read_file", "write_file", "list_dir"]))
    max_out = int(cfg.get("max_output_chars", DEFAULT_MAX_OUTPUT))
    exec_timeout = int(cfg.get("exec_timeout", 120))

    async def exec_(command: str, workdir: str = ".", timeout: int | None = None) -> str:
        try:
            timeout = int(timeout) if timeout else exec_timeout
        except (TypeError, ValueError):
            timeout = exec_timeout
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as e:
            return f"could not start command: {e}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"timed out after {timeout}s (command killed)"
        out_s = out.decode(errors="replace").strip()
        err_s = err.decode(errors="replace").strip()
        parts = [f"exit code: {proc.returncode}"]
        if out_s:
            parts.append(f"stdout:\n{out_s}")
        if err_s:
            parts.append(f"stderr:\n{err_s}")
        return _truncate("\n\n".join(parts), max_out)

    async def read_file(path: str) -> str:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"error: not a file: {path}"
        return _truncate(p.read_text(errors="replace"), max_out)

    async def write_file(path: str, content: str) -> str:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {p}"

    async def list_dir(path: str = ".") -> str:
        p = Path(path).expanduser()
        if not p.is_dir():
            return f"error: not a directory: {path}"
        lines = []
        for e in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if e.is_dir():
                lines.append(f"{e.name}/")
            else:
                lines.append(f"{e.name}  ({e.stat().st_size} bytes)")
        return _truncate("\n".join(lines) or "(empty)", max_out)

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
            exec_,
        ),
        "read_file": Tool(
            "read_file",
            "Read a text file and return its content.",
            {"type": "object",
             "properties": {"path": {"type": "string", "description": "File path"}},
             "required": ["path"]},
            read_file,
        ),
        "write_file": Tool(
            "write_file",
            "Create or overwrite a text file. Parent directories are created automatically.",
            {"type": "object",
             "properties": {
                 "path": {"type": "string", "description": "File path"},
                 "content": {"type": "string", "description": "Full file content"}},
             "required": ["path", "content"]},
            write_file,
        ),
        "list_dir": Tool(
            "list_dir",
            "List directory entries: directories first (with /), then files with sizes.",
            {"type": "object",
             "properties": {"path": {"type": "string",
                                     "description": "Directory path (default: current dir)"}},
             "required": []},
            list_dir,
        ),
    }
    return [tools[k] for k in tools if k in allow]
