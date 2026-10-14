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


def _abs(p: Path) -> Path:
    return p if p.is_absolute() else REPO_ROOT / p
