# TODO

## Third behavior layer — done (2026-09-30)

Built as designed. The system prompt is four parts now:

```
<base>                 models/base.md                  every session
=== BEHAVIOR ===
<model>                models/<model>.md               per model entry
=== SESSION ===
<session>              models/sessions/<name>.md       per session  ← new
=== CONTINUED SESSION ===
<carryover>            the rollover handoff
```

Keyed on the session name, so it is per Discord channel without the core
knowing Discord exists — the bot already names sessions `<server>-<channel>`.
`behavior.session_dir` switches it on; unset and the layer is off. A session
with no file gets the two-layer prompt it got before.

Everything on the migration checklist was done, plus `Session.
reload_session_behavior()` for the rename path. The dead `load_behavior()` /
`build_system_prompt()` pair in `llm_engine.py` was deleted first, as the
design said to.

`tests/test_session_behavior.py` — 10 cases: layer order, the missing-file
fallback, the layer being off, the carryover surviving, the prompt *not*
growing across repeated rebuilds, path traversal refused, odd names slugged,
a Discord channel name resolving, and rename re-resolving both ways. Verified
live against a real core as well: a renamed session picked up the new
channel's file with its handoff and conversation intact.

Not done, and deliberately: per-*user* behavior inside a shared channel. One
channel is one session and one conversation, so there is nowhere to hang it.

## Dead code to delete

`llm_engine.py:572 load_behavior()` and `llm_engine.py:583
build_system_prompt()` are defined and **never called** — nothing in `source/`,
`tests/` or `web/` references either. `build_system_prompt` is a second,
divergent prompt assembler: it hardcodes *"You are letsClaw, a lightweight
technical agent engine"* and emits `=== BEHAVIOR ===` with no base layer and no
carryover. The real assembly is `Session.fresh_history()` (`core.py:535`).

Delete both before adding the third layer, or the next reader will extend the
wrong one.

---

## Discord client — done (2026-09-25)

Built as specified: a thin client like the WebUI and terminal, its own process,
attaching to the core over the existing WS protocol. See **Discord Client** in
the README for how to run it.

### What shipped

| file | |
|---|---|
| `source/discord_client.py` | the client — settings, link, bridge, gate, bot |
| `source/chunker.py` | the fence-aware chunker, split out so it tests without discord.py |
| `run_discord.sh` | launcher, 4 lines like the others |
| `tests/test_chunk.py` | 15 cases — the ported `chunk.test.ts` set plus CJK and the real 2000 boundary |
| `tests/test_discord.py` | 27 cases — gate, routing, rendering, all on fakes |
| `tests/test_discord_live.py` | 6 cases — a real core on a temp config, protocol end to end |
| `requirements.txt`, `config.example.yaml`, `README.md` | the section and the security note |
| `source/server.py` | the two passthroughs below |

### The two open decisions, resolved

**`?model=` in `server.py`: included**, along with a second one-liner for
`origin` — both were needed to make features the spec already described
actually work, and `mgr.get()` already took a model. `?model=` applies only
when the attach is what creates the session; re-attaching never switches a live
one. `origin` is clamped to 64 chars and falls back to the session name.
`tests/test_discord_live.py` covers both against a real core.

**`dm_allowlist`: replaced by a hard-required `users` allowlist.** Not
DM-scoped and not warn-and-open: a bot is a remote shell, so `discord.users`
gates every surface, and an empty list — the default — refuses everybody,
including in guild channels. A stranger who addresses the bot gets one refusal
per ten minutes carrying their own user ID, so the owner can add them; a
stranger who was not addressing the bot gets silence.

### Deviations from the original plan, and why

* **The chunker is its own module** (`chunker.py`), not part of the ~450-line
  single file. Its test suite was item 1 of the plan, and a pure string module
  tests without discord.py, a token or a loop.
* **Long answers become a file.** Past `max_messages` (default 8) the opening
  chunks go out and the whole answer follows as `answer.md`. The plan did not
  say what to do with a forty-chunk answer, and forty messages is not it.
* **`reasoning=0` on the socket.** The thinking is never drawn here, so it is
  never shipped — the plan did not mention the flag, but the core already had it.
* **No `chunkMode: "newline"`.** That half of openclaw's chunker serves draft
  streaming, which is a v1 non-goal; its one test case went with it.

### Shipped since, beyond the original plan

* **Discord-native slash commands** — `_register_commands()`
  (`discord_client.py:1261`) builds an `app_commands.CommandTree`: `/info`,
  `/models`, `/rollover`, `/clear`, `/new`, `/behavior`, `/stop`, `/reload`,
  plus `/model` with autocomplete and a dropdown picker, and `/ask`. Published
  per guild at startup. Covered by `test_the_slash_tree_is_registered`
  (`tests/test_discord.py:846`). *(This was listed as "still not built" until
  2026-09-29 — it had shipped and the list was stale.)*
* **Auto-naming and rename-following** — `<server>-<channel>` slugs, with the
  channel id → name record in `state/discord_names.json` and `migrate_names()`
  bringing existing sessions across at startup.
* **`mention_only: white`/`black`** with `mention_list`, resolved and printed
  per channel at startup.
* **`/new` is now the destructive one.** `/rollover` archives and carries a
  handoff; `/new` wipes the conversation *and* every archive this session id
  ever wrote (`session.purge_archives()`). The reflexive keystroke is the safe
  one.
* **Tool-call recovery on the raw transport** — llama.cpp's `/completion` has no
  tool-call parser, so `llm_engine.py` parses the template's own markup itself.
  Confined to that transport on purpose: on the chat endpoint the same markup
  arriving as text is the model *writing about* a tool call, and running it
  would be a real hazard.

### Still not built

Draft streaming (live-edited messages as the answer builds), multi-account, and
per-channel behavior files — the last one is the design at the top of this file.

### Not yet done

**The live test.** Items 1 and 2 of the plan's testing section are written and
green, and `test_discord_live.py` covers the core protocol without Discord.
Item 3 — a real bot from the dev portal, Message Content Intent on, driving a
real channel — has not been run as an automated test, though the bot is running
against a real server (`Dudu#1416`, two guilds) and the slash tree publishes.

---

## Housekeeping

* **`config.yaml` has `max_messages: 888888`.** The default is 8. At this value
  the `answer.md` fallback can never trigger, so a long answer goes out as forty
  messages instead of a file. Probably a debugging leftover.
* **Nine files are modified and uncommitted** (+968/−127 against `6b1ea97`),
  most of it the Discord client. Worth a commit before the next change lands on
  top of it.
