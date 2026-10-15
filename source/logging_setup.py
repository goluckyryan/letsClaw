"""config.yaml's logging: section, wired up for one process.

The core and the Discord bot are separate processes reading the same
config.yaml, so they both resolved the same logging.file and appended to it.
Two writers in one file interleave their lines, and the file grew without a
ceiling — 500 KB in a few days here, with nothing that would ever stop it.

Rotation alone would have made the first half worse. Whichever process rolls
the file renames it, and the other goes on writing to an inode that is no
longer at that path: its log stops appearing, and the rolled copy it still
holds open is deleted out from under it at the next roll. So the writer's name
goes into the filename — logs/letclaw.log becomes letclaw-core.log and
letclaw-discord.log — and each process rotates only its own. One file per
writer is what makes rotation safe, not an extra tidiness on top of it.

log_dir() is the parent of logging.file, so session_ID.log still sits beside
these and is unaffected by the stem.
"""
import logging
import logging.handlers
import sys

import paths

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# 10 MB live plus five rolled copies: a ~60 MB ceiling per writer, which is
# weeks of INFO at the rate above and still small enough to grep whole.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 5
# The smallest ceiling that is still a ceiling. Below this the number is not
# believed and the default is used instead; see _max_bytes().
MIN_MAX_BYTES = 1024 * 1024


def _int(value, default, key):
    """int(value), or `default`, complaining to stderr rather than to a logger.

    config.int_or is the same idea, but it reports through logging — which is
    the thing this module has not set up yet, so its warning would go to a root
    logger with no handlers and be lost. Here the only channel is stderr.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        print(f"⚠️  logging.{key}: expected a number, got {value!r} — "
              f"using {default}", file=sys.stderr)
        return default


def _max_bytes(value):
    """logging.max_bytes, clamped to something that really is a ceiling.

    RotatingFileHandler reads maxBytes=0 as "never rotate" — Python's
    documented behaviour, and the exact opposite of what a key called
    max_bytes promises. A zero (or a negative, or a few hundred bytes typed
    by mistake) would put back the unbounded file this module exists to stop,
    silently, because _int() is perfectly happy with 0 as a number.

    So anything under the floor is not believed: the default is used and the
    reason goes to stderr, the same way a non-number does. The alternative —
    honouring 0 as "no rotation" — is honest too, but it leaves the footgun
    loaded, and nobody who wants an unbounded log is reaching for a key named
    max_bytes to ask for it.
    """
    n = _int(value, DEFAULT_MAX_BYTES, "max_bytes")
    if n < MIN_MAX_BYTES:
        why = "would never rotate" if n <= 0 else "is not a useful ceiling"
        print(f"⚠️  logging.max_bytes: {n} {why} — below the "
              f"{MIN_MAX_BYTES} byte floor, using {DEFAULT_MAX_BYTES}",
              file=sys.stderr)
        return DEFAULT_MAX_BYTES
    return n


def configure(config, stem):
    """Install this process's root handlers. Returns the log file, or None.

    `stem` names the writer and is inserted before the suffix of the configured
    filename; see the module docstring for why that matters. Always logs to
    stderr; the file is added when logging.file is set and openable, and a
    directory that cannot be created costs the file handler, not the process.
    """
    cfg = config.get("logging", {}) or {}
    level = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stderr)]

    path, written = cfg.get("file"), None
    if path:
        try:
            p = paths.resolve(path)
            p = p.with_name(f"{p.stem}-{stem}{p.suffix}")
            p.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                p,
                maxBytes=_max_bytes(cfg.get("max_bytes", DEFAULT_MAX_BYTES)),
                backupCount=_int(cfg.get("backups", DEFAULT_BACKUPS),
                                 DEFAULT_BACKUPS, "backups"),
                # Logs carry the emoji the clients print and whatever the model
                # said; the platform default encoding is not a safe bet for it.
                encoding="utf-8"))
            written = p
        except OSError as e:
            print(f"⚠️  cannot log to {path}: {e}", file=sys.stderr)

    logging.basicConfig(level=level, handlers=handlers, format=FORMAT)
    return written
