# Discord client — a third client on the core's WS protocol

**Shipped 2026-09-25** · commit `6b1ea97` · superseded nothing

Moved here from `models/todo.md`, where it was a completed-work note. This is
the record of what was built and which decisions were made along the way; the
[README's Discord section](../../README.md#discord-client) is the user-facing
documentation, and [clients.md](../clients.md) is the code map.

---

## The shape

A thin client like the WebUI and the terminal, in its own process, attaching to
the core over the existing WebSocket protocol. **The core never imports
`discord.py` and does not know Discord exists** — see
[design principle 1](../design-principles.md#1-the-core-owns-the-conversation-clients-are-windows-onto-it).

| file | |
|---|---|
| `source/discord_client.py` | the client — settings, link, bridge, gate, bot |
| `source/chunker.py` | the fence-aware chunker, split out so it tests without `discord.py` |
| `run_discord.sh` | launcher, 4 lines like the others |
| `tests/test_chunk.py` | 15 cases — the ported `chunk.test.ts` set plus CJK and the real 2000 boundary |
| `tests/test_discord.py` | 48 cases — gate, routing, rendering, all on fakes |
| `tests/test_discord_live.py` | 7 cases — a real core on a temp config, protocol end to end |
| `source/server.py` | the two passthroughs below |

## The two open decisions, resolved

### `?model=` in `server.py`: **included**

Along with a second one-liner for `origin`. Both were needed to make features
the spec already described actually work, and `mgr.get()` already took a model.

- `?model=` applies **only when the attach is what creates the session**;
  re-attaching never switches a live one, since that would move the
  conversation out from under anyone else attached to it.
- `origin` is clamped to 64 characters and falls back to the session name.

`tests/test_discord_live.py` covers both against a real core.

### `dm_allowlist`: **replaced by a hard-required `users` allowlist**

Not DM-scoped, and not warn-and-open. **A bot is a remote shell**, so
`discord.users` gates every surface, and an empty list — the default — refuses
everybody, including in guild channels.

A stranger who *addresses* the bot gets one refusal per ten minutes carrying
their own user ID, so the owner can add them. A stranger who was **not**
addressing the bot gets silence — passing chatter never draws a refusal.

## Deviations from the original plan, and why

- **The chunker is its own module** (`chunker.py`), not part of the ~450-line
  single file. Its test suite was item 1 of the plan, and a pure string module
  tests without `discord.py`, a token or a loop.
- **Long answers become a file.** Past `max_messages` (default 8) the opening
  chunks go out and the whole answer follows as `answer.md`. The plan did not
  say what to do with a forty-chunk answer, and forty messages is not it.
- **`reasoning=0` on the socket.** The thinking is never drawn here, so it is
  never shipped — the plan did not mention the flag, but the core already had
  it.
- **No `chunkMode: "newline"`.** That half of openclaw's chunker serves draft
  streaming, which is a v1 non-goal; its one test case went with it.

## Shipped since, beyond the original plan

- **Discord-native slash commands** — `_register_commands()`
  (`discord_client.py:1266`) builds an `app_commands.CommandTree`: `/info`,
  `/models`, `/rollover`, `/clear`, `/new`, `/behavior`, `/stop`, `/reload`,
  plus `/model` with autocomplete and a dropdown picker, and `/ask`. Synced per
  guild (immediate) *and* globally (needed for DMs, up to an hour to
  propagate), guarded so it happens once per process rather than per reconnect.
- **Auto-naming and rename-following** — `<server>-<channel>` slugs, with the
  channel id → name record in `state/discord_names.json` and `migrate_names()`
  bringing existing sessions across at startup. A 404 on the rename counts as
  success: there was nothing to move.
- **`mention_only: white`/`black`** with `mention_list`, resolved and printed
  per channel at startup so nobody has to remember which way round they go.
- **`/new` is now the destructive one.** `/rollover` archives and carries a
  handoff; `/new` wipes the conversation *and* every archive this session id
  ever wrote (`session.purge_archives()`). The reflexive keystroke is the safe
  one — [principle 10](../design-principles.md#10-the-reflexive-keystroke-is-the-safe-one).
- **Tool-call recovery on the raw transport** — llama.cpp's `/completion` has
  no tool-call parser, so `llm_engine.py` parses the template's own markup
  itself. **Confined to that transport on purpose**: on the chat endpoint the
  same markup arriving as text is the model *writing about* a tool call, and
  running it would be a real hazard.

## Still not built

Draft streaming (live-edited messages as the answer builds) and multi-account.
Per-channel behavior files were the other item here and
[shipped on 2026-09-30](2026-09-30-session-behavior-layer.md).

## The live test, still not automated

Items 1 and 2 of the plan's testing section are written and green, and
`test_discord_live.py` covers the core protocol without Discord. Item 3 — a
real bot from the dev portal, Message Content Intent on, driving a real channel
— has not been run as an automated test, though the bot runs against a real
server (`Dudu#1416`, two guilds) and the slash tree publishes.

This is the one gap that cannot be closed with fakes, and it is worth knowing
that everything below the gateway is proven and the gateway itself is not.
