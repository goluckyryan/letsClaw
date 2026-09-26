# TODO

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

### Still not built (unchanged from the plan)

Draft streaming (live-edited messages), Discord-native slash commands,
multi-account, and per-channel behavior files. The last one still needs the
small core change the plan anticipated — behavior is resolved per model today,
not per session — and is now listed under Future Features in the README.

### Not yet done

**The live test.** Items 1 and 2 of the plan's testing section are written and
green, and `test_discord_live.py` covers the core protocol without Discord.
Item 3 — a real bot from the dev portal, Message Content Intent on, driving a
real channel — has not been run: it needs a token. The gateway login path is
exercised as far as Discord's 401, so the token and intent plumbing is known to
reach Discord and come back.
