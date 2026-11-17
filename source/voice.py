"""voice.py — the voice: config section, read and spoken.

The WebUI speaks its answers through the speakers of the machine the browser is
on. The core's part is small: synthesize a WAV from text (POST /speak), and say
whether it can (the `voice` field of /info). Turning an answer into something
worth listening to is a model call and lives beside the other summarisers in
session.py (build_spoken_script); the cleaner and the chunker are JavaScript in
web/app.js. See docs/voice.md for the design and the measurements.

`piper` is imported lazily, the way token_counter.py imports tiktoken. The core
must boot and serve the WebUI with no piper installed — that is the difference
between an optional feature and a new hard dependency. Availability is asked
with `importlib.util.find_spec`, which finds the package without importing it:
importing piper pulls in onnxruntime, and a core with voice switched off must
not pay for that on every /info.
"""

import importlib.util
import io
import logging
import re
import threading
import wave
from pathlib import Path

import paths

logger = logging.getLogger("letclaw.voice")

DEFAULT_MODEL = "voices/en_US-lessac-medium.onnx"
SCRIPT_MODES = ("always", "auto", "never")

# Every key of the voice: section, with its default and the floor that keeps a
# typo from becoming a cap of zero. config.example.yaml documents each one.
_INTS = {
    "max_chars": (400, 20),
    "max_chunks": (8, 1),
    "script_min_chars": (280, 0),
    "script_max_tokens": (300, 16),
    "max_answer_chars": (4000, 100),
}
_FLOATS = {
    "speed": (1.0, 0.1),
    "gain": (1.0, 0.0),
    "script_timeout": (12.0, 1.0),
}


def settings(config):
    """The voice: section with every key present, coerced, and floored.

    Read per request rather than once: /reload rewrites the config dict in
    place, so reading here is what makes a retuned cap reach an open tab.
    A value that will not coerce falls back to the default with a warning —
    a voice section with one bad number should still speak.
    """
    raw = (config or {}).get("voice") or {}
    if not isinstance(raw, dict):
        raw = {}
    out = {"enabled": raw.get("enabled", True) is not False,
           "model": str(raw.get("model") or DEFAULT_MODEL),
           "script_model": str(raw.get("script_model") or "")}
    for key, (default, floor) in _INTS.items():
        out[key] = _number(raw, key, default, floor, int)
    for key, (default, floor) in _FLOATS.items():
        out[key] = _number(raw, key, default, floor, float)
    script = str(raw.get("script", "auto")).strip().lower()
    if script not in SCRIPT_MODES:
        logger.warning("voice.script=%r is not one of %s; using auto",
                       raw.get("script"), "/".join(SCRIPT_MODES))
        script = "auto"
    out["script"] = script
    return out


def _number(raw, key, default, floor, kind):
    value = raw.get(key, default)
    try:
        # bool is an int subclass; `max_chars: true` is a typo, not a 1.
        if isinstance(value, bool):
            raise TypeError
        value = kind(value)
    except (TypeError, ValueError):
        logger.warning("voice.%s=%r is not a number; using %s", key, value, default)
        return default
    return max(floor, value)


def model_path(s):
    """The voice file, repo-relative unless absolute."""
    p = Path(s["model"]).expanduser()
    return p if p.is_absolute() else paths.REPO_ROOT / p


def _piper_findable():
    """Whether `import piper` would find a package. Never imports it."""
    try:
        return importlib.util.find_spec("piper") is not None
    except (ImportError, ValueError):
        return False


def status(config):
    """(available, reason). `reason` is "" when available.

    Three conditions, in the order a person would fix them: the switch, the
    package, the voice file. `enabled` is included so a core with voice turned
    off paints the button unavailable rather than lit and silent. Each check is
    cheap (a dict read, a spec lookup, a stat), so this runs per request: the
    model path is reloadable, and a cached "missing" would outlive the download
    that fixed it.
    """
    s = settings(config)
    if not s["enabled"]:
        return False, "voice is switched off (voice.enabled: false in config.yaml)"
    if _import_failure:
        return False, _import_failure
    if not _piper_findable():
        return False, ("piper is not installed on the core — "
                       ".venv/bin/pip install piper-tts")
    path = model_path(s)
    if not path.is_file():
        return False, (f"no voice file at {path} — download "
                       f"{path.name} and {path.name}.json from "
                       "https://huggingface.co/rhasspy/piper-voices "
                       "into voices/")
    if not Path(f"{path}.json").is_file():
        return False, f"the voice file needs its config beside it: {path}.json"
    return True, ""


def info(config):
    """The `voice` field of /info: whether the button may be offered, and
    every number the page must obey. The core's config owns them; a page that
    hardcoded 400 would disagree with a core retuned to 200, and every one of
    its requests would be refused."""
    s = settings(config)
    ok, reason = status(config)
    return {"available": ok, "reason": reason,
            "max_chars": s["max_chars"], "max_chunks": s["max_chunks"],
            "script": s["script"], "script_min_chars": s["script_min_chars"],
            "max_answer_chars": s["max_answer_chars"]}


class Unavailable(RuntimeError):
    """Synthesis cannot run here; the message says what to install or fix."""


# One voice, loaded on first use and kept for the process's life (0.7 s,
# ~136 MB). Keyed by path so a /reload that names another voice loads that one.
# The lock serialises loading and synthesis: one CPU-bound worker is the shape
# this has, and two threads in one ONNX session buy nothing but contention.
_lock = threading.Lock()
_voice = None
_voice_path = None
# Set when `import piper` was found but failed to import — a broken install.
# Recorded once so every later request answers with the same reason instead of
# re-raising the import on each one.
_import_failure = ""


def _load(path):
    global _voice, _voice_path, _import_failure
    if _voice is not None and _voice_path == path:
        return _voice
    try:
        from piper import PiperVoice
    except ImportError as e:
        _import_failure = f"piper failed to import on the core — {e}"
        logger.warning("%s", _import_failure)
        raise Unavailable(_import_failure) from e
    try:
        voice = PiperVoice.load(str(path))
    except Exception as e:
        raise Unavailable(f"could not load the voice {path.name}: {e}") from e
    logger.info("voice loaded: %s", path)
    _voice, _voice_path = voice, path
    return voice


def synthesize(s, text):
    """One WAV, as bytes. Blocking — call through asyncio.to_thread.

    `s` is a settings() dict, taken by the caller once per request so the cap
    it checked and the voice it synthesizes with come from the same reading.
    """
    with _lock:
        voice = _load(model_path(s))
        from piper import SynthesisConfig
        # `speed` is a rate, which is what a person means by the word; piper's
        # length_scale is its reciprocal (2.0 is half speed).
        cfg = SynthesisConfig(length_scale=1.0 / s["speed"], volume=s["gain"])
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            voice.synthesize_wav(text, wav, syn_config=cfg)
        return buf.getvalue()


_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*\s")


def cap_script(text, limit):
    """`text` cut to at most `limit` characters, at a sentence end if one fits.

    The page sends at most max_chunks × max_chars characters to /speak; a
    script longer than that would have its tail dropped there instead, with no
    sentence boundary to end on. Cut here, where the cut can be clean: the last
    sentence end that fits, else the last space, else hard.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit + 1]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends:
        return head[:ends[-1]].strip()
    space = head.rfind(" ")
    return (head[:space] if space > 0 else text[:limit]).strip()
