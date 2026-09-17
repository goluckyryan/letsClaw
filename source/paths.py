"""Where things live.

The modules sit in source/, but almost everything they need — config.yaml, the
behavior Markdown under models/, the web assets, state/ and logs/ — lives one
level up at the repo root. Anchoring to the repo rather than to the working
directory keeps the app behaving the same however it was launched, and keeping
that answer in one place means there is exactly one line to get wrong.
"""

from pathlib import Path

# source/paths.py -> source/ -> the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve(path, default=None):
    """A configured path: used as-is if absolute, otherwise repo-relative."""
    return _abs(Path(path or default).expanduser())


def _abs(p: Path) -> Path:
    return p if p.is_absolute() else REPO_ROOT / p
