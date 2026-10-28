"""Where things live.

The modules sit in source/, but almost everything they need — config.yaml, the
behavior Markdown under models/, the web assets, state/ and logs/ — lives one
level up at the repo root. Anchoring to the repo rather than to the working
directory keeps the app behaving the same however it was launched, and keeping
that answer in one place means there is exactly one line to get wrong.
"""

import os
import stat
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
        elsewhere, and failing it is no reason to fail a write that landed;
      * the target's mode, copied onto the temp file before the rename, because
        rename replaces the mode along with the contents and mkstemp's 0600 is
        not the mode the file had.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The mode an existing target already carries. mkstemp hands back 0600 and
    # rename carries that to the target, so without this an edit to a 0755
    # script leaves it not executable, and a 0664 file a single user can read.
    # `write_text`, which this replaced, never did that: it opens the target in
    # place and so cannot see the mode at all. Set on the fd *before* the
    # rename, so there is no moment when the target is more locked down than it
    # was; a target that does not exist yet keeps mkstemp's 0600, which is the
    # right default for a new file holding state.
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            if mode is not None:
                os.fchmod(f.fileno(), mode)
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


def safe_name(name, limit=40, fallback="session"):
    """A session name flattened to something safe to put in a filename.

    Alphanumerics, dash, underscore and dot survive; everything else becomes
    an underscore. Lossy by design — 'my session', 'my/session' and
    'my+session' all come out 'my_session' — so it names a file, it never
    keys one.

    Here rather than in `session`, for the reason IMAGE_TYPES is here rather
    than in `server`: two modules need the same answer and neither should
    import the other for it. `session` uses it for the live file, the
    transcript and the journal stem; `behavior` uses it to turn a session
    name into `models/sessions/<name>.md`, which is the one place a session
    name becomes a path that is *read*. Those two had the character class
    written out separately, agreeing exactly, with a comment in `behavior`
    promising they would stay that way. The day the set widens — a space kept
    as a space, say, or a colon allowed because Linux takes one — it has to
    widen in both or a name reaches one module as itself and the other as a
    different string. The difference they are allowed to have is the two
    arguments.

    `limit` and `fallback` are where they legitimately differ. `session`
    truncates at 40 and substitutes 'session' for a name that flattened to
    nothing, because something has to go in the filename. `behavior` allows
    64 and asks for '' instead, because a name with nothing left in it means
    that session simply has no behavior file — and it rejects an all-dots or
    all-underscore slug on top of this, since '..' survives the character
    class intact.
    """
    return "".join(c if c.isalnum() or c in "-_." else "_"
                   for c in str(name))[:limit] or fallback


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
