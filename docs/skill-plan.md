# Skills — plan

Status: **planned** (2026-11-11). Nothing here exists yet; every path and number
below was checked on this box the same day.

Goal: letsClaw agents can be handed written-down know-how — a directory with a
`SKILL.md` in it — and pick it up when a task matches, the way opencode and
openclaw do. Same idea, smallest possible implementation, and one deliberate
departure from both: **the body never enters the prompt.**

## What a skill is, in the systems named

Both were inspected on this machine, not from memory.

| | openclaw | opencode |
|---|---|---|
| where | `~/openclaw/skills/`, 55 skills, **tracked in git** | `.claude/skills/*` auto-loaded as plugins (strings in `~/.opencode/bin/opencode`) |
| unit | one directory, one `SKILL.md`, YAML frontmatter | same |
| how the model learns of it | a `<available_skills>` block of name + description + `<location>` | a `<available_skills>` block; `prompt.skills`, and a refresh message "The available skills have changed. This list supersedes the previous available skills list." |
| how the body is loaded | the model `read`s the file | same |
| budget control | `maxSkillsInPrompt` / `maxSkillsPromptChars`, with a **compact** fallback (name + location, description dropped) before skills are dropped at all | `prompt_skills` |

The shared mechanism is the important part, and it is one round trip: **the
catalog is always in the prompt; the body is a file the model opens when it
wants it.** A skill costs its description every turn and its full text only on
the turns that use it.

openclaw's own frontmatter, counted across its 55 skills:

| key | uses | taken here? |
|---|---|---|
| `name` | 54 | yes |
| `description` | 54 | yes |
| `metadata.openclaw.emoji` | 47 | no |
| `metadata.openclaw.requires` (`bins`, `env`) | 47 | no |
| `metadata.openclaw.install` (brew/npm/uv specs) | 42 | no |
| `homepage` | 34 | ignored, parsed |
| `metadata.openclaw.os` | 9 | no |
| `metadata.openclaw.primaryEnv` | 5 | no |
| `allowed-tools`, `user-invocable`, `argument-hint` | 1 each | no |

Only `name` and `description` are load-bearing in practice. Everything else is
openclaw's installer and UI, and none of it is a fact the model needs.

## The decision that shapes the rest: read on demand, never inject

**No skill body goes into the system prompt, ever.** Not the first one, not the
"always" ones, not the matching one.

This is [principle 3](design-principles.md#3-no-hidden-token-injection) — the
context is the behavior files plus the conversation, nothing else, and it is
what makes the `⚡` line trustworthy. A catalog is a *listing*, visible in
`/behavior` and counted like anything else in `history[0]`; silently injecting
one skill's 4 KB because a keyword matched is the quiet per-prompt addition the
principle exists to forbid, and it is also wrong on the merits: the match is
made by string comparison, and a wrong skill in the prompt is a wrong
instruction, not a spare fact.

What that buys, measured against openclaw's real 55-skill set:

| what is in the prompt | tokens |
|---|---|
| `models/base.md`, today | 1 965 |
| a catalog of all 55, name + full description + path | **3 433** |
| the same at 8 skills (a normal install) | 462 |
| one skill's body, injected (median 1 929 chars) | ~490 |

A catalog of a *large borrowed* set costs more than the base behavior. A catalog
of a normal hand-written set costs a quarter of it. Both numbers are the reason
the ladder below exists and the reason nothing bigger is injected.

## Where skills live

    skills/                     repo root, tracked in git — the shipped set
      <name>/SKILL.md
      <name>/scripts/…          whatever the skill wants; the core never reads it

`skills/` at the root rather than under `models/`: `models/` holds the behavior
layers, which are *this install's* prompt, and a skill is a library that happens
to be Markdown. The root name also matches both systems being modelled, which
matters because the second root is likely to be somebody else's tree.

Tracked, unlike `models/sessions/`. A skill is content worth sharing — openclaw
tracks all 55 of its skill files — and it is not machine-specific the way a
session's behavior file is. `skills/README.md` is the only required file; a
directory with nothing in it is the feature switched on and nothing to load.

Extra roots are configuration:

    skills:
      enabled: true
      dirs:
        - skills
        - /home/ryan/openclaw/skills     # read-only borrowed set, name clashes lose

Earlier root wins on a name clash, and the loser is reported at load. Borrowing
openclaw's set rather than copying it is the point: 55 skills, zero duplicated
files, and `git pull` in that tree is the update path.

## The loader

One new module, `source/skills.py`, ~120 lines, and it is **synchronous and
pure** — no asyncio, no engine, no session. It answers one question: *given the
config, what is the catalog text?*

```python
def catalog(config) -> str        # "" when there is nothing to load
```

Behind it:

- `Skill` = `name`, `description`, `path`. Three fields, no more.
- Scan each root's immediate subdirectories for `SKILL.md`. No recursion — a
  skill that nests is a mistake and gets reported, not silently half-loaded.
  (openclaw has to *detect* a nested root at runtime, `workspace.ts:258`; here
  the rule is one level and that is that.)
- Parse frontmatter with the same regex openclaw's loader effectively uses plus
  `yaml.safe_load` on the block. **Measured: all 55 real skills parse in 38 ms
  with PyYAML, and exactly one fails** (`canvas`, no frontmatter). PyYAML is
  already a required dependency; no new one.
- A file that fails to parse, has no `description`, or is bigger than
  `max_skill_chars` is **skipped with a warning naming the file** — never
  silently dropped and never fatal. Principle 9: the failure mode is "this
  skill is not offered", which is the state a fresh install is already in.
- The name comes from the **directory**, always. Frontmatter `name` is checked
  against it and a mismatch warns. Measured across openclaw's set: 0 mismatches
  out of 54 — so the field is redundant in practice, and trusting the directory
  removes a class of "the file says one thing, the path another".
- `name` must match `^[a-z0-9][a-z0-9._-]*$` and the resolved path must stay
  inside its root. Both are cheap and both are the difference between a
  directory listing and a path that reaches a system prompt.

### Why a cache, and which one

The catalog is read at the same three moments the behavior layers are: session
creation, a prompt rebuild, and `/reload`. It is a directory scan and a few
dozen file reads, so it cannot run on the event loop — principle 14. It goes
through `asyncio.to_thread` exactly as `BehaviorStore` does.

Do **not** fold it into `BehaviorStore`. That class caches *file contents* keyed
by path, and `stale()`/`refresh_sync()` compare cached text against the file at
that key. A catalog is not a file: there is no path whose content is "the
catalog", so it cannot be a key, and a key that is not a real path makes
`stale()` warn about a file that does not exist. Give `skills.py` its own
`{skill_dir: (mtime, text)}` map — the same lock discipline, and the shape fits.

Its staleness rule is then the honest one: a root whose **mtime** moved gets
re-scanned. Behavior files are compared by content because they are few and
small; a skills root may hold 55 files and 4 MB, and re-reading all of them on
every `/reload` to notice that one changed is a worse trade. A new directory
under the root moves the root's mtime, so a *new skill* is caught; an edit to
an existing `SKILL.md` is not, and needs `/reload` twice or a restart. That gap
is stated in the module docstring rather than papered over.

## The catalog block

Appended to the system message, after the session layer, **before** the
carryover marker:

    # Skills

    When a task clearly matches one of these, read its SKILL.md with `read_file`
    before answering and follow it. Resolve a relative path inside a skill
    against that skill's own directory. If none clearly matches, read none —
    most turns need no skill. Never read more than one before you have picked.

    Roots, earlier wins on a name clash:
      1. /home/ryan/letsClaw/skills
      2. /home/ryan/openclaw/skills

    - **helios-grafana-tunneling** — Expose the Grafana instance on mac2017 …
      `/home/ryan/openclaw/skills/helios-grafana-tunneling/SKILL.md`

Three choices worth defending:

**Markdown, not `<available_skills>` XML.** The prompt is already Markdown and
`read_file` returns Markdown; XML tags buy nothing here and cost ~900 chars of
tag noise across 55 entries (measured: 12 937 chars as XML vs 12 723 as
Markdown at the same content). The tag form is what openclaw emits because
openclaw parses it back out in places; there is nothing to parse here.

**Absolute paths in the entries.** The alternative is one root line plus relative
names, which measures 2 466 vs 3 433 tokens for the 55-skill set — a real 1 000
token saving. It is not taken, because the model then has to *join* two strings
to find the file, and a wrong join is a `read_file` error on a path that looks
right. `paths.REPO_ROOT` is already the answer to "where is anything" and
`read_file` takes any absolute path. Correct on the edge case beats 1 000 tokens
in a catalog nobody is near the limit for; the ladder below recovers the space
anyway when a machine genuinely is.

**The instruction line is part of the block, not `base.md`.** A session with
`skills.enabled: false` must not be told to go looking for skills. The preamble
is 612 chars / 151 tokens, and it is paid once per prompt, not once per entry —
which is why the *rules* live there and not in each skill's description.

### The budget ladder

`max_catalog_tokens` (default 1 500) is a ceiling on the whole block. Over it,
entries degrade in fixed steps and the block **says which step it is on** —
a model that cannot see a catalog was cut will not go looking for what is
missing:

| rung | entry shape | measured, 55 skills |
|---|---|---|
| 0 | name + full description + path | 3 433 tok |
| 1 | description truncated to 200 chars | 2 920 tok |
| 2 | name + path, no description | 1 352 tok |
| 3 | name only, plus the root line | 319 tok |
| 4 | block omitted, one line: `skills: 55 present, catalog too large — list <root> with list_dir` | ~40 tok |

openclaw has rungs 0/2/4 in spirit (full → compact → drop) and hides the
downgrade behind a warning line prepended by the caller. The rungs are ordered
by what a model loses: dropping *description text* costs it the judgement of
which skill applies; dropping *paths* costs it the ability to open one. So
descriptions go first and paths go last, which is the opposite of openclaw's
compact form and the one place this plan is knowingly better.

At 8 skills rung 0 is 462 tokens and never trips. The ladder is for the borrowed
55-skill root, and it is why borrowing is safe to enable.

## Plumbing: a fourth layer, and the two traps in it

`fresh_history()` (`source/core.py:1052`) appends the layers in order. The new
line goes between the session layer and the carryover:

```python
if self.skills_block:
    content += SKILLS_MARKER + self.skills_block
if carryover:
    content += CARRYOVER_MARKER + carryover
```

**Trap 1 — the order is load-bearing.** `carryover()` (`core.py:1142`) recovers
the handoff by partitioning `history[0]` on `CARRYOVER_MARKER` and taking
*everything after it*. Anything appended below the carryover is read back as
part of the handoff on the next rebuild and re-appended, growing the prompt one
catalog per turn, forever. The skills block must sit above that marker. This is
the same reason `fresh_history`'s docstring already says the carryover "stays
last, and that is load-bearing".

**Trap 2 — `behavior_for()` returns a 3-tuple and four call sites unpack it by
position.** `core.py:1096`, `:1118`, `:3530` and `behavior.py:264`. Adding a
fourth element to that tuple is a silent bug at three of the four (a
`ValueError` at best, a wrong layer at worst). The skills block does **not**
travel in that tuple — it is not a behavior layer, it is a listing, and it comes
from `SessionManager` rather than from `BehaviorStore`. It is set on the session
where `tools`/`schemas`/`tools_tok` are already set (`core.py:3778`), from the
manager's cached catalog, and refreshed on the same paths.

Consequences of it being *on the session* rather than in the store:

- `/behavior` gains a fourth field, `skills`, so a person can read exactly what
  the model was told (`core.py:3307` returns the other three today).
- `refresh_behavior()` reports a catalog change the same way it reports "behavior
  re-read from disk: base.md" — one notice, named file, no surprise.
- A rebuild that does not re-read it (none today) would keep a stale block; the
  three call sites are listed above precisely so the change reaches all of them.

**`/reload` semantics, settled explicitly.** `/reload` already says of behavior
files that they "apply at the next rollover, /clear or /new" — a live window is
not rebuilt, because rewriting `history[0]` mid-session throws away the prefix
the model has been running on. Skills follow that exactly, and the notice says
the same words. A skill added to a live session's roots is therefore not visible
until the next window. That is the same delay `models/sessions/*.md` has, it is
documented there, and matching it is worth more than inventing a second rule.

## Command: `/skills`, and where it lands

`/skills` — bare form lists what is loaded and from where; `/skills <name>`
prints that skill's `SKILL.md`. It reads and changes nothing, so it belongs
**above the busy guard** with `/info` and `/behavior` (`core.py:3237`), which
means it works mid-turn — the moment you most want to know whether the thing you
are waiting on had a skill to read.

The eight-listing rule (`architecture.md`'s table) applies, and the
command-surface check in the local suite will enforce it by name — it parses the
listings against `Session.command` rather than against a hardcoded list, so a
word missing from any one of them fails there. The rows:

| place | what changes |
|---|---|
| `source/core.py` | the branch in `Session.command` |
| `source/chat.py` | `COMMANDS` (help line + tab completion) |
| `web/app.js` | `COMMANDS` — a **hard gate**, a word missing here is unreachable in the browser |
| `web/index.html` | a `#btn-skills` header button — `/behavior` has one (`#btn-behavior`, `index.html:70`) and `/skills` is the same kind of read-only dump, so not having one would be the odd choice. It is the only row here that is optional; the check cannot see it and says so in its docstring |
| `source/discord_client.py` | `COMMAND_WORDS` (miss it and `!skills` is sent to the model as chat), `HELP`, `_register_commands` |
| `README.md` | the terminal command table |
| `docs/architecture.md` | the Mermaid `Cmds` node and the prose beside it |

`/skills <name>` returns file *contents*, which is a new shape for a command
reply — `/behavior` already does exactly this with three files, so the pattern
exists in all three clients (`chat.py:557`, `app.js:1837`,
`discord_client.py:1425`).

## Why no new tool

The obvious design is a `skill` tool with an `open` action. It is not taken, for
three reasons:

1. **`read_file` already does it.** The tool set is unconfined by design
   (`design-principles.md`, "Where the principles are known to be strained") —
   `read_file` reads any path the process can. A skill tool that reads a file is
   `read_file` with a path join in front of it.
2. **Every tool schema is in every prompt of every session.** `tools.py` says so
   about the `reason` field: "a description that reads nicely here costs more in
   a week than the whole feature is worth." A twelfth tool's schema is a fixed
   per-session cost paid whether or not skills are used; the catalog is a cost
   only an install with skills pays, and it degrades.
3. **A tool is an action; this is a document.** Making it a tool invites the
   model to treat `skill(name)` as the whole of consulting it, and then the body
   has to come back through a tool result, where it is clipped by
   `max_output_chars` like anything else — which is fine, and is exactly what
   `read_file` does today.

**The clipping is real and accepted:** 5 of openclaw's 55 bodies exceed the
8 000-char default (`gh-issues` at 34 064, `skill-creator` 18 154, `xurl`
13 836, `coding-agent` 10 458, `healthcheck` 10 145). A clipped `read_file` says
so and the model can come back with `inspect_file` or a line range — the
behaviour is documented in `tokens.md` and applies to every large file, not just
skills. The median body is 1 929 chars, well inside. `max_skill_chars: 8000` is
the *loader's* cap on what it will list, deliberately the same figure as the
tool clip: a skill too big to read in one go is a skill that should be split, and
the loader says which one it skipped.

## Config

    skills:
      enabled: true
      dirs: [skills, /home/ryan/openclaw/skills]
      max_catalog_tokens: 1500
      max_skill_chars: 8000

`skills` joins `SECTION_KEYS` (`config.py:42`). That list is pinned against
`config.example.yaml` by a check that fails by name if the two disagree — nine
sections become ten, and `config.example.yaml` must show the block or the pin
breaks. `test_config_load.py`'s "sections are the documented ones" is the
existing gate; it needs no change beyond the two lists matching.

`enabled` defaults **false**, and the default `dirs` is `[skills]`. A fresh
clone has an empty `skills/` with a README in it, so the default is also the
no-op — the same shape as `discord:` and `webui.files_dir`, where the section
existing does not mean the feature is on.

## A first skill for this repo

`skills/letsclaw-dev/SKILL.md` — how to work on letsClaw itself: the doc
principles, where the gates are, the eight-listing rule, the version bump. It is
the highest-value first entry for three reasons: it is the knowledge a session
editing this tree needs and does not have; it can be *tested by doing*
(principle 11 — the skill tells the agent to run the gates, and the gates are
the check); and it is written by someone with a reason to read it, which is the
only way a skill directory stays honest.

`models/todo.md`'s `helios-grafana-tunneling` request (2026-11-06, done in the
openclaw tree) is the second one, and it is a port, not an authorship — the file
exists, is 2 793 bytes, and already follows the format.

## Tests

**Core, fast tier, hermetic** — no engine, no model file, no network. Every
fixture is a `tmp_path` tree the test writes:

- a root with one valid skill → the block names it, carries its description and
  its absolute path
- `enabled: false` → `catalog()` returns `""`, and the system message is
  byte-identical to what it was before the feature existed. This is the
  no-op-by-default guarantee, asserted on the actual prompt string rather than
  on a flag
- two roots with the same skill name → the earlier root's path appears, and one
  warning names the loser
- frontmatter that is not YAML → that skill skipped, its sibling still listed,
  the warning names the file. Never an exception out of `catalog()`
- missing `description` → skipped, same handling
- body over `max_skill_chars` → skipped and named (guards the promise that
  nothing listed is unreadable)
- a `SKILL.md` one level deeper (nested root) → not listed; a name that fails
  the character rule → not listed; a symlink out of the root → not listed
- frontmatter `name:` disagreeing with the directory → listed under the
  directory name, warning raised
- a root that does not exist → no crash, one warning, the other root still
  loads
- **the ladder**: `max_catalog_tokens` lowered until each rung trips → each
  rung's shape appears in order, the block states which rung it is on, and the
  last rung still names the root so the skills are findable by `list_dir`
- description truncation cuts at a character boundary and never mid-word
- ordering is deterministic across runs (sorted by name) — otherwise every
  prompt differs between two identical installs and the token floor moves
  without anything changing

**Prompt integration**, same tier, on a real `Session` with a stub engine:

- the block sits **above** `CARRYOVER_MARKER`, asserted by position in the
  string, and after a rollover the block is still above it and the carryover
  text is unchanged — the trap in "Plumbing", tested rather than commented
- a rebuild (`/clear`, rollover, `/new`) keeps exactly one copy of the block:
  run two rebuilds and count occurrences. This is the regression test for the
  re-append bug the ordering rule prevents
- `/behavior` returns the `skills` field; `/skills` lists; `/skills <name>`
  returns the file; `/skills nosuch` says so without raising
- `/skills` works while a turn is busy (above the guard, like `/info`)
- the catalog's tokens are inside the `history[0]` count that `/info` reports —
  principle 3's promise is that nothing sits outside the arithmetic

**Command surface** — the existing check parses the eight listings against
`Session.command`, so `/skills` needs no new test, only the seven edits. It will
fail by name until they are all in, which is the point.

**Config** — the existing pin compares `SECTION_KEYS` to the sections
`config.example.yaml` documents; adding `skills:` to one and not the other fails
there.

## Build order

1. **`source/skills.py` + its tests.** Pure, synchronous, no integration. Ships
   nothing to the user but it is the whole risky part, and it is testable
   against a fixture tree with no core running.
2. **The core half.** `skills` joins `SECTION_KEYS`, the block reaches
   `fresh_history()`, `SessionManager` caches the catalog and sets it on the
   session, `/reload` refreshes it, `/behavior` and `/skills` render it, the
   seven listings gain the word. Fast tier green, then full.
3. **`skills/README.md` + `letsclaw-dev`**, and the docs pass: a section in
   `architecture.md` beside the behavior layers (principle 2's stack diagram
   gains a fourth line), `config.example.yaml`'s block, README's feature list and
   command table, `docs/clients.md` only if a client does anything new (it does
   not — `/skills` is rendered like `/behavior`), `version.py` bump, and
   `models/todo.md`'s grafana item moved to the port note.

Step 2 is the only one that can regress a live session; steps 1 and 3 cannot.

## Risks / open edges

- **A skill is instructions from disk, and the model obeys them.** That is the
  feature. It is the same trust level as `exec` and `models/sessions/*.md`, and
  the same mitigation: the core is loopback-bound and the roots are chosen by
  the operator. But a borrowed root is *someone else's prose inside your agent's
  prompt* — which is why `dirs` is explicit and never a wildcard, and why the
  warning at load names every root that contributed.
- **`read_file` has no offset**, so a 34 KB skill comes back as its head and
  tail with the middle replaced by a truncation marker. Acceptable (the model is
  told, and `inspect_file` reaches the middle) but worth knowing: the five skills
  named above are the ones where the first read is not the whole document.
- **Catalog cost is paid by every session, including ones that never use a
  skill.** 462 tokens at 8 skills, 3 433 at 55. The ladder bounds it and
  `enabled: false` zeroes it; nothing makes it free.
- **Name collisions across roots are resolved by order, not by content.** Two
  unrelated skills called `github` in two roots means one is invisible. The
  warning names it; there is no merge, because a merged description would be a
  sentence about two different things.
- **The mtime staleness rule misses edits** to an existing `SKILL.md` until the
  root's own mtime moves. Stated in the module docstring. The alternative —
  hashing every file on every rebuild — is the trade `BehaviorStore` makes for
  three small files, and a skills root is not three small files.
- **`/skills <name>` returns the file body into a command reply**, which goes to
  one client and not into history. Consistent with `/behavior`; worth noting
  because it is the largest payload any command returns, and Discord chunks it
  through `chunker.py` like any other long answer.
- **Not built here, deliberately:** skill *installation* (openclaw's
  `metadata.install` — 42 of its skills declare one), per-skill enable/disable,
  invoking a skill as a slash command, `allowed-tools` scoping, and any UI in
  the WebUI beyond the `/skills` reply. Each is a feature that presumes skills
  are actually being written, which is the thing this plan cannot presume.
