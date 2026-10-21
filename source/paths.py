"""Where things live.

The modules sit in source/, but almost everything they need — config.yaml, the
behavior Markdown under models/, the web assets, state/ and logs/ — lives one
level up at the repo root. Anchoring to the repo rather than to the working
directory keeps the app behaving the same however it was launched, and keeping
that answer in one place means there is exactly one line to get wrong.
"""

import os
import tempfile
from pathlib import Path

# source/paths.py -> source/ -> the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent


def atomic_write(path, text, encoding="utf-8"):
    """Replace `path` with `text`, or leave the old contents untouched.

    A plain `write_text` truncates first and writes after, so a crash between
    the two leaves an empty or half-written file — and the things written this
    way are registries, where a torn write loses every record rather than one.

    The bytes land in a sibling temp file, are flushed and fsync'd, and only
    then rename over the target. Rename is atomic within a filesystem, which is
    why the temp file is a sibling and not in /tmp. Three details earn their
    keep:

      * a unique temp name, so two writers racing on one path cannot scribble
        into each other's buffer — the loser's rename simply happens second;
      * fsync on the file, so the rename cannot be reordered ahead of the data
        and expose a target full of zeros after a power cut;
      * fsync on the directory, so the rename itself survives one. That last is
        best-effort: it is meaningful on Linux and may not be permitted
        elsewhere, and failing it is no reason to fail a write that landed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leave the temp file behind: these directories are listed by
        # load_live and by `ls state/live`, and a drift of .tmp files is both
        # noise and a slow leak.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dirfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError:
        pass
    return path


def resolve(path, default=None):
    """A configured path: used as-is if absolute, otherwise repo-relative."""
    return _abs(Path(path or default).expanduser())


# Where the agent puts pictures it wants shown. Not under web/: that directory is
# the app's own source, served unauthenticated to anyone who loads the page, and
# generated output does not belong in it. state/ is already gitignored.
DEFAULT_FILES_DIR = "state/files"

# Served by extension, so a mistyped path cannot turn /files into a general
# arbitrary-file-read route for anyone holding the token.
#
# Here rather than in server.py because three modules now need the same answer:
# the route that serves a picture, the route that lists them, and the show_image
# tool that puts one there. tools.py cannot import server (server -> core ->
# tools), and two copies of this map would drift the first time one gained a
# format the other refused.
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".ico": "image/x-icon", ".avif": "image/avif",
}


def files_root(config):
    """The directory /files serves, as an absolute path."""
    configured = (config.get("webui", {}) or {}).get("files_dir") or DEFAULT_FILES_DIR
    return resolve(configured).resolve()


def _abs(p: Path) -> Path:
    return p if p.is_absolute() else REPO_ROOT / p
