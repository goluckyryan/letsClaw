# Architecture

How the code is laid out, what each module owns, and what actually happens
during one turn. Written for someone who has to change this code.

The [README](../README.md) covers what letsClaw does from the outside.
[design-principles.md](design-principles.md) covers why it is shaped this way.
This file is the map between them.

---

## The shape in one picture

```
   terminal (chat.py)  ─┐
   browser (web/app.js) ├─ WebSocket ─→  server.py  ──→  core.py  ──→  llm_engine.py ──→ model
   Discord bot          ─┘               (transport)     (state)       (transports)
   (discord_client.py)                                      │
                                                            ├──→ tools.py    (exec, files, …)
                                                            └──→ session.py  (disk: live, archive, journal)
```

One long-lived core process. Clients are separate processes holding no
conversation state — see
[principle 1](design-principles.md#1-the-core-owns-the-conversation-clients-are-windows-onto-it).
`./serve.sh` starts the core and the bot together.

## Files

| file | lines | owns |
|---|---|---|
| `source/core.py` | ~3450 | **the engine.** `Session` (one conversation), `SessionManager` (all of them), config reload, rollover, the thinking pad, commands |
| `source/session.py` | ~790 | **the disk layer.** Live store, archives, the journal, the handoff summariser, the id registry |
| `source/server.py` | ~1090 | **the transport.** aiohttp routes, the WebSocket handler, the Host and Origin guards, auth, backpressure |
| `source/llm_engine.py` | ~980 | **the model.** Two transports (`chat` and `raw`), streaming, tool-call parsing, resume |
| `source/config.py` | ~520 | **config.yaml → engines.** Loading, the webui presentation keys, number coercion, `build_all_engines` (which doubles as the validator), and the reload diff. Touches no `Session`, which is why it is not in `core` |
| `source/behavior.py` | ~300 | **the behavior layers.** `BehaviorStore`, the read-through cache behind a lock, and the two directives extracted out of `base.md` |
| `source/config_edit.py` | ~290 | rewriting the `webui:` block of `config.yaml` **as text**, so the file's comments survive a save |
| `source/ssh_forward.py` | ~570 | **model servers over ssh.** One supervised `ssh -N -L` child per model entry that names an `ssh_forward`, on a port the core picks itself |
| `source/tools.py` | ~800 | `exec`, `read_file`, `write_file`, `list_dir`, `show_image`, the search and edit tools, … and their JSON schemas |
| `source/logging_setup.py` | ~110 | one rotating log file **per writer** (`letclaw-core.log`, `letclaw-discord.log`), because two processes sharing one file cannot rotate it safely |
| `source/token_counter.py` | ~230 | counts with tiktoken's `cl100k_base`, cached under `state/tiktoken/` so it is fetched once ever rather than once per reboot; degrades loudly to 4-chars-per-token if the vocab cannot be loaded at all |
| `source/paths.py` | ~70 | `REPO_ROOT` and `resolve()` — [principle 15](design-principles.md#15-one-definition-of-where-the-repo-is) |
| `source/chunker.py` | ~310 | fence-aware message splitting; separate so it tests without `discord.py` |
| `source/chat.py` | ~640 | the terminal client |
| `source/ui.py` | ~90 | the terminal client's spinner and TTFT line |
| `source/discord_client.py` | ~2310 | the Discord client |
| `source/discord_probe.py` | ~50 | **"should the bot be started?", answered once.** `serve.sh` and `install.sh` both run it and read the verdict off the exit code; they used to each carry their own copy of the question and the copies drifted |
| `web/` | ~2220 (`app.js`) | the browser client (`index.html`, `app.js`, `style.css`) — vanilla JS, no build step |

`session.py` is imported as `store` and **never imports
`core`** — the dependency runs one way. Neither do `config.py`, `behavior.py`
or `paths.py`, which is what lets a test import any of them on their own.

There is no package machinery. `server.py` puts `source/` on the import path,
so modules import each other by plain name.

---

## `core.py`

### `Session` — one conversation

Holds `history`, `engine`, the three behavior layers, per-session policy, and
the set of attached `Subscriber`s.

**Policy is copied per session, never read live off the shared config dict**
(`Session.__init__()`). A session that disables its own rollover must not disable
everyone else's.

Two odometers survive `/clear` and restarts: `total_output` and
`rollover_count`.

`session_id` — 8 hex, minted once in `Session.__init__()`, immediately replaced by
`SessionManager.get` with a registry-checked one so a collision cannot happen
silently. See [principle 5](design-principles.md#5-identity-is-the-session-id-the-name-is-a-label).

### `Subscriber` — one attached client's outbound lane

A bounded `deque` (`maxlen=2000`) plus a wake event. `push()` drops `reasoning`
events for clients that asked not to get them — **but never `reasoning_stat`**,
because a token counter is all a non-reasoning client has to show for a
two-minute think.

**`emit()` is synchronous and non-blocking by contract.** The engine calls the
streaming callback from inside the SSE read loop; awaiting a socket there would
stall *every session's* model read. Adding an `await` to `emit`, `push`, or any
`on_text`/`on_reasoning` path is the single easiest way to wreck this system.

### `SessionManager` — all of them

Owns the sessions dict, the engine cache, the behavior-file cache, tools and
their schemas (built **once** per core, not per session), and the two directive
templates.

Notable methods: `get` (create on demand), `restore` (eager at startup),
`reload`, `rename`, `delete`, `session_behavior_path`, `close`.

### History hygiene

Three free functions worth knowing before you touch the turn loop:

- **`settled(history)`** — returns a copy with any unfinished
  trailing turn popped: trailing `tool_calls`, `role="tool"`, or `role="user"`,
  back-to-front so a whole interrupted turn unwinds as a unit. Both `persist()`
  and `repair_history()` use it, so memory and disk agree on what "finished"
  means.
- **`truncated_tool_calls(result)`** — calls whose `arguments`
  are non-empty but fail `json.loads`. **These are never run**; `rm -rf
  /tmp/scratch` truncated to `rm -rf /` is the accident this prevents. Empty
  arguments are legitimate.
- **`stored_tool_args(tc)`** — what one call's `arguments` looks like in
  history: always a JSON object. The server re-validates every assistant
  message on every later request, so a stored `""` or `123` is a 400 on the
  *next* turn and on every turn after it, until the window rolls over —
  `truncated_tool_calls` decides what may run, which is a different question
  from what may be stored.
- **`strip_tool_markup(text)`** — removes `<tool_call>…` from
  prose. Only ever relevant on the raw transport.

---

## The turn, end to end

`Session.run_turn()` → `Session._turn()`.

### `run_turn` — the wrapper

1. **Busy check.** `if self.lock.locked()` → emit `busy` and return. A second
   submit is **refused, never queued**: running it would execute against a
   context its sender never saw.
2. Take the lock, bump `turn_id`, reset `round_id` to 0, clear the replay
   buffer, emit `turn_start`.
3. `await asyncio.wait_for(self._turn(text), engine.turn_timeout)`.
4. Timeout / cancellation / exception → `repair_history()` and a notice.
   `CancelledError` is **re-raised**, after the `finally`.
5. `finally`, in this order: `persist()`, then `emit(turn_end)` in that try's
   own `finally` — so `turn_end` means *on disk*, and a failing save cannot
   swallow the event. Then apply `_pending_config` if a reload landed mid-turn.

### `_turn` — the work

```
_headroom_check()           ← BEFORE the user message lands, so a roll starts
                              the fresh window with this turn's question in it
history.append(user)
jot("turn N user", text)

# room, not a round count — nothing mid-turn can roll over, so what the loop
# runs out of is window
while True:
    if _pad_headroom() < MIN_TOOL_ROUND_ROOM:
        notice                                   # "no room left for another tool round"
        result = await call_round()              # no schemas: it must answer in words
        if was_cut_off(result):                  # thought hit the cap, said nothing
            result = await _think_pad(..., allow_tools=False)
        answer = result.text; break
    drain_steering()                             # a /steering queued during the last tool
    try:
        result = await call_round(tools=schemas)
    except RoundInterrupted:                     # /steering landed mid-round
        record_partial(); drain_steering(); continue
    if not result.wants_tool:
        if was_cut_off(result):
            result = await _think_pad(...)       # ← the notepad, below
            if result.wants_tool:
                await run_tools(result); continue
        if self._steering:                       # answer finished as a correction landed
            history.append(assistant of text); drain_steering(); continue
        answer = result.text; break
    await run_tools(result)

history.append(assistant)
emit(stats)
_digest_turn(user_msg, reasoning, blocks_before)   ← collapses this turn's
                                               working set, BEFORE rollover
                                               emit(digest_done) — note this
                                               precedes turn_end
_after_turn()                                ← rollover evaluated ONLY here,
                                               reading used_now()
```

**That order is load-bearing too.** Digesting after `_after_turn` would let
rollover throw the un-collapsed window away first, and the digest would be
doing nothing.

Rollover is evaluated only after the tool loop has fully drained, so no
`tool_call`/`tool_result` pair can be split across a window boundary.

### The four closures inside `_turn`

- **`call_round(...)`** — the single funnel for every model
  call. Measures TTFT, dispatches to `chat_with_tools` or `resume_round`, then
  `tally`, then journals **every path by construction**. This is the only place
  a pad's reasoning is ever recorded, since the pad never enters history.
- **`tally(result, history_shaped=True)`** — folds one round's
  usage in. **`history_shaped=False` for resumed rounds**: a resume's prompt
  carries the thinking pad, which never reaches history, so letting it set
  `last_prompt` would measure the window tens of thousands of tokens fuller and
  trip a spurious rollover. When `history_shaped=True` it does the converse and
  **publishes `_last_used` immediately**, not just at turn end: the prompt just
  sent *was* the current history, so it is a live measurement, and the pad wall
  reads it on every round. That is what lets a tool result landing mid-turn move
  the wall. See trap 7 for the obligation this creates.
- **`run_tools(result)`** — appends the assistant message
  (substituting `"{}"` for truncated calls so history stays server-valid),
  **reserves every result slot up front** with `"error: cancelled"` so a turn
  torn off mid-loop still leaves a well-formed history, then per call: emit
  `tool_call` *before* running, jot, run, **full output to the journal and
  clipped output to the model**, emit `tool_result`. A run of consecutive
  read-only calls (`PARALLEL_SAFE` in tools.py — `read_file`, `list_dir` and
  the three search tools) runs together through `asyncio.gather`, so three
  two-second reads take the two of the slowest; a write and an exec run alone,
  in their issued position, so a read after the write of the same path keeps
  the order the model meant.
  Results and `tool_result` events land in completion order; the slots, and
  the history, stay in issue order.
- **`push_reasoning_stat(...)`** — counts only the new slice,
  **cutting at the last space**, because a token carries its leading space
  (`" alpha"` is one token; `"alpha"` + `" "` is two) and cutting anywhere else
  inflates the running sum.

### Token accounting

`out_exact = rounds > 0 and out_reported == rounds` (in `tally()`) — the
server's sum is trusted **only if every round reported one**. A partial sum
looks authoritative while understating.

`used = last_prompt + count_text(answer) + 4`, deliberately **not** prompt +
completion: completion includes reasoning that is thrown away, which would
inflate the figure and can even make it *fall* as the conversation grows.

`reason_tok` is monotonic by construction (`max(...)`), and the final
`stats.reasoning_tokens` quotes the same variable the live counter used — two
numbers for one quantity reads as a bug.

---

## The thinking pad

`_headroom_check()` through `_think_pad()` and `_compact_pad()`. The most
intricate subsystem; read the section in
[principle 6](design-principles.md#6-separate-the-notepad-from-the-answer-sheet)
first.

**Premise:** reasoning and answer come out of one budget, so a round can think
hard and say nothing. Rather than bin that work, the reasoning becomes a *pad*
handed back unclosed, and the model continues mid-sentence.

> **The pad lives only in `_think_pad`'s local variable. It never enters
> `history`**, so it costs nothing after the turn and cannot
> move the rollover point. Keep it that way.

| function | |
|---|---|
| `_answer_reserve()` | `clamp(10% of free, 4096, max_output_tokens)`, then clamped to `free - 2000`, floored at 256 |
| `_pad_headroom()` | `budget - used_now() - _answer_reserve() - PAD_CONTEXT_MARGIN (2000)`. `used_now()`, not `estimate()` — it prefers the server's measurement |
| `_headroom_check()` | pre-turn rollover when headroom < `MIN_THINK_HEADROOM` (4000). Reads the *same* `_pad_headroom()` as the wall, and must keep doing so |
| `_think_pad(...)` | the resume loop |
| `_pad_tail(pad)` | the last ~2000 tokens **verbatim** — never summarised, because the model must resume *mid-sentence* |
| `_compact_pad(...)` | replaces the pad with `<checkpoint note> + tail`. Off by default; compaction is lossy |
| `_forced_conclusion(...)` | the fallback when `resume_mode` resolves to `off` |

**The resume loop is deliberately unbounded** (`_think_pad()`) — no round
budget, because a count is not a limit the machine actually has. It exits on:
the time deadline, the context wall, a finished answer, a non-`length` stop, or
**the no-progress guard** — cut off at the cap having written *nothing* means
the pad cannot grow, the wall will never move, and every further round is
identical. That guard is the sole replacement for the round counter.

**Tools during the pad:** `tools_ok = allow_tools and transport == "chat"`,
because only the chat transport parses tool calls. A resume that produces an
untruncated tool call returns immediately and the main loop runs it. In the
**answer phase** tools are offered once and only once — a resume must not call
one, because there is no way to splice a tool result into an unclosed thinking
block without forfeiting the pad.

`_forced_conclusion` has one wart worth knowing: **on success it leaves the
directive in `history`**, so a synthetic user message
permanently joins the conversation. Only on the non-resumable path.

---

## The four-layer system prompt

`Session.fresh_history()`:

```python
content = self.base_behavior                                    # models/base.md
if self.behavior:         content += "\n=== BEHAVIOR ===\n" + self.behavior
if self.session_behavior: content += SESSION_MARKER + self.session_behavior
if carryover:             content += CARRYOVER_MARKER + carryover
```

**The ordering is load-bearing.** `carryover()` recovers layer 4
by partitioning on `CARRYOVER_MARKER` and taking everything *after* it. Anything
appended below would be read back as part of the handoff and re-appended on the
next rebuild — growing the prompt every turn.

Corollary: a session-behavior file containing the literal string
`=== CONTINUED SESSION ===` will silently corrupt this.

**Three places rebuild `history[0]`, and each must round-trip `carryover()`:**
`reload_session_behavior` (455), `roll_over` (1752), `use_model` (1942).
`restore()` avoids the problem differently — by assigning history *after*
`use_model`, which would otherwise destroy the carryover.

`apply_config` used to be a fourth and no longer is: it leaves `history`
alone on purpose, because the prefix it would rebuild is the one the server
has already tokenised, and rebuilding it mid-conversation costs every live
session its KV cache for a behavior edit it did not ask for. An edited file
is picked up instead by `refresh_behavior()` at the next rollover, `/clear`
or `/new` — the paths that were going to replace the prefix anyway.

Directives are computed from the **unstripped** base, which is why stripping
the `# Directives` group out of every prompt does not remove the core's ability
to send them at the right moment.

The rules this layer follows, each with its reasoning, are
[principle 2](design-principles.md#2-behavior-is-markdown-and-some-of-that-markdown-is-machinery)
(behavior is Markdown, and the two directive sections are machinery),
[principle 4](design-principles.md#4-key-on-the-session-name-not-on-the-clients-identity)
(key on the session name, not the client's identity) and
[principle 5](design-principles.md#5-identity-is-the-session-id-the-name-is-a-label)
(id is the identity, the name is a label).

---

## Rollover

`_after_turn()` → `_maybe_roll()` → `roll_over()`.

`trip = budget * rollover_pct / 100`; `0` disables. Modes: `auto` rolls, `ask`
prompts (and will not re-ask until usage climbs another 5% of budget), `off`
warns once.

**The loop-breaker** (in `_after_turn()`): after a successful roll, if the *fresh*
window still exceeds the trip, `rollover_pct` is set to `0` with a warning.
That setting is runtime-mutable and persisted, which is why `apply_config` uses
a divergence test rather than blindly reapplying config values, and why
`_headroom_check` refuses to act when `rollover_pct` is zero.

`roll_over` is **ordered for failure-safety**:

1. `save_transcript` **first** — a failed summariser or a full disk costs the
   summary, never the conversation.
2. Close the journal, *before* building the carryover, because the carryover
   advertises its path.
3. `summary_max = min(1500, budget//20, budget - used - 200)`; below 256 the
   summary is skipped with a warning rather than attempted badly.
4. `build_handoff` — exceptions warn and continue with the last exchange only.
5. `append_handoff` into long-term memory.
6. New `history[0]` from `format_carryover(...)`, plus `last_exchange` **only
   if it fits under the trip**.

`roll_over` does not call `persist()`; its caller does.

---

## Config reload

`SessionManager.reload()` is the most consequential function in
the file.

1. **Validate before committing.** `load_config` + `build_all_engines` in a
   `try`; on failure return `{"ok": False, "error": …}` and change **nothing**.
   Building the engines *is* the validation — same code path a real turn takes.
2. Diff; identical → early return.
3. **`self.config.clear(); self.config.update(fresh)`** — mutated **in place,
   never rebound.** The manager, the aiohttp app and every `Session` hold this
   same dict; in-place mutation is what makes `core.token`, the state
   directories and `GET /models` pick up new values with no plumbing.
   **Rebinding it silently desynchronises all of them.**
4. Swap engines, clear the behavior cache, recompute directives, rebuild tools.
5. Per session, **iterating a list copy** (a client attaching during the
   `await` would otherwise mutate the dict mid-iteration): build a blob, and
   either apply it or stash it in `_pending_config` if the session is busy.
6. **Close only orphaned old engines.** A session mid-stream must keep its
   engine — closing the client under it aborts the read.

`apply_config()` is deliberately **synchronous and
assignment-only**: all I/O happens in `reload` beforehand, because the deferred
path runs inside `run_turn`'s `finally` while a `CancelledError` is
propagating, and an `await` there can be interrupted again and leave the
session half-updated.

**Restart-only settings:** `core.bind`, `core.port`, and the three keys that
`logging_setup.configure()` reads once when it installs the handlers —
`logging.file`, `logging.max_bytes`, `logging.backups`. `logging.level` is
not among them: `_reload` sets it on the root logger. Nor is
`core.allowed_hosts`, which `_host_ok` reads off the live config dict on
every request, so the in-place mutation in step 3 is what delivers it.

**A session pinned to a model that vanished from the config keeps its engine**
rather than being silently moved — but that engine is no longer in `_engines`,
so `close()` will not close it at shutdown. Known leak.

---

## `session.py` — the disk layer

Two stores, deliberately separate:

| | path | |
|---|---|---|
| **live** | `state/live/` | rewritten every turn, read back at startup |
| **archive** | `state/sessions/` | write-once, never read back into a conversation |

### Live

`live_path` is `<safe-name>-<sha1(name)[:8]>.json`. **The digest is the key;
the readable half is cosmetic** — the slug is lossy (`my session`, `my/session`
and `my+session` all flatten identically) and truncates at 40 chars. The real
name lives inside the file.

`save_live` is **atomic by rename**: write `.tmp`, `flush`, `os.fsync`,
`os.replace`. Compact JSON, not indented — it runs after every turn.

`load_live` never raises. It skips unparseable files, malformed shapes, and
**wrong `LIVE_SCHEMA`** — each with a log line, and **never deletes them**.

> ⚠️ **Bumping `LIVE_SCHEMA` silently discards every user's live
> conversation.** This is why `session_id` was added additively.
> [Principle 12](design-principles.md#12-bookkeeping-must-never-cost-a-turn).

### The id registry

`logs/session_ID.log`, **rewritten in full on every change** rather than
appended, because an id must be able to *leave* when its session is deleted.
Tab-separated, not whitespace-split, because session names contain spaces.
`register_id` is an **upsert**, so a rename is reflected and `created` survives.

### Archives and the index

`save_transcript` → `<sid>_<safe-name>_<stamp>.json`. Encoding **and** writing
both go through `to_thread`, because a 262144-token history can be megabytes
and would otherwise stall every other session's stream. The write is
`paths.atomic_write`, like every other durable write here — not for the rename
(the name is fresh, there is nothing to replace) but for the fsync. `roll_over`
drops the live history once this returns, so a torn archive is not a damaged
copy of the conversation, it is the conversation.

`state/sessions/index.jsonl` holds four record types — `archive`, `journal`,
`rename`, `delete`. **The index exists because a filename is not an identity**:
transcripts written before a rename keep the old name forever, and the id is
what joins them. The index is append-only, so `delete` cannot retract the lines
its session wrote; the `delete` record is the tombstone that explains why those
files are no longer on disk.

`purge_archives` (called by `/new` and by deleting a session) **refuses an empty
or all-zero id**,
because `"0"*8` is the shared prefix used by id-less writers and matching on it
could take out someone else's archive. There is no undo.

### The journal

An append-only Markdown record of one session *segment*. It exists because the
JSON archive is structurally missing two irrecoverable things: **the model's
reasoning** and **untruncated tool output**.

Markdown and not JSON because `read_file` has no offset and truncates at 8000
chars — a 400 KB JSON archive is unreachable to the model, while a
one-header-per-line file is reachable at any size via `grep`/`sed`. The header
convention (`## turn 12 round 3 tool_call exec call_a1b2  [16:04:11]`) makes
`grep -n '^## turn'` a table of contents.

That `turn`/`round` pair is the same one the clients draw in their left gutter
(`#12.3`), because `emit()` stamps every event with it and every history record
carries it — so a block on screen is greppable in the journal by the number
printed beside it. Both keys are display-only and `api_messages()` strips them,
alongside `at`, before anything is sent to the model.

`write()` never raises, and **a single `OSError` closes the segment for good**
rather than warning once per block. Unlike `save_transcript`, `Journal._open`
**does disambiguate** with a `-2` suffix: a rollover closes one segment and
opens the next in the same second, and colliding there would append the new
window to the closed segment.

### The handoff

`build_handoff` runs the summariser on **its own message list** — the real
history is never touched. Four mandated sections: OBJECTIVE / ESTABLISHED /
**RULED OUT** / OPEN. It sends `enable_thinking: False` as a template kwarg and
**retries without the hint** if the server rejects it.

`format_carryover` emits three literal `grep` commands for the journal and
explicitly tells the model **not** to `read_file` the record — pointing it at a
file it can only ever see 2% of is worse than saying nothing.

`last_exchange` skips anything with `tool_calls` and every `role="tool"`
message: half of a tool-call pair in a fresh history is a 400 from the server.

---

## `server.py` and the protocol

See [protocol.md](protocol.md) for the full reference. The three structural
points:

- **The writer task is the sole owner of the socket for outbound traffic**, so
  a stalled peer cannot stall generation. Overflow drops the client with close
  code 1013 and it reconnects with `last_seq`.
- **`submit` runs as `create_task` and is never awaited inline**, so the reader
  stays live for `stop` and `rollover_reply` during a long turn.
- **The browser defence is two headers, not one, and the Host half is a
  middleware.** `_same_origin` only establishes that the browser agrees with
  itself: Host is client-supplied, so a page on a domain re-pointed at
  127.0.0.1 sends a matching Origin and Host honestly and passes. `_host_guard`
  is what pins the other half, refusing any request whose Host is not a name
  this machine answers to — loopback, the bind, the hostname and FQDN and
  what they resolve to, computed once at `build_app` because `getaddrinfo`
  must not run per request, plus whatever `core.allowed_hosts` adds for a
  reverse proxy. It is a middleware rather than part of `_guard` because
  rebinding pays off on the *read* routes too: `/sessions`, `/config` and
  `/sessions/<name>/history` are whole conversations, and `/files` is the
  directory the agent's pictures land in. `/health` is the one
  exemption, since a container probe calls it under whatever name the
  orchestrator invented.

---

## Traps

Collected from reading the code. Each of these has a comment in the source
explaining it; this is the index.

1. **`emit()` must never await.** See above. Everything else on this list is
   recoverable; this one wrecks the system.
2. **The thinking pad must never enter `history`.**
3. **Layer 4 must stay last in the system prompt**, and every rebuild of
   `history[0]` must round-trip `carryover()`.
4. **`self.config` is mutated in place, never rebound.**
5. **`LIVE_SCHEMA` bumps discard users' live conversations.**
6. **`/new` is irreversible**; `/clear` is not. Do not make them more alike.
7. **`_last_used` is published mid-turn** by `tally` on every round where
   `history_shaped=True`, so the pad wall tracks the window as tool results
   land. The corollary: **every path that reshapes `history` must clear it** —
   `repair_history`, `_digest_turn`, the `max_history` trim, `roll_over`,
   `use_model`, `/clear`. Leave it set and the next headroom check reads a
   window that no longer exists, then rolls over to relieve pressure that call
   had already relieved.
8. **`persist()` writes `settled(history)` but does not modify the live
   history** — disk and memory intentionally disagree after an interrupted turn
   until `repair_history()` runs. Don't "fix" it.
9. **`/reload` is intentionally outside the turn lock** (`Session.command()`).
   Taking it deadlocks, because `reload` defers busy sessions and would defer
   the one that asked.
10. **An unknown command issued during a turn reports "a turn is running"**,
    not "unknown command" — the lock check precedes the fall-through.
11. **Session names are unvalidated at creation**; only `rename` validates.
    `session_behavior_path` is the double guard. If you add another place where
    a session name becomes a read path, replicate it.
