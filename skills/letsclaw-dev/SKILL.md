---
name: letsclaw-dev
description: How to change letsClaw's own code (source/, web/, models/, docs/) — the gates to run, the doc rules, the places a new command must be listed, and the version bump. Read it before editing this repository.
---

# Working on letsClaw

The repo root is where `serve.sh` lives. Resolve every path below against it.

## Before you edit

1. Read `docs/README.md`, then the part of `docs/architecture.md` that covers
   what you are changing. Its **Traps** list at the end is short; read it.
2. Read `docs/design-principles.md` if the change adds tokens to a prompt,
   blocks the event loop, or changes what a client renders. Those are the
   three ways a reasonable-looking change breaks a principle.

## Rules that are easy to break

- `Session.emit()` is synchronous. Never add an `await` to it, to
  `Subscriber.push`, or to any streaming callback.
- Nothing goes into `history[0]` that is not a behavior file, the skills
  catalog, or the carryover. The carryover stays last.
- Blocking work in the core (file reads, directory scans) goes through
  `asyncio.to_thread`.
- `self.config` is mutated in place, never rebound.
- Never write into the real `state/` from a test or a scratch script. Give it
  a temp directory in every config path you pass.

## Adding a command

Every row of the table under "The command node is a listing" in
`docs/architecture.md` must change: `Session.command` in `source/core.py`,
`COMMANDS` in `source/chat.py`, `COMMANDS` in `web/app.js` (a hard gate — a
word missing there is unreachable in the browser), `COMMAND_WORDS`, `HELP` and
`_register_commands` in `source/discord_client.py`, the README command table,
and the Mermaid `Cmds` node in `docs/architecture.md`. A read-only command
goes above the busy guard in `Session.command`, so it answers mid-turn.

## Docs

The rules are at the top of `docs/README.md`. The ones that get broken:

- Present tense, no history: no "used to", "now", "since the fix".
- Name symbols, not line numbers.
- A doc that contradicts the code is a bug in the doc. Fix it in the same
  change.
- `docs/architecture.md`'s file table gives exact `wc -l` figures. Update the
  rows for every file you changed.

## The gates

If `tests/` exists in this checkout (it is gitignored, so a clone may lack it):

    .venv/bin/python tests/run_all.py            # fast tier: run after every change
    .venv/bin/python tests/run_all.py --full     # before a commit

A failing check names what is wrong and usually where to type. Fix the code or
the doc it names. Never weaken a check to make it pass. If a check looks wrong,
show which side is stale before you edit either one.

## Committing

- Bump `__version__` in `source/version.py` in every commit: minor for a
  feature, patch for a fix, major for a break in `config.yaml` or the on-disk
  state.
- A new `config.yaml` section goes in `SECTION_KEYS` (`source/config.py`) and
  in `config.example.yaml`, or the config check fails.
- Commit only when asked to.
