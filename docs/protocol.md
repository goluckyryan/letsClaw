# The core protocol

Everything a client needs. The core is `source/server.py` (transport) over
`source/core.py` (state); a client is anything that can hold a WebSocket and
parse JSON. There are three today — see [clients.md](clients.md) — and the
protocol is the only thing they share.

**Protocol version** is `core.PROTOCOL_VERSION` (`core.py:39`), currently `1`.
`chat.py:35` and `discord_client.py:72` import it; **`web/app.js:37` hard-codes
it** and must be kept in sync by hand. Bumping it means editing two places.

---

## HTTP

Routes registered at `server.py:309-319`.

| Method + path | Auth | Returns |
|---|---|---|
| `GET /health` | **no** | `{ok, proto, sessions}` — open so a monitor can check liveness without holding the secret |
| `GET /models` | yes | `{default, configured[]}` |
| `GET /sessions` | yes | `{sessions: [...]}` — the WebUI polls this every 5 s |
| `DELETE /sessions/{name}` | yes | 404 if no such session, 409 if a turn is running |
| `POST /sessions/{name}/rename` | yes | body `{"to": "..."}`. 400 malformed / bad name, 404 missing, 409 busy or taken |
| `POST /reload` | yes | re-reads `config.yaml` in place; **400 and no change** if it will not load |
| `GET /config` | yes | `{path, config}` — the live config dict, for the WebUI Settings panel. Authenticated because `core.token` and `discord.token` are in it |
| `POST /config` | yes | body `{"webui": {...}}` — rewrites **only the `webui:` block** of `config.yaml` (comments and every other byte untouched; the block is appended if missing), then applies it via the ordinary reload. 400 malformed or unserialisable |
| `GET /ws` | yes | the stream (below) |
| `GET /` , `GET /static/*` | **no** | the WebUI from `web/` — registered only if `web/index.html` exists |
| `GET /theme.css` | **no** | generated CSS from `webui.pin` in config.yaml — the style of the pinned prompt; `/reload` re-themes it |

**Auth** (`_authorised`, `server.py:56-62`): the token is `core.token`; empty
means everything is open. Accepts `Authorization: Bearer <tok>` **or**
`?token=<tok>`. Rejection is a 401.

`/` is unauthenticated on purpose — the page holds no secrets and is the thing
that *asks* for the token.

> ⚠️ `/static/` serves the whole `web/` directory, so `test_md.mjs`,
> `browser_test.py` and `shot.py` are reachable under it too.

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
(`core.py:1781`).

### Attaching is a connection, not a handshake

There is no attach message. The name is a query parameter and `hello` — the
first event on every connection — carries the snapshot back. **So reconnecting
is the same operation as connecting.**

The order in `ws_handler` (`server.py:197-204`) is load-bearing:

1. `session.attach(sub)` — so nothing emitted during the greeting is lost
2. send `session.snapshot(last_seq)` — the `hello` event
3. start the writer task — last, so nothing overtakes the greeting

### Client → core

Five verbs, all JSON text frames keyed on `"t"`. Malformed JSON gets a
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

**Commands** (`Session.command`, `core.py:1566`), name lowercased and a leading
`/` stripped:

- read-only, reply to the asker: `info`, `behavior`, `models`, `reload`
- take the turn lock: `clear`, `rollover`, `new`, `model` — these return
  `{"ok": false, "error": "a turn is running…"}` when busy

### Core → client

Every event carries `seq` and `session`, stamped by `Session.emit`
(`core.py:483`).

| `t` | payload | |
|---|---|---|
| `hello` | `session, seq, proto, model, budget, busy, theme, messages, rollover{}, missed[], gap` | the snapshot; `theme` is `webui.theme` (`auto`/`dark`/`light`); `messages` excludes the system message, `gap` is true when the replay buffer could not cover `last_seq` |
| `turn_start` | `turn_id, text, origin` | the core's echo of your own message |
| `steering` | `turn_id, text` | a `/steering` interjection was taken: the round was interrupted (or the correction queued at a round boundary) and the model re-plans with it. It is a real user message, so a fresh attach sees it in `hello.messages` instead |
| `text` | `delta` | |
| `reasoning` | `delta` | **live only, never replayed**; suppressed for `reasoning=0` |
| `reasoning_stat` | `tokens, …` | **live only**; a reconnect would replay hundreds of superseded counts |
| `tool_call` | `id, name, …` | emitted **before** the tool runs, so a slow `exec` is visible |
| `tool_result` | `id, name, size` | |
| `stats` | token and timing figures | the `⚡` line |
| `turn_end` | `turn_id` | |
| `notice` | `level` (`info`/`warn`), `text` | ~30 emit sites |
| `error` | `msg`, `fatal` | |
| `busy` | `reason` | |
| `session_state` | `what` ∈ `reloaded`/`cleared`/`wiped`/`model`/`renamed`/`deleted` | `reloaded` also carries `theme` (the new `webui.theme`); `wiped` and `deleted` also carry `files` + `bytes`: the archives that went with the conversation |
| `rollover_start` / `rollover_ask` / `rollover_done` | | `rollover_ask` is the **only** server→client request |
| `response` | `request_id` + the command's reply | unicast, not broadcast |
| `pong` | — | |

### Replay semantics

`Session.emit` (`core.py:488-500`) buffers into `turn_events` for the current
turn:

- consecutive `text` deltas are **coalesced** (append-only, so lossless — and
  it keeps a 2000-delta turn from replaying token by token)
- `reasoning` and `reasoning_stat` are **excluded entirely**
- everything else is buffered verbatim

A fresh attach is served from history; `missed` covers a reconnect.

### Backpressure

The writer task (`server.py:149-169`) is the **sole owner of the socket for
outbound traffic** — the turn never touches it, so a stalled peer cannot stall
generation.

A subscriber's queue has `maxlen=2000` (`core.py:260`). On overflow the client
is **dropped**: close code `TRY_AGAIN_LATER` (1013) with
`b"too slow; reconnect and replay"`. A client that sees 1013 should reconnect
with its `last_seq`.

### Session re-homing

If a session is deleted while you are attached (`server.py:221-230`), your
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
presentation you can skip. The Discord bot ignores `stats`, `tool_call`,
`tool_result` and all reasoning, and is a complete client.

Things worth copying from the existing three:

- **reconnect with `last_seq`**, and treat close code 1013 as "reconnect now"
- **do not render optimistically** — draw your own message when `turn_start`
  echoes it, so every attached client shows the same transcript
- set `reasoning=0` if you will never draw the thinking; the token count still
  arrives as `reasoning_stat`
- send `origin` if your session is shared by several people
