# The three clients

All three attach to the same core over the same WebSocket
([protocol.md](protocol.md)) and hold no conversation state. They differ in
what they render and how hard they try to stay connected.

| | `source/chat.py` | `web/app.js` | `source/discord_client.py` |
|---|---|---|---|
| lines | ~640 | ~2220 | ~2310 |
| default session | `terminal` | `web` | `<server>-<channel>` |
| reasoning | live, dimmed | live, foldable — off / `▸` / `▾` | **off** (`reasoning=0`) |
| stats `⚡` | yes | yes | no |
| tool calls | yes | yes | one grey line each (`show_tools`) |
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
token. If the core restarts, you restart the client — this one only, since the
WebUI and the bot both reconnect on their own. It does at least say so: a close
with code 1012 (`SERVICE_RESTART`, which the core sends only from `/restart`)
prints that the core is coming back and that the conversation is kept, rather
than the generic "the core closed the connection" it printed when a restart
issued from the WebUI silently killed every terminal attached to it. That is
fine for a local REPL and is the first thing to fix if it ever becomes more
than that.

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
with secrets never shown at all — `/config` replaces `core.token`,
`discord.token` and every `models.*.api_key` with `********` before the response
leaves the core, so the panel prints `•••••••• set — in config.yaml` where a
secret lives (there used to be a reveal toggle; it could only ever reveal the
placeholder, so it was removed) — and the restart-only keys
(`core.bind`, `core.port`, `logging.file`, `discord.enabled`) badged.
Only the `webui` section is
editable — its Save button posts to `POST /config`, which rewrites just that
block and reloads it live, so the rest of the file, comments included, is
never touched by the browser.

A **Media** panel (🖼️, beside it in the same sidebar foot, backed by
`GET /files`) grids every picture in `webui.files_dir` — where the agent's
`show_image` tool puts them — newest first and capped at 500. Clicking a tile
opens the same lightbox an inline image does; a `this session` toggle narrows
the grid to what the open transcript is showing, read off the log rather than
tracked as the session runs; and 🗑 deletes the file through
`DELETE /files/<name>`, unless `webui.files_delete` is false. It is a second
overlay built from the Settings panel's own box and close button, deliberately:
two hand-rolled modals drift apart on the first theme change.

Neither panel exists in the terminal or on Discord, and neither is a slash
command. They are buttons, like the sidebar's `+`, `✎` and `✕` — a WebUI
affordance over an HTTP route, not a protocol feature two of the three clients
would have to refuse.

Does the most: session sidebar (polling `GET /sessions` every 5 s), markdown
rendering, foldable thinking blocks with a live token count (`🧠 think` cycles
off / folded / open, and each block overrides it), rename and
delete, the model picker, the rollover prompt, and auto-reconnect with
exponential backoff that replays from `last_seq`.

The token is entered in the page and kept in `localStorage`; it goes out as
`?token=` because a browser cannot set a WebSocket header.

> ⚠️ **`PROTOCOL_VERSION` is hard-coded as `1` in `app.js:37`** while the two
> Python clients import it from `core.py`. Bumping the protocol means
> editing this line by hand.

## Discord — `source/discord_client.py`

Its own process (`./run_discord.sh`, or `./serve.sh` starts both). Five parts:
settings, the link (socket), the bridge (Discord ↔ core), the gate
(authorisation), and the bot.

**One channel is one session**, auto-named `<server>-<channel>`, with the
channel snowflake as the key in `state/discord_names.json` so a channel rename
follows rather than forking the conversation. `migrate_names()` brings existing
sessions across at startup.

Renders text and one small grey line per tool call. No reasoning and no
stats — and it is still a complete client, which is the useful thing to know
if you are writing a fourth.

**The tool line is not progress reporting.** A turn is posted once, at
`turn_end`, so nothing here is live; the line exists because a tool round
would otherwise be a *gap*. The `<tool_call>` block is lifted out of the text
stream by `ToolMarkupFilter`, but the newlines around it are not, so a round
the model did not introduce arrived as a blank band in the middle of the
answer. `_tool_call()` closes that band and writes what the call was for —
the `reason` every tool schema asks for (`tools.py:REASON_KEY`), or the
tool's name when the model did not give one. `show_tools: false` drops the
line and keeps the trimming: the blank was a bug, not a setting.

**Answers are chunked** by `source/chunker.py` (fence-aware, so a code block is
never split mid-fence). Past `max_messages` chunks the opening chunks go out
and the whole answer follows as an `answer.md` attachment
(`_attach_answer()`).

**Authorisation is a hard-required allowlist.** `discord.users` empty — the
default — refuses everybody, everywhere, including guild channels. A bot is a
remote shell. A stranger who addresses the bot gets one refusal per ten minutes
carrying their own user id so the owner can add them; a stranger who was not
addressing it gets silence.

`mention_only` takes `true` / `false` / `white` / `black` (the last two with
`mention_list`), resolved and printed per channel at startup.

Slash commands are registered as a real `app_commands.CommandTree`
(`_format_info()`), synced per guild (immediate) and globally (needed
for DMs, up to an hour to propagate), guarded to run once per process rather
than per reconnect.

Full record: the [Discord Client section of the
README](../README.md#discord-client) — what it does, what it refuses, and how
to configure it.

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
