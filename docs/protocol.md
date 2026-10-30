# The core protocol

Everything a client needs. The core is `source/server.py` (transport) over
`source/core.py` (state); a client is anything that can hold a WebSocket and
parse JSON. There are three today — see [clients.md](clients.md) — and the
protocol is the only thing they share.

**Protocol version** is `core.PROTOCOL_VERSION`, currently `1`.
`chat.py` and `discord_client.py` import it; **`web/app.js` hard-codes
it** and must be kept in sync by hand. Bumping it means editing two places.

---

## HTTP

Routes registered in `build_app()`.

| Method + path | Auth | Returns |
|---|---|---|
| `GET /health` | **no** | `{ok, proto, version, sessions, restarting}` — open so a monitor can check liveness without holding the secret. `version` is the core's release (`source/version.py`), `proto` the wire version below, `restarting` true only inside the tenth-of-a-second grace window, so a poller that catches it has something to connect a healthy read to the refused connection after it |
| `GET /models` | yes | `{default, configured[], forwards}` — `forwards` is one entry per ssh tunnel the core owns, each `{target, jump, remote, local, pid}`, with `pid` null when the tunnel is down. No shipped client renders it: the WebUI and the bot read `configured` only, so it is there for a person at a terminal deciding whether a refused connection is the model or the tunnel |
| `GET /sessions` | yes | `{sessions: [...]}` — the WebUI polls this every 5 s |
| `GET /sessions/{name}/history` | yes | `{ok, session, messages[]}` — the conversation's tail, newest last, `?limit=` (10 by default, `limit<1` clamped to 1), system message omitted. `404` for an unknown session. Read-only and capped deliberately: its one caller is the Discord bot's switch digest, which posts the last three lines when a channel moves sessions, and an unbounded copy of a long session would be a slow JSON for no one |
| `DELETE /sessions/{name}` | yes | 404 if no such session, 409 if a turn is running |
| `POST /sessions/{name}/rename` | yes | body `{"to": "..."}`. 400 malformed / bad name, 404 missing, 409 busy or taken |
| `POST /reload` | yes | re-reads `config.yaml` in place; **400 and no change** if it will not load |
| `GET /config` | yes | `{path, config}` — the live config dict, for the WebUI Settings panel. `core.token`, `discord.token` and every `models.*.api_key` come back replaced by `********` — redacted rather than dropped, so the panel can still tell "set" from "unset", and there is nothing here to reveal. Authentication is not what protects the secrets (`_redacted` does that, because a secret in a response body is in devtools, in a HAR export and on screen during a screen-share); it protects the *shape* of the install — which model servers, which paths, which ssh forwards, which hosts are allowed |
| `POST /config` | yes | body `{"webui": {...}}` — rewrites **only the `webui:` block** of `config.yaml` (comments and every other byte untouched; the block is appended if missing), then applies it via the ordinary reload. 400 malformed or unserialisable |
| `GET /files` | yes | `{ok, dir, total, truncated, can_delete, files[]}` — what is in `webui.files_dir`, newest first, capped at 500. Each entry is `{name, size, mtime, type}`. Top level only: the read route below serves a nested path, but no answer can reference one |
| `GET /files/{name}` | yes | one image from `webui.files_dir`. Confined to that directory (a traversal or a symlink out of it is 404) and restricted to an image extension. **The one route that must also accept the token by `?token=`**, because its caller is an `<img>` loader and an `<img>` cannot send a header — every authenticated route takes a query token (`_authorised()` reads both), this is just the one whose caller has no choice |
| `DELETE /files/{name}` | yes + same-origin | unlink one image. Same guards as the read; **403** when `webui.files_delete` is false, 404 for anything the read route would also refuse, 500 with the message if the unlink fails |
| `GET /ws` | yes | the stream (below) |
| `GET /` , `GET /static/*` | **no** | the WebUI from `web/` — registered only if `web/index.html` exists |
| `GET /theme.css` | **no** | generated CSS from `webui.pin` in config.yaml — the style of the pinned prompt; `/reload` re-themes it |

**Auth** (`_authorised()`): the token is `core.token`; empty
means everything is open. Accepts `Authorization: Bearer <tok>` **or**
`?token=<tok>`. Rejection is a 401.

The **Auth** column above says `yes` for the token and understates the writes.
Every route that changes state — `DELETE /sessions/{name}`, `POST
/sessions/{name}/rename`, `POST /reload`, `POST /config`, `DELETE /files/{name}`
— **and `GET /ws`** call `_guard`, which additionally rejects a cross-origin
`Origin`; the read-only GETs call `_read_guard`, which checks the token only.
The WebSocket is the one worth the words: a socket is exempt from the browser's
same-origin policy by nature, which is exactly why the core checks its `Origin`
by hand. A fourth client that sends an `Origin` header from another port gets a
403 that the token alone will not explain — omit the header (as `curl` and a
native client do) or come from the same origin.

`/` is unauthenticated on purpose — the page holds no secrets and is the thing
that *asks* for the token.

> `/static/` serves the whole `web/` directory. The test helpers that used to
> sit there — `test_md.mjs`, `browser_test.py`, `shot.py` — now live in `tests/`,
> which is not served, so the route exposes only the page and the vendored KaTeX.

---

## WebSocket — `GET /ws`

### Query parameters

| param | default | |
|---|---|---|
| `session` | `terminal` | the name to attach to, **created on demand** |
| `last_seq` | `0` | replay cursor — everything after this seq is re-sent |
| `reasoning` | on | `0` withholds `reasoning` deltas (never `reasoning_stat`) |
| `model` | — | **only applies when this attach creates the session** |
| `token` | — | auth fallback for browsers, which cannot set WS headers |

`?model=` not switching a live session is deliberate: it would move the
conversation out from under anyone else attached to it. Switching is `/model`'s
job.

`?session=` is **not validated** — a session named `../../etc/passwd` can
exist. The guard is at the point the name becomes a path that is read
(`Session.roll_over()`).

### Attaching is a connection, not a handshake

There is no attach message. The name is a query parameter and `hello` — the
first event on every connection — carries the snapshot back. **So reconnecting
is the same operation as connecting.**

The order in `ws_handler()` is load-bearing:

1. `session.attach(sub)` — so nothing emitted during the greeting is lost
2. send `session.snapshot(last_seq)` — the `hello` event
3. start the writer task — last, so nothing overtakes the greeting

### Client → core

Six verbs, all JSON text frames keyed on `"t"`. Malformed JSON gets a
non-fatal `error`; non-text frames are ignored.

| `t` | fields | notes |
|---|---|---|
| `submit` | `text`, `origin?` | empty text ignored; a turn already running returns `busy`. `origin` is clamped to 64 chars and falls back to the session name — Discord sends `discord:<username>` so one channel's shared session still records who typed |
| `command` | `name`, `args`, `request_id` | the reply comes back as `response` **to this client only** |
| `rollover_reply` | `request_id`, `yes` | a stale or duplicate reply is dropped at debug level |
| `stop` | — | cancels the turn task |
| `steering` | `text` | a correction for the turn in flight: queued as a user message, interrupts the streaming round, and is answered by the next round. With no turn running the core answers with a `notice` |
| `ping` | — | → `pong` |

`submit` runs as `asyncio.create_task(session.run_turn(...))` and is **never
awaited inline**, so the reader stays live for `stop` and `rollover_reply`
during a long turn.

**Commands** (`Session.command()`), name lowercased and a leading
`/` stripped. Three groups, not two, and the third is the one that was being
filed under the first:

- **read-only**, reply to the asker: `info`, `behavior`, and the bare forms of
  `model` (the listing) and `digest` (the state) — answered above the busy
  guard, because the moment you want to know is while a turn is running.
  (`models` used to be a command of its own returning the first of those; it
  was a second spelling of bare `/model` and is gone.)
- **manager-level**, above the busy guard and outside the turn lock: `reload`,
  `restart`. They act on the whole core rather than one session, and taking the
  turn lock here would deadlock — `reload` defers busy sessions, including the
  one that asked. Each does its own refusing instead: `reload` defers, `restart`
  reports every busy session by name and takes `force` to override
- **turn-lock-taking**: `clear`, `rollover`, `new`, `model`, `digest` — these
  return `{"ok": false, "error": "a turn is running…"}` when busy. The last
  two are the pair that splits across the guard: their bare form reports and
  sits in the read-only group above, their argument form mutates and sits here.

`restart` has to sit above the busy guard for a second reason of its own: the
whole content of its refusal is *which* sessions are busy, and below the guard a
`/restart` typed into a busy session would never get past "a turn is running in
this session".

### Core → client

Every event carries `seq` and `session`, stamped by `Session.emit`
(`Session.emit()`).

| `t` | payload | |
|---|---|---|
| `hello` | `session, seq, proto, version, model, budget, busy, theme, pin, messages, rollover{}, missed[], gap` | the snapshot; `version` is the core's release, which the WebUI shows beside its status pill — a cached page cannot report the build it is talking to from a constant of its own; `theme` is `webui.theme` (`auto`/`dark`/`light`) and `pin` is `webui.pin`, the text of the pinned prompt — the WebUI styles its pinned band from `pin` and reads it off this greeting because `/theme.css` is cached and this is the fresh copy; `messages` excludes the system message, `gap` is true when the replay buffer could not cover `last_seq` |
| `turn_start` | `turn_id, text, origin` | the core's echo of your own message |
| `steering` | `turn_id, text` | a `/steering` interjection was taken: the round was interrupted (or the correction queued at a round boundary) and the model re-plans with it. It is a real user message, so a fresh attach sees it in `hello.messages` instead |
| `text` | `delta` | |
| `reasoning` | `delta` | **live only, never replayed**; suppressed for `reasoning=0` |
| `reasoning_stat` | `tokens, …` | **live only**; a reconnect would replay hundreds of superseded counts |
| `tool_call` | `id, name, arguments, …` | emitted **before** the tool runs, so a slow `exec` is visible. `arguments` is the raw JSON string; every tool's schema carries a `reason` key (`tools.py:REASON_KEY`) holding one phrase on why the call was made, which is what a client shows when it will not show the command |
| `tool_result` | `id, name, size` | |
| `stats` | token and timing figures | the `⚡` line |
| `turn_end` | `turn_id` | |
| `notice` | `level` (`info`/`warn`/`error`), `text` | the general "say this to the human" event; 40+ emit sites. `error` is rare and means the opposite of "informational": the thing that failed will keep failing every turn until a person acts. All three clients now separate the levels — `ChatClient.handle()` marks `error` with ❌ and `warn` with ⚠️, the WebUI maps it to the styled `.notice.err`, and `DiscordBridge.handle()` posts `warn` and `error` and stays quiet on `info`. (This row used to say no client distinguished `error`; that was true before the three were fixed, and the line outlived it.) |
| `digest_done` | `turn_id, tokens, digest_text` | `conversation.digest_turns` collapsed the turn that just ended, and this is the digest that replaced it — the summary itself, as `rollover_done` carries `handoff_text`, and for the same reason: it is what the model is shown from here on, so a reader who cannot see it finds out about a lossy one only when the model acts on the gap. Emitted **inside** the turn, so it arrives *before* that turn's `turn_end`; a client that buffers its output until `turn_end` has to hold it back or it prints the summary above the answer it summarises (`DiscordBridge._post_digest()`). The digest's own last line is the `grep` that reaches the journal block it stands in for, so the full working-out is one command away |
| `error` | `msg`, `fatal` | |
| `busy` | `reason` | |
| `session_state` | `what` ∈ `reloaded`/`cleared`/`wiped`/`model`/`renamed`/`deleted`/`digest`/`digest_expanded` | `reloaded` also carries `theme` (the new `webui.theme`); `wiped` and `deleted` also carry `files` + `bytes`: the archives that went with the conversation; `digest` and `digest_expanded` both carry the digest state as `_digest_state()` shapes it — `on`, `set_by` (`you`/`config`), `expandable` — with `digest_expanded` adding `turns`/`messages`/`skipped` for what the undo restored. These two are what the WebUI's 📄 button paints from |
| `rollover_start` / `rollover_ask` / `rollover_done` | | `rollover_ask` is the **only** server→client request. There is no `rollover_failed`: a rollover that could not archive its window says so as an `error` notice carrying the reason, and the conversation is kept. The event existed, carried `reason/error/used/budget`, and no client ever read it — it was removed rather than given a consumer, because the one affordance it could have driven is `/rollover`, which the notice already names |
| `response` | `request_id` + the command's reply | unicast, not broadcast |
| `pong` | — | |

### Replay semantics

`Session.emit()` buffers into `turn_events` for the current
turn:

- consecutive `text` deltas are **coalesced** (append-only, so lossless — and
  it keeps a 2000-delta turn from replaying token by token)
- `reasoning` and `reasoning_stat` are **excluded entirely**
- everything else is buffered verbatim

A fresh attach is served from history; `missed` covers a reconnect.

### Backpressure

The writer task (`_writer()`) is the **sole owner of the socket for
outbound traffic** — the turn never touches it, so a stalled peer cannot stall
generation.

A subscriber's queue has `maxlen=2000` (`Subscriber`). On overflow the client
is **dropped**: close code `TRY_AGAIN_LATER` (1013) with
`b"too slow; reconnect and replay"`. A client that sees 1013 should reconnect
with its `last_seq`.

### Session re-homing

If a session is deleted while you are attached (`session.dead`, handled in `ws_handler()`), your
socket stays open. You get a notice, and your next `submit` or `command`
transparently lands on a **fresh, empty session of the same name** — the
handler detaches, calls `mgr.get(session.name)`, re-attaches and sends a new
snapshot. It uses `session.name`, not the name captured at attach, so a rename
follows the client rather than resurrecting the old name.

---

## Writing a fourth client

The minimum is smaller than it looks:

1. Open `ws://host:port/ws?session=<name>`.
2. Read `hello`, note `proto` and `budget`.
3. Send `{"t":"submit","text":"…"}`.
4. Accumulate `text` deltas; the turn is over at `turn_end`.
5. Answer `rollover_ask` with `rollover_reply`, or let it time out
   (`prompt_timeout`, which keeps the session).

Everything else — `reasoning`, `tool_call`, `stats`, the sidebar — is
presentation you can skip. The Discord bot ignores `stats` and all reasoning,
and is a complete client.

It does read `tool_call` and `tool_result`, but not to render them live: it
posts a turn once, at `turn_end`, so a tool round would otherwise be a blank
gap in the answer — the `<tool_call>` block is lifted out of the text stream
but the newlines around it are not. A client that buffers a turn has to
handle the two events for that reason alone, even if it shows nothing.

Things worth copying from the existing three:

- **reconnect with `last_seq`**, and treat close code 1013 as "reconnect now"
- **do not render optimistically** — draw your own message when `turn_start`
  echoes it, so every attached client shows the same transcript
- set `reasoning=0` if you will never draw the thinking; the token count still
  arrives as `reasoning_stat`
- send `origin` if your session is shared by several people
