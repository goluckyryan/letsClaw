# The third behavior layer — `models/sessions/<name>.md`

**Shipped 2026-09-30** · commit `ce58035`

Moved here from `models/todo.md`, where it was a completed-work note.

---

## What it is

The system prompt is four parts now:

```
<base>                 models/base.md                  every session
=== BEHAVIOR ===
<model>                models/<model>.md               per model entry
=== SESSION ===
<session>              models/sessions/<name>.md       per session  ← this
=== CONTINUED SESSION ===
<carryover>            the rollover handoff
```

Assembled by `Session.fresh_history()` (`core.py:539`).

## The decision that makes it work: key on the session name

**Keyed on the session name, so it is per Discord channel without the core
knowing Discord exists** — the bot already names sessions `<server>-<channel>`,
so `models/sessions/dudus-den-solaris.md` *is* the `#solaris` channel's file.

The obvious alternative, keying on a Discord channel id, would have been worse
on two counts: it would have excluded `terminal.md` and every WebUI session,
and it would have put Discord's vocabulary inside the core. See
[design principle 4](../design-principles.md#4-key-on-the-session-name-not-on-the-clients-identity).

**A directory rather than a config map**, because sessions spring into
existence on attach: dropping the file in is the whole operation, and
`ls models/sessions/` says which sessions are tuned.

`behavior.session_dir` switches the layer on; unset and it is off. A session
with no file gets the two-layer prompt it got before.

## Why the carryover stays last

`fresh_history`'s docstring calls this load-bearing, and it is. `carryover()`
(`core.py:573`) recovers the handoff by partitioning history[0] on
`CARRYOVER_MARKER` and taking **everything after it**. Anything appended below
the carryover would be read back as part of the handoff and re-appended on the
next rebuild — **growing the prompt every turn.**

`tests/test_session_behavior.py` has a case for exactly this: the prompt not
growing across repeated rebuilds.

## Path safety

The session name is turned into a filename by the same rule `state/` uses —
everything outside `[A-Za-z0-9-_.]` becomes `_` — and the result must sit
**directly** in `session_dir` or it is refused (`session_behavior_path`,
`core.py:1781`).

Both guards matter: **`?session=` is not validated on attach**, so a session
named `../../etc/passwd` can exist, and this is the one place a session name
becomes a path that is *read*.

## When the prompt is rebuilt

Not frozen at startup. It is rebuilt when a session switches models, rolls
over, is renamed, or takes a config reload — which is also how a changed file
reaches a running session.

`Session.reload_session_behavior()` (`core.py:561`) exists for the rename path
specifically: the name is what picks the file, so a rename has to re-resolve it
and rebuild history[0] with the carryover put back.

Creating a file for an **already-live** session needs a `/reload` to be
noticed, since the read is cached — exactly as editing `base.md` is.

## Dead code deleted first

`llm_engine.py`'s `load_behavior()` / `build_system_prompt()` pair was deleted
before this landed, as the design said to. `build_system_prompt` was a second,
divergent prompt assembler: it hardcoded *"You are letsClaw, a lightweight
technical agent engine"* and emitted `=== BEHAVIOR ===` with no base layer and
no carryover. Neither was called from anywhere in `source/`, `tests/` or
`web/`.

Leaving it would have meant the next reader extending the wrong assembler.
**Verified gone:** `grep -rn 'load_behavior\|build_system_prompt' source/ tests/ web/`
returns nothing.

## Tests

`tests/test_session_behavior.py` — 10 cases: layer order, the missing-file
fallback, the layer being off, the carryover surviving, the prompt *not*
growing across repeated rebuilds, path traversal refused, odd names slugged, a
Discord channel name resolving, and rename re-resolving both ways.

Verified live against a real core as well: a renamed session picked up the new
channel's file with its handoff and conversation intact.

## Not done, deliberately

**Per-*user* behavior inside a shared channel.** One channel is one session and
one conversation, so there is nowhere to hang it. The `origin` field on
`submit` records *who typed*, but it does not fork the conversation, and making
it do so would mean one channel holding N conversations — a different feature.
