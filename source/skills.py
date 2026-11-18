"""Skills: directories of written-down know-how, offered to the model as a catalog.

A skill is a directory holding a `SKILL.md` whose YAML frontmatter carries a
`description`. The model is shown a *catalog* — each skill's name, description
and absolute path — in the system message, and opens the one it wants with
`read_file`. **No skill body ever enters the prompt**: a catalog is a listing,
visible in /behavior and counted in history[0] like everything else there
(design principle 3). See docs/architecture.md, "Skills".

This module is synchronous and pure: no asyncio, no engine, no session. It
answers one question — given the config, what is the catalog text? — and the
core calls it through asyncio.to_thread, because it is a directory scan.

Staleness is per file. Each call lists every root and stats each SKILL.md; a
file whose (mtime, size) moved is re-read and re-parsed, an unchanged one is
served from the cache. A stat is cheap enough to do for every skill on every
rebuild, which is what lets an *edited* SKILL.md be noticed and not only a new
or deleted one.
"""

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

import paths
from token_counter import count_text

logger = logging.getLogger("letclaw.skills")

DEFAULT_DIRS = ("skills",)
DEFAULT_MAX_CATALOG_TOKENS = 1500
# The same figure as tools.max_output_chars' default, deliberately: a skill too
# big for read_file to return whole is a skill that should be split, and the
# loader names it rather than listing something the model cannot read in one go.
DEFAULT_MAX_SKILL_CHARS = 8000
# Rung 1 cuts each description to this many characters.
SHORT_DESCRIPTION_CHARS = 200

SKILL_FILE = "SKILL.md"
# The directory name is the skill's name. This rule is what stands between a
# directory listing and a string that reaches a system prompt.
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
FRONTMATTER_RX = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)

PREAMBLE = (
    "When a task clearly matches one of these, read its SKILL.md with "
    "`read_file` before answering and follow it. Resolve a relative path inside "
    "a skill against that skill's own directory. If none clearly matches, read "
    "none — most turns need no skill. Never read more than one before you have "
    "picked.")

RUNG_NOTES = {
    0: "",
    1: (f"Descriptions are cut to {SHORT_DESCRIPTION_CHARS} characters to fit "
        "the catalog budget; the SKILL.md has the rest."),
    2: ("Descriptions are left out to fit the catalog budget — read a "
        "SKILL.md to see what it covers."),
    3: ("Only names are listed, to fit the catalog budget. A skill's file is "
        "<root>/<name>/SKILL.md under the root it is listed with."),
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str          # absolute path of the SKILL.md, under its root
    root: str          # the configured root it was found in, absolute


@dataclass(frozen=True)
class Scan:
    skills: tuple      # Skill, sorted by name, clashes resolved
    roots: tuple       # absolute root paths, in config order, existing or not
    warnings: tuple    # one line per problem, each naming its file or root


def settings(config):
    """(enabled, dirs, max_catalog_tokens, max_skill_chars) from config.skills."""
    import config as configlib
    cfg = (config or {}).get("skills") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    dirs = cfg.get("dirs")
    if dirs is None:
        dirs = list(DEFAULT_DIRS)
    elif isinstance(dirs, str):
        dirs = [dirs]
    return (cfg.get("enabled") is True,
            [str(d) for d in dirs if d],
            configlib.int_or(cfg.get("max_catalog_tokens", DEFAULT_MAX_CATALOG_TOKENS),
                             DEFAULT_MAX_CATALOG_TOKENS),
            configlib.int_or(cfg.get("max_skill_chars", DEFAULT_MAX_SKILL_CHARS),
                             DEFAULT_MAX_SKILL_CHARS))


def parse(text, dir_name, path, max_chars, root=""):
    """(Skill, None) or (None, warning). Never raises."""
    if len(text) > max_chars:
        return None, (f"{path}: {len(text)} chars, over skills.max_skill_chars "
                      f"({max_chars}) — not listed; split it")
    m = FRONTMATTER_RX.match(text)
    if not m:
        return None, f"{path}: no YAML frontmatter (--- block at the top) — not listed"
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        return None, f"{path}: frontmatter is not valid YAML ({first}) — not listed"
    if not isinstance(meta, dict):
        return None, f"{path}: frontmatter is not a mapping — not listed"
    desc = meta.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return None, f"{path}: no description in the frontmatter — not listed"
    warning = None
    named = meta.get("name")
    if named is not None and str(named) != dir_name:
        warning = (f"{path}: frontmatter name {named!r} disagrees with the "
                   f"directory — listed as {dir_name!r}")
    return Skill(dir_name, " ".join(desc.split()), str(path), str(root)), warning


class SkillCatalog:
    """The parsed skill files, cached by (mtime, size), behind a lock.

    Its own cache rather than a corner of BehaviorStore: that class keys file
    *contents* by path and its stale() compares text against the file at the
    key. A catalog is not a file, so there is no path to key it by.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # str(path) -> ((mtime_ns, size), Skill | None, warning | None)
        self._files = {}
        # Warnings already logged, so a session creation does not repeat every
        # one of them. A warning that stops applying is forgotten, so it is
        # logged again if it comes back.
        self._logged = set()

    def scan(self, config):
        """Every skill the configured roots offer, and what went wrong."""
        _, dirs, _, max_chars = settings(config)
        with self._lock:
            roots, warnings, seen, by_name = [], [], set(), {}
            for d in dirs:
                root = paths.resolve(d)
                roots.append(str(root))
                for skill, warning in self._scan_root(root, max_chars, seen):
                    if warning:
                        warnings.append(warning)
                    if skill is None:
                        continue
                    if skill.name in by_name:
                        warnings.append(
                            f"{skill.path}: skill {skill.name!r} is also in an "
                            f"earlier root ({by_name[skill.name].path}) — this "
                            "one is not listed")
                        continue
                    by_name[skill.name] = skill
            # Files that vanished drop out of the cache.
            for gone in set(self._files) - seen:
                del self._files[gone]
            fresh = set(warnings)
            for w in warnings:
                if w not in self._logged:
                    logger.warning("skills: %s", w)
            self._logged = fresh
        return Scan(tuple(sorted(by_name.values(), key=lambda s: s.name)),
                    tuple(roots), tuple(warnings))

    def _scan_root(self, root, max_chars, seen):
        if not root.is_dir():
            yield None, f"{root}: skills root does not exist — nothing loaded from it"
            return
        real_root = root.resolve()
        try:
            entries = sorted(root.iterdir())
        except OSError as e:
            yield None, f"{root}: cannot list the skills root ({e})"
            return
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            skill_md = entry / SKILL_FILE
            if not skill_md.is_file():
                nested = sorted(entry.glob(f"*/{SKILL_FILE}"))
                if nested:
                    yield None, (f"{nested[0]}: a SKILL.md one level too deep — "
                                 "skills sit directly under a root; not listed")
                continue
            if not NAME_RX.match(entry.name):
                yield None, (f"{skill_md}: directory name {entry.name!r} is not "
                             "a valid skill name (a-z, 0-9, . _ -) — not listed")
                continue
            try:
                real = skill_md.resolve()
            except OSError as e:
                yield None, f"{skill_md}: cannot resolve ({e}) — not listed"
                continue
            if not real.is_relative_to(real_root):
                yield None, (f"{skill_md}: resolves outside its root ({real}) — "
                             "not listed")
                continue
            key = str(skill_md)
            seen.add(key)
            yield self._load(skill_md, key, entry.name, max_chars, root)

    def _load(self, skill_md, key, name, max_chars, root):
        try:
            st = skill_md.stat()
        except OSError as e:
            self._files.pop(key, None)
            return None, f"{skill_md}: cannot stat ({e}) — not listed"
        stamp = (st.st_mtime_ns, st.st_size, max_chars)
        hit = self._files.get(key)
        if hit and hit[0] == stamp:
            return hit[1], hit[2]
        try:
            text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            result = (None, f"{skill_md}: cannot read ({e}) — not listed")
        else:
            result = parse(text, name, skill_md, max_chars, root)
        self._files[key] = (stamp, *result)
        return result

    def catalog(self, config):
        """The catalog block for the system message, or "" when there is none."""
        enabled, _, budget, _ = settings(config)
        if not enabled:
            return ""
        scan = self.scan(config)
        if not scan.skills:
            return ""
        return render(scan, budget)


def _short(desc, limit=SHORT_DESCRIPTION_CHARS):
    """`desc` cut to `limit` characters at a word boundary, marked with an ellipsis."""
    if len(desc) <= limit:
        return desc
    cut = desc[:limit]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip(" ,;:.—-") + " …"


def _roots_lines(roots):
    head = ("Roots, earlier wins on a name clash:" if len(roots) > 1
            else "Root:")
    return [head] + [f"  {i}. {r}" for i, r in enumerate(roots, 1)]


def rung(scan, step):
    """The catalog block at one rung of the budget ladder (0..4)."""
    # Only the roots that contributed: an empty or missing root is a warning
    # in the log, not a line the model should go and list.
    roots = [r for r in scan.roots if any(s.root == r for s in scan.skills)]
    if step >= 4:
        where = " or ".join(roots)
        return (f"{len(scan.skills)} skills are present but the catalog is too "
                f"large for its budget. List {where} with list_dir, and read "
                "<root>/<name>/SKILL.md for the one that matches a task.")
    lines = [PREAMBLE]
    if RUNG_NOTES[step]:
        lines += ["", RUNG_NOTES[step]]
    lines += [""] + _roots_lines(roots) + [""]
    if step == 3:
        for i, r in enumerate(roots, 1):
            names = [s.name for s in scan.skills if s.root == r]
            if names:
                lines.append(f"Root {i}: " + ", ".join(names))
        return "\n".join(lines)
    for s in scan.skills:
        if step == 2:
            lines.append(f"- **{s.name}** — `{s.path}`")
            continue
        desc = s.description if step == 0 else _short(s.description)
        lines.append(f"- **{s.name}** — {desc}")
        lines.append(f"  `{s.path}`")
    return "\n".join(lines)


def render(scan, budget):
    """The first rung whose block fits `budget` tokens; rung 4 always fits."""
    for step in range(4):
        text = rung(scan, step)
        if count_text(text) <= budget:
            return text
    return rung(scan, 4)
