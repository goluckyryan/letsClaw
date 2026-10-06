# The three clients

All three attach to the same core over the same WebSocket
([protocol.md](protocol.md)) and hold no conversation state. They differ in
what they render and how hard they try to stay connected.

| | `source/chat.py` | `web/app.js` | `source/discord_client.py` |
|---|---|---|---|
| lines | ~600 | ~1600 | ~1300 |
| default session | `terminal` | `web` | `<server>-<channel>` |
| reasoning | live, dimmed | live, collapsible | **off** (`reasoning=0`) |
| stats `⚡` | yes | yes | no |
| tool calls | yes | yes | no |
| reconnect | **none** | auto, backoff | auto, plus idle-detach |
| `last_seq` | **not sent** | yes | yes |
| auth | env/config | token in the page | config |

---

## Terminal — `source/chat.py`

A REPL. Launch with `./terminalUI.sh`, or `python source/chat.py --session foo`.

Renders everything: text, dimmed reasoning, a live thinking-token counter,
tool calls and results, and the `⚡` stats line. Ctrl-C sends `stop`; a second
one exits.

**It is the least robust of the three, deliberately-by-omission rather than by
design:** no reconnect, no `last_seq`, no auth handling beyond reading the
token. If the core restarts, you restart the client. This is fine for a local
REPL and is the first thing to fix if it ever becomes more than that.

## Browser — `web/`

`index.html` + `app.js` + `style.css`, served by the core from `/`,
plus `GET /theme.css` — generated from `webui.pin` in config.yaml so the
pinned prompt (band color, transparency, text size and color) is a config
setting. The page theme is `webui.theme` (`auto`/`dark`/`light`); it rides
the `hello` and `reloaded` events rather than a stylesheet, because a live
tab cannot be made to re-fetch one — so `/reload` re-themes every open tab
immediately. **Vanilla JS, no build step, no dependencies** — edit and reload.

A **Settings** panel (⚙️ at the bottom of the session sidebar, backed by
`GET /config`) shows every parameter in `config.yaml`, grouped by section,
with secrets masked behind a reveal toggle and the restart-only keys
(`core.bind`, `core.port`, `logging.file`) badged. Only the `webui` section is
editable — its Save button posts to `POST /config`, which rewrites just that
block and reloads it live, so the rest of the file, comments included, is
never touched by the browser.

Does the most: session sidebar (polling `GET /sessions` every 5 s), markdown
rendering, collapsible thinking blocks with a live token count, rename and
delete, the model picker, the rollover prompt, and auto-reconnect with
exponential backoff that replays from `last_seq`.

The token is entered in the page and kept in `localStorage`; it goes out as
`?token=` because a browser cannot set a WebSocket header.

> ⚠️ **`PROTOCOL_VERSION` is hard-coded as `1` in `app.js:37`** while the two
> Python clients import it from `core.py:39`. Bumping the protocol means
> editing this line by hand.

## Discord — `source/discord_client.py`

Its own process (`./run_discord.sh`, or `./serve.sh` starts both). Five parts:
settings, the link (socket), the bridge (Discord ↔ core), the gate
(authorisation), and the bot.

**One channel is one session**, auto-named `<server>-<channel>`, with the
channel snowflake as the key in `state/discord_names.json` so a channel rename
follows rather than forking the conversation. `migrate_names()` brings existing
sessions across at startup.

Renders text only. No reasoning, no stats, no tool traffic — and it is still a
complete client, which is the useful thing to know if you are writing a fourth.

**Answers are chunked** by `source/chunker.py` (fence-aware, so a code block is
never split mid-fence). Past `max_messages` chunks the opening chunks go out
and the whole answer follows as an `answer.md` attachment
(`discord_client.py:606`).

**Authorisation is a hard-required allowlist.** `discord.users` empty — the
default — refuses everybody, everywhere, including guild channels. A bot is a
remote shell. A stranger who addresses the bot gets one refusal per ten minutes
carrying their own user id so the owner can add them; a stranger who was not
addressing it gets silence.

`mention_only` takes `true` / `false` / `white` / `black` (the last two with
`mention_list`), resolved and printed per channel at startup.

Slash commands are registered as a real `app_commands.CommandTree`
(`discord_client.py:1266`), synced per guild (immediate) and globally (needed
for DMs, up to an hour to propagate), guarded to run once per process rather
than per reconnect.

Full record: [decisions/2026-09-25-discord-client.md](decisions/2026-09-25-discord-client.md).

---

## What is shared, and what is not

**Shared:** the protocol constant (in the two Python clients), and
`source/chunker.py`.

**Duplicated:** everything else. Each of `/info`, `/behavior`, `/models`,
`/reload`, `rollover_ask`, `session_state` and `stats` has **two or three
independent renderers**, and the three command tables have drifted apart.

This is the codebase's real maintenance tax, and it is worth naming plainly:
**adding a field to a command's reply means editing up to three renderers, and
nothing will tell you if you miss one.** There is no test that the three
clients agree.

Whether to fix it is a genuine trade. A shared renderer would have to abstract
over ANSI, HTML and Discord markdown, which are not very alike; the duplication
is what keeps each client readable on its own. But if you are adding a fourth
client, or changing a command's reply shape, this is the thing that will bite.
