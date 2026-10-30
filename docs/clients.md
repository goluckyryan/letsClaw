# The three clients

All three attach to the same core over the same WebSocket
([protocol.md](protocol.md)) and hold no conversation state. They differ in
what they render and how hard they try to stay connected.

| | `source/chat.py` | `web/app.js` | `source/discord_client.py` |
|---|---|---|---|
| lines | 749 | 2983 | 2555 |
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
tool calls and results, and the `⚡` stats line.

**Three flags, and no config file of its own.** `-s`/`--session` picks the
session name (default `terminal`), `--url` points at a core somewhere other than
the `core.bind`/`core.port` in the config, and `--token` passes the shared secret
when the core wants one — or set `CLAW_TOKEN`. A client on the same machine as
the core picks the token up from the local `config.yaml` without being told, so
on the box that runs the core the flag is usually unnecessary. `terminalUI.sh`
passes its arguments straight through, which is why the wrapper has no options
of its own.

**Tab completes** commands, and model names after `/model `. The completion list
prints each command with its description rather than a bare column of names.
`/quit`, `/exit` and `/reasoning` are handled here — they change this window, not
the conversation — so they never reach the core; everything else goes out as a
`command` message and comes back as a `response` to this client alone.

**Readline needs a blocking read**, so input runs on its own thread and is fed to
the event loop through a queue: the prompt stays editable while an answer streams
in above it. That thread is also why Ctrl-C has to be handled the way below
rather than the obvious way.

**Ctrl-C stops the turn in flight.** `install_ctrl_c` puts a handler on the
event loop, so mid-turn Ctrl-C queues exactly what a typed `/stop` queues and
the prompt stays up — a half-typed line survives it and can be sent afterwards.
At an idle prompt it means what EOF means: leave, core still running. It is a
loop handler and not a `signal.signal` one because a tty raises SIGINT to the
whole foreground group, and a Python-level handler reaches the thread parked in
`input()`; the loop's wakeup fd absorbs the signal first and that read simply
restarts. Before this, Ctrl-C closed the socket and **the turn kept running**
on a core that outlives its clients, with nothing on screen to say so.

Attaching to a session with a turn already in flight counts as mid-turn: no
`turn_start` reaches this client (it sends no `last_seq`, so the event is not
replayed), so the greeting's `busy` flag is the only witness. It is a flag of
its own rather than `in_turn`, because `in_turn` also decides where the prompt
gets redrawn and an attach-mid-turn has always rendered as an idle prompt.

**Closing the window is not the same as Ctrl-C.** Closing the terminal drops the
socket and the turn goes on without you; reattach and what you missed is
replayed.

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
`show_image` tool puts them — newest first and capped at 500, so a plot from a
conversation you cleared, or one scrolled past the log's node cap, is still on
disk and still in this list. Clicking a tile opens the same lightbox an inline
image does; a `this session` toggle narrows the grid to what the open transcript
is showing — a scan of the log **when the panel opens**, not a list kept as the
session runs, so it cannot go stale, and its one limit is that an image trimmed
away by the log cap is no longer "on screen". 🗑 deletes the file itself, after
a confirm, through `DELETE /files/<name>`, unless `webui.files_delete` is false
— which takes the button away entirely, for a `files_dir` whose contents are not
disposable. It is a second overlay built from the Settings panel's own box and
close button, deliberately: two hand-rolled modals drift apart on the first
theme change.

The tiles are the full images scaled by CSS. There is no thumbnailer, because
that means a dependency on a machine this core cannot assume is online; lazy
loading, a reserved tile height and the 500-file cap are what make that
affordable. The first open of a large directory is slow — the fix if it ever
bites is a thumbnail route, not more JavaScript.

**Inline pictures.** An answer containing `![a plot](run42.png)` renders it
inline, click for full size. Only a flat filename with an image extension is
accepted, refused twice over — see trap 14 in
[architecture.md](architecture.md#traps). A src that is refused is shown as its
own literal text rather than quietly dropped, and a file that is not there
becomes a visible note instead of a broken-image icon. There is no upload:
pictures go agent → you, not the other way. `show_image` costs **121 tokens of
schema** on every request of every tool-enabled session
([tokens.md](tokens.md)) — drop it from `tools.allow` if you never want
pictures.

**Maths** rides the same page: `$…$` and `\(…\)` inline, `$$…$$` and `\[…\]`
centred, via the KaTeX vendored into `web/vendor/`. The ordering rules that make
`echo $HOME` stay literal are trap 15 in
[architecture.md](architecture.md#traps).

Neither panel exists in the terminal or on Discord, and neither is a slash
command. They are buttons, like the sidebar's `+`, `✎` and `✕` — a WebUI
affordance over an HTTP route, not a protocol feature two of the three clients
would have to refuse.

Does the most: session sidebar (polling `GET /sessions` every 5 s), markdown
rendering, foldable thinking blocks with a live token count (`🧠 think` cycles
off / folded / open, and each block overrides it), rename and
delete, the model picker, the rollover prompt, and auto-reconnect with
exponential backoff that replays from `last_seq`.

**The 📄 button is a three-position wheel** in the think button's style —
**off** → **collapse** → **expand** → **off** — where the position is the
wheel's own: each click advances it and sends the command the new position
names, `collapse` turning the digest on, `expand` undoing the last collapse,
`off` turning it off. Both on positions light the button's on lamp, and the
server's state still owns that lamp and the collapsed-turn count; a broadcast
that contradicts the wheel pulls it back — a tab that sees the session go off
from elsewhere snaps to off — but between those, the wheel is the tab's, as the
think button's mode is. A reattach learns the position from the `/info` it
fetches. The wheel's `expand` with nothing collapsed is a no-op the core reports
as "nothing to expand" rather than refusing — a click is never an error — and
expanding more than one turn is going around the wheel again, or a typed
`/digest expand` (`all` stays a typed argument, never a button).

Nothing is streamed *into* the wheel's state: `/digest`'s read-only report is
what a fresh tab reads, so the button cannot claim a mode the core has never
been told about.

Beside **Send**, `↑`/`↓` jump to the previous and next **question** in the
transcript — the log, not the composer, so the answer stays below it. That is
separate from `↑` in an empty box, which recalls what you last *typed*. A
`/steering` correction is a user message but not a question, so it is stepped
over. The jump makes **no** allowance for the pinned band, and an early version
that did was measured landing in exactly the same place: the question you are
on is the pinned one, so every earlier question `↑` can reach sits above it,
and scrolling to one carries the pin's own flow position below the top of the
pane, where sticky lets go of it. The target lands where it can be read because
the pin gets out of the way by itself, not because `scrollToPrompt()` works
around it.

The token is entered in the page and kept in `localStorage`; it goes out as
`?token=` because a browser cannot set a WebSocket header.

> ⚠️ **`PROTOCOL_VERSION` is hard-coded as `1` in `app.js`** while the two
> Python clients import it from `core.py`. Bumping the protocol means
> editing this line by hand.

## Discord — `source/discord_client.py`

Its own process (`./run_discord.sh`, or `./serve.sh` starts both). Five parts:
settings, the link (socket), the bridge (Discord ↔ core), the gate
(authorisation), and the bot.

**The core never imports `discord.py`** and does not know it exists; the bot
holds one WebSocket per active channel and translates between Discord messages
and core events. Restarting the bot costs nothing, because the session on the
core's side *is* the conversation.

### Setup

Make a bot at <https://discord.com/developers/applications> → New Application →
Bot. Copy its token into `config.yaml` under `discord.token` (`config.yaml` is
gitignored, so it stays put), and switch **Message Content Intent** on under
Privileged Gateway Intents — without it every message arrives with empty text.
Invite it with the `bot` scope and Send Messages + Read Message History. Then put
your own user ID in `discord.users` (Discord → Settings → Advanced → Developer
Mode, then right-click yourself → Copy User ID).

`serve.sh` starts the bot whenever `discord.enabled` is not false and
`discord.token` is set, and says which of the two was missing when it does not —
so it stays the right command either way. The question is answered by
`source/discord_probe.py`, whose exit code both `serve.sh` and `install.sh` read:
they used to each carry their own copy of it, and the copies drifted.
`discord.enabled: false` is the off switch — the bot does not start, and
`./run_discord.sh` refuses to run while it is set, rather than failing
mysteriously. `./serve.sh --no-discord` leaves it out for one run.

### One channel is one conversation, named after itself

| where | session |
|---|---|
| guild channel | `<server>-<channel>` — `dudus-den-solaris` |
| thread | the thread's own name, its own conversation, not the parent's |
| DM | `discord-dm-<user_id>` — a DM has no channel name to use |

The server prefix is not decoration: two servers each having a `#general` is the
normal case, and without it their conversations would silently merge into one
session. `auto_names: false` goes back to bare `discord-<channel_id>`.

**The snowflake is the key, not the name.** The bot records which name it last
gave each channel id in `state/discord_names.json`, and when Discord's name no
longer matches it renames the *core session* rather than starting an empty one —
so renaming `#esps` carries its conversation across. `migrate_names()` brings
sessions named under the old scheme across at startup. If the rename is refused
(the session is mid-turn, or the new name is taken) it keeps using the old name
and tries again next time, rather than splitting the conversation in two.

**A rename is followed, so a reconnect lands on the right session.** The core
*creates* a session on an unknown name, so a reconnect that still dials an old
name would open an empty twin while the real conversation sits under the new one.
A rename the bot drove itself (a Discord channel rename) is already in its name
map; a rename from elsewhere (the WebUI) is adopted and the channel is bound to
the new name, because a WebUI rename is user intent, exactly like
`/session <name>`. Either way the next reconnect dials the conversation, not a
twin.

**Pointing a channel at another session.** One-to-one is the default, not a wall:
`/session <name>` (or `!session <name>`) repoints the channel at any session the
core already has — or creates it if it does not. The old conversation is untouched
and stays reachable from wherever else it is bound; the channel stops talking to
it and starts talking to the other one, and the last three lines of the session it
joined are posted so you can see where it landed. `/session` with no name shows
the current binding, and `/session auto` brings the channel back to its default
name. The mapping is persisted in `state/discord_session_map.json`, so it survives
a bot restart — and a mapped channel is left alone by the rename-following logic,
which is user intent, not a name the bot chose. Several channels can share one
session; `/info` in any of them says which channel it is bound from.

### Authorisation is a hard-required allowlist

> ⚠️ **A bot is a remote shell.** The tools are unconfined, so anyone who can get
> a message to this bot can run commands on the machine hosting the core. Anyone
> on Discord can DM a bot by default, and inside a guild the only wall is
> Discord's own channel permissions. So `discord.users` is a **required**
> allowlist of snowflake IDs: an empty list refuses everybody, which is the
> startup default rather than an oversight.

A stranger who addresses the bot gets one refusal per ten minutes carrying their
own user id so the owner can add them; a stranger who was not addressing it gets
silence.

**Taking someone's access away is a `/reload`, not a restart.** `discord.users`,
`mention_only` and `mention_list` are re-read by every attached bridge when the
core announces a reload, and nothing else in the `discord:` section is touched —
a reload is not a reason to re-point a running bot at a different core. It fails
closed: a `config.yaml` that will not parse leaves the allowlist already in force
rather than widening it. An allowlist that needed a restart would not be one, and
for a while this was exactly that — removing a snowflake and reloading printed
"♻️ config reloaded" and changed nothing at all.

In a guild the bot answers when @mentioned or replied to — hygiene, so it stays
quiet in a busy channel, not a security control. DMs never need a mention.
`mention_only` takes four values:

| | |
|---|---|
| `true` | always address it (the default) |
| `false` | it answers everything |
| `white` | address it **only** in the channels in `mention_list` |
| `black` | address it **everywhere except** those channels |

`mention_list` takes channel names, `#name`, `server/channel` when two servers
share a channel name, or raw snowflakes — matched as slugs, so
`#Ptolemy-Translation` and `ptolemy-translation` are the same entry. A bare name
matches that channel in *every* server. Rather than ask you to remember which way
round `white` and `black` go, the bot prints the resolved answer for every channel
it can see at startup:

```
must @mention in: #general, #novel
answers freely in: #solaris, #caen, #esps, #ptolemy-translation, #analysis_11c
```

An unreadable value warns and falls back to `true`, so a typo never opens the bot
up.

### What it renders, and what it refuses to

Renders text and one small grey line per tool call. No reasoning and no stats —
and it is still a complete client, which is the useful thing to know if you are
writing a fourth. `reasoning=0` is asked at attach, so none of the thinking ever
crosses the wire; `/reasoning` is answered but does nothing here.

**The answer arrives once, at the end.** Nothing is streamed: deltas are buffered
and posted on `turn_end`, which keeps the reader loop off the Discord API so a
slow Discord can never stall a turn. A typing indicator runs meanwhile.

**The tool line is not progress reporting.** The line exists because a tool round
would otherwise be a *gap*. The `<tool_call>` block is lifted out of the text
stream by `ToolMarkupFilter`, but the newlines around it are not, so a round the
model did not introduce arrived as a blank band in the middle of the answer.
`_tool_call()` closes that band and writes what the call was for — the `reason`
every tool schema asks for (`tools.py:REASON_KEY`), or the tool's name when the
model did not give one:

```
Let me check the bus.

⚙️ checking the USB bus ↳ 412 chars

No CAEN 21e1 device on dcs0's buses.
```

The `reason` never reaches the handler; it exists only to be shown. The choice is
deliberate: a reader on a phone wants to know *why* the agent went away for ninety
seconds, and the command itself is both longer and, on a
`cd … && grep -n … | sed -n …`, much less informative. The raw arguments are still
in the WebUI and the terminal. The line is part of the answer, not a message of
its own, so a turn that ran twenty commands is still one post.
`show_tools: false` drops the line and keeps the trimming: the blank was a bug,
not a setting.

**Answers are chunked** by `source/chunker.py` — 2000 characters, a soft 17-line
cap, and fences closed and reopened so a code block never leaks into the next
message. **Every chunk is posted, however many.** There was a cap, `max_messages`,
which posted the opening and filed the rest as an `answer.md` attachment; it was
removed, because filing the tail turned a readable thread into a download at
exactly the length that most needed reading. The length is bounded anyway — an
answer cannot exceed the model's `max_output_tokens`, about 65 messages on a 32k
model and 16 on an 8k one — and discord.py waits out a 429, so a long burst is
slow rather than lost. Only a chunk Discord actually refuses falls back to the
whole answer as an `answer.md` attachment (`_attach_answer()`), saying which
message failed rather than dropping the rest in silence.

**The rollover question** is posted like any other, and `yes`/`no` answers it —
addressed the normal way, so replying to the question works. Not answering within
`prompt_timeout` keeps the session, exactly as in the other clients. With
`mention_only: false` a bare "yes" in the channel can also answer it, which is one
more reason to leave it on.

**The channel stays attached** for the life of the bot by default
(`idle_detach_min: 0` = never). That is what keeps Discord and the WebUI in
lockstep: a turn you run from the WebUI still posts to the channel, because the
bridge never detaches and misses it. Set `idle_detach_min` to a number of minutes
to get the old behaviour back — the socket drops when the channel goes idle that
long and re-attaches on the next message; the conversation is on the core, not
here, so nothing is lost. The link reconnects on its own with backoff and resumes
by `last_seq`, so a core restart mid-answer loses nothing.

### Commands

The slash commands work as plain messages: `/info`, `/model [name]`, `/rollover`,
`/new`, `/clear`, `/behavior`, `/stop`, `/reload`, `/restart`, `/session [name]`,
plus a local `/help`. Two are Discord-only or WebUI-only, in each direction:
`/session` maps a *channel* to a session, and the terminal and the browser pick
their session when they attach; `/steering` is the other way round, terminal and
WebUI only. `/restart` restarts the **core** — the bot is a separate process, stays
up, and reconnects on its own backoff.

Slash commands are registered as a real `app_commands.CommandTree`
(`_format_info()`), synced per guild (immediate) and globally (needed for DMs, up
to an hour to propagate), guarded to run once per process rather than per
reconnect.

---

## The `/digest` command

The config answers what every session should do; `/digest` answers what *this*
session will do. It is in every client — terminal, WebUI, Discord — and the
WebUI's toolbar carries it as a 📄 button. What the collapse is, what it costs and
why it is gated on the journal is
[architecture.md](architecture.md#digest); this is the operator's half.

- **`/digest off`** and **`/digest collapse`** are state, and the session's own.
  While set, the choice outranks both config layers, so a config edit, a `/model`
  switch, a `/clear` or a restart cannot take it back — it is written to the
  session's live file, which is what carries it across a restart. A session that
  never chose follows the config as before.
- **`/digest expand`** and **`/digest expand all`** are the undo of a collapse,
  not a state: the last collapsed turn back out of the journal, or every collapsed
  turn in the window. The re-inserted turn is bigger than the digest it replaces,
  which is the whole of the risk, so `all` is opt-in.
- **`/digest`** with nothing after it is the read-only report: on or off, by config
  or by you, and how many collapsed turns an expand would reach. It answers while a
  turn is running, for the same reason `/info` does.

The 📄 button is a three-position wheel in the think button's style — **off** →
**collapse** → **expand** → **off** — and the mechanics of why the wheel may own
its position while the server owns the lamp are under
[Browser](#browser--web).

---

## What is shared, and what is not

**Shared:** the protocol constant (in the two Python clients), and
`source/chunker.py` — one file rather than a copy per client, though the bot is
currently its only importer.

**Duplicated:** everything else. Each of `/info`, `/behavior`, `/model`,
`/reload`, `rollover_ask`, `session_state` and `stats` has **two or three
independent renderers**.

This is the codebase's real maintenance tax, and it is worth naming plainly:
**adding a field to a command's reply means editing up to three renderers, and
nothing will tell you if you miss one.** The *fields* are untested; the
*command lists* are not. `tests/test_command_surface.py` checks every client's
table against the core's own dispatch, after the three had drifted far enough
that `/models` was reachable in the terminal and on Discord while the WebUI
rejected it outright.

Whether to fix it is a genuine trade. A shared renderer would have to abstract
over ANSI, HTML and Discord markdown, which are not very alike; the duplication
is what keeps each client readable on its own. But if you are adding a fourth
client, or changing a command's reply shape, this is the thing that will bite.
