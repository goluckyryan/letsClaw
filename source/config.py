"""Reading config.yaml and turning it into engines.

Everything between the file on disk and a usable LLMEngine: load it, read the
webui presentation keys out of it, coerce the numbers people actually write,
build one engine per model, and say what changed across a reload.

None of it touches a Session, which is why it is here rather than in core —
server.py was already reaching past core's API for core.known_models and
core.PIN_KEYS, and that is the tell that this was a module all along.

Synchronous and network-free throughout. build_all_engines doubles as the
config validator: building the engines is a far better check than inspecting
keys, because it is the same code path a real turn will run.
"""
import logging
import re
from pathlib import Path

import yaml

import paths
from llm_engine import LLMEngine

logger = logging.getLogger("letclaw.config")

CONFIG_PATH = paths.REPO_ROOT / "config.yaml"

DEFAULT_CONTEXT_LENGTH = 12000
# Seconds held back from turn_timeout so the answer still fits inside the wall
# after a long think.
DEFAULT_ANSWER_TIME_RESERVE = 120


def load_config(path=None):
    """Read config.yaml. Raises rather than exiting — a service must not sys.exit."""
    path = Path(path or CONFIG_PATH)
    if not path.exists():
        raise FileNotFoundError(f"No config.yaml at {path} "
                                "(copy config.example.yaml and customise)")
    with open(path) as f:
        return yaml.safe_load(f) or {}


def int_or(value, default):
    """int(value), or `default` when the config says something that is not one.

    YAML hands back a string for `max_history_messages: "50"` and None for a
    key written with no value at all, and both of those used to flow straight
    into a comparison against len(history), where they raise rather than fall
    back. A bad number in a config file should cost a log line, not the turn.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("expected a number, got %r — using %s", value, default)
        return default


# --------------------------------------------------------------- presentation

def config_theme(config):
    """webui.theme: auto (follow the OS), dark, or light.

    Anything else — a missing section, a typo — degrades to auto rather than
    forcing a palette the user did not ask for.
    """
    t = str((config.get("webui", {}) or {}).get("theme", "auto")).lower()
    return t if t in ("auto", "dark", "light") else "auto"


PIN_KEYS = ("background", "transparency", "font_size", "font_color")

# A pin value is interpolated straight into the stylesheet /theme.css generates,
# so it is CSS source, not data — and that route is unauthenticated, served to
# every tab. Allowed is what colours, lengths and numbers are actually made of:
# letters, digits, spaces, and  # % . , ( ) + - _  for rgba() and signed values.
# Everything that could end the declaration or open another — ; } { : quotes
# backslash @ — is absent by construction, which is the whole of the defence.
_CSS_VALUE_RE = re.compile(r"^[A-Za-z0-9 #%.,()+_-]{0,200}$")


def valid_pin_value(v):
    """True if `v` can be interpolated into /theme.css as a CSS value.

    Checked at save time so a bad value is refused with a message, and again at
    render time so one hand-edited into config.yaml — or saved before this
    existed — still cannot reach a browser.
    """
    s = str(v)
    # url( survives the character class (rgba() needs parens) and is the one
    # construct that turns a style into an outbound request, so it goes by name.
    return bool(_CSS_VALUE_RE.match(s)) and "url(" not in s.lower().replace(" ", "")


def config_pin(config):
    """webui.pin: the style of the pinned prompt, as {key: value}.

    Only the known keys survive, so a typo in config.yaml cannot reach the
    page as a broken custom property. It rides the hello and reloaded events
    in addition to the /theme.css stylesheet: a browser will not re-fetch a
    stylesheet that is already loaded, so the event is the only way a change
    reaches a tab that is already open — the one you are watching while you
    test the transparency.
    """
    pin = (config.get("webui", {}) or {}).get("pin", {}) or {}
    return {k: pin[k] for k in PIN_KEYS if k in pin}


# -------------------------------------------------------------------- engines

def known_models(config):
    return [k for k in config.get("models", {}) if k != "default_model"]


def build_engine(config, model_name=None, forward=None):
    """Construct an engine for one configured model entry.

    forward: an ssh_forward.SSHForward for this model, when the entry names
    one — the engine is pointed at the forward's local port. None otherwise.

    Synchronous: nothing here does real I/O. The behavior file is read separately
    so the engine can be cached per model while behavior stays per session.
    """
    models_cfg = config.get("models", {})
    name = model_name or models_cfg.get("default_model")
    entry = models_cfg.get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"Unknown model '{name}'. Configured: "
                         f"{', '.join(known_models(config)) or '(none)'}")
    base_url = entry.get("base_url")
    if not base_url:
        raise ValueError(f"Model '{name}' has no base_url — every entry must name "
                         "the server that serves it.")
    # An ssh_forward model is reached through a forward the core owns: the
    # config's port is a placeholder and the real local port is the forward's.
    # forward is None here (sync builder); the async call sites pass it in.
    if forward is not None:
        base_url = forward.local_url(base_url)
    engine = LLMEngine(
        base_url=base_url, api_key=entry.get("api_key", ""),
        default_model=entry.get("provider_name", name),
        timeout=(int(entry.get("turn_timeout", 36000) or 0) or None),
        temperature=entry.get("temperature"),
        max_tokens=entry.get("max_tokens"),
        top_p=entry.get("top_p"),
    )
    engine.context_length = entry.get("context_length", DEFAULT_CONTEXT_LENGTH)
    # The answer sheet: the cap the closing round runs under, once thinking has
    # been shut with </think>. Every token of it buys answer, not reasoning.
    # 0 = the closing round keeps the per-round max_tokens.
    engine.max_output = int(entry.get("max_output_tokens", 32768) or 0)
    # Hard wall for one turn, in seconds — a limit on a round, thinking model or
    # not. 0 = no wall. It must reach the HTTP client too, whose 600 s read
    # timeout would otherwise kill a long round.
    engine.turn_timeout = int(entry.get("turn_timeout", 36000) or 0)
    # Whether a cut-off round is resumed at all. There is deliberately no round
    # budget: the pad grows until time or context stops it, both of which are
    # real limits. A round that reasons nothing ends it (see _think_pad).
    engine.reasoning = bool(entry.get("reasoning", True))
    # Seconds held back from turn_timeout so the answer still fits inside the
    # wall after a long think. Thinking stops at turn_timeout - this.
    engine.answer_time_reserve = int(entry.get("answer_time_reserve",
                                               DEFAULT_ANSWER_TIME_RESERVE) or 0)
    # How many times a pad that has filled the context may be distilled to a
    # checkpoint and carry on. Off by default: compaction is lossy, and for a
    # derivation that carries exact values through many steps, concluding from a
    # complete pad usually beats continuing from a summarised one.
    engine.max_pad_compactions = int(entry.get("max_pad_compactions", 0) or 0)
    mode = str(entry.get("resume_mode", "auto")).lower()
    if mode not in ("auto", "chat", "raw", "off"):
        logger.warning("model %s: bad resume_mode %r, using auto", name, mode)
        mode = "auto"
    engine.resume_mode = mode
    # Qwen3 templates turn this into a system directive; llama.cpp defaults to
    # xhigh, which is itself a large part of why rounds run out mid-reasoning.
    engine.reasoning_effort = entry.get("reasoning_effort") or None
    return engine, name, entry


def build_all_engines(config, forwards=None):
    """One engine per configured model, or raise.

    This is the config validator. Building them is a far better check than
    inspecting keys, because it is the same code path that will run for real.
    Synchronous and network-free: a model with an ssh_forward is pointed at the
    forward's local port (a string swap — the port is assigned when the forward
    object is built), but the forward is NOT brought up here. Spawning the ssh
    is lazy: engine_for() ensures it on first real use, and the keepalive
    revives it if the path drops. That keeps a reload cheap and hermetic — a
    reload that only touches another model never reaches for the ssh path.
    Raises ValueError naming the offending model.
    """
    models_cfg = config.get("models") or {}
    if not isinstance(models_cfg, dict):
        raise ValueError("models: must be a mapping of name -> settings")
    names = known_models(config)
    if not names:
        raise ValueError("no models configured")
    default = models_cfg.get("default_model")
    if default and default not in names:
        raise ValueError(f"default_model '{default}' is not one of: {', '.join(names)}")
    out = {}
    for name in names:
        fwd = forwards.forward(name) if forwards is not None else None
        out[name] = build_engine(config, name, forward=fwd)
    return out


# ----------------------------------------------------------------------- diff

class _Missing:
    def __repr__(self):
        return "(unset)"


_MISSING = _Missing()


def _short(v, n=40):
    text = repr(v) if not isinstance(v, str) else v
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def diff_config(old, new, prefix="", out=None, limit=40):
    """Dotted 'a.b.c: old -> new' lines describing what changed.

    For the reply and one log line, so a reload says what it did rather than
    just that it happened. Dicts recurse; anything else is compared whole — a
    changed list is one line, not a diff of its elements.
    """
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    for key in sorted(set(old) | set(new)):
        if len(out) >= limit:
            out.append("…")
            break
        path = f"{prefix}{key}"
        a, b = old.get(key, _MISSING), new.get(key, _MISSING)
        if isinstance(a, dict) and isinstance(b, dict):
            diff_config(a, b, f"{path}.", out, limit)
        elif a != b:
            out.append(f"{path}: {_short(a)} -> {_short(b)}")
    return out
