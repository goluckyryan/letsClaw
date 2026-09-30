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
| `source/core.py` | ~2130 | **the engine.** `Session` (one conversation), `SessionManager` (all of them), config reload, rollover, the thinking pad, commands |
| `source/session.py` | ~680 | **the disk layer.** Live store, archives, the journal, the handoff summariser, the id registry |
| `source/server.py` | ~330 | **the transport.** aiohttp routes, the WebSocket handler, auth, backpressure |
| `source/llm_engine.py` | | **the model.** Two transports (`chat` and `raw`), streaming, tool-call parsing, resume |
| `source/tools.py` | | `exec`, `read_file`, `write_file`, `list_dir`, … and their JSON schemas |
| `source/token_counter.py` | | tiktoken when present, a 4-chars-per-token estimate otherwise |
| `source/paths.py` | | `REPO_ROOT` and `resolve()` — [principle 15](design-principles.md#15-one-definition-of-where-the-repo-is) |
| `source/chunker.py` | | fence-aware message splitting; separate so it tests without `discord.py` |
| `source/chat.py` | | the terminal client |
| `source/discord_client.py` | ~1300 | the Discord client |
| `web/` | | the browser client (`index.html`, `app.js`, `style.css`) — vanilla JS, no build step |

`session.py` is imported as `store` (`core.py:33`) and **never imports
`core`** — the dependency runs one way.

There is no package machinery. `server.py` puts `source/` on the import path,
so modules import each other by plain name.

---

## `core.py`

### `Session` — one conversation

Holds `history`, `engine`, the three behavior layers, per-session policy, and
the set of attached `Subscriber`s.

**Policy is copied per session, never read live off the shared config dict**
(`core.py:423`). A session that disables its own rollover must not disable
everyone else's.

Two odometers survive `/clear` and restarts: `total_output` and
`rollover_count`.

`session_id` — 8 hex, minted once (`core.py:457`), immediately replaced by
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

- **`settled(history)`** (`core.py:278`) — returns a copy with any unfinished
  trailing turn popped: trailing `tool_calls`, `role="tool"`, or `role="user"`,
  back-to-front so a whole interrupted turn unwinds as a unit. Both `persist()`
  and `repair_history()` use it, so memory and disk agree on what "finished"
  means.
- **`truncated_tool_calls(result)`** (`core.py:303`) — calls whose `arguments`
  are non-empty but fail `json.loads`. **These are never run**; `rm -rf
  /tmp/scratch` truncated to `rm -rf /` is the accident this prevents. Empty
  arguments are legitimate.
- **`strip_tool_markup(text)`** (`core.py:323`) — removes `<tool_call>…` from
  prose. Only ever relevant on the raw transport.

---

## The turn, end to end

`Session.run_turn` (`core.py:747`) → `Session._turn` (`core.py:809`).

### `run_turn` — the wrapper

1. **Busy check.** `if self.lock.locked()` → emit `busy` and return. A second
   submit is **refused, never queued**: running it would execute against a
   context its sender never saw.
2. Take the lock, bump `turn_id`, clear the replay buffer, emit `turn_start`.
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

for _ in range(max_tool_rounds):
    result = await call_round(tools=schemas)
    if not result.wants_tool:
        if was_cut_off(result):              # thought hit the cap, said nothing
            result = await _think_pad(...)   # ← the notepad, below
            if result.wants_tool:
                await run_tools(result); continue
        answer = result.text; break
    await run_tools(result)
else:                                        # rounds exhausted
    notice; result = await call_round()      # no tools offered this time
    ...

history.append(assistant)
emit(stats)
_after_turn(used)                            ← rollover evaluated ONLY here
```

Rollover is evaluated only after the tool loop has fully drained, so no
`tool_call`/`tool_result` pair can be split across a window boundary.

### The four closures inside `_turn`

- **`call_round(...)`** (`core.py:907`) — the single funnel for every model
  call. Measures TTFT, dispatches to `chat_with_tools` or `resume_round`, then
  `tally`, then journals **every path by construction**. This is the only place
  a pad's reasoning is ever recorded, since the pad never enters history.
- **`tally(result, history_shaped=True)`** (`core.py:873`) — folds one round's
  usage in. **`history_shaped=False` for resumed rounds**: a resume's prompt
  carries the thinking pad, which never reaches history, so letting it set
  `last_prompt` would measure the window tens of thousands of tokens fuller and
  trip a spurious rollover.
- **`run_tools(result)`** (`core.py:974`) — appends the assistant message
  (substituting `"{}"` for truncated calls so history stays server-valid),
  **reserves every result slot up front** with `"error: cancelled"` so a turn
  torn off mid-loop still leaves a well-formed history, then per call: emit
  `tool_call` *before* running, jot, run, **full output to the journal and
  clipped output to the model**, emit `tool_result`.
- **`push_reasoning_stat(...)`** (`core.py:847`) — counts only the new slice,
  **cutting at the last space**, because a token carries its leading space
  (`" alpha"` is one token; `"alpha"` + `" "` is two) and cutting anywhere else
  inflates the running sum.

### Token accounting

`out_exact = rounds > 0 and out_reported == rounds` (`core.py:1094`) — the
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

`core.py:1154-1427`. The most intricate subsystem; read the section in
[principle 6](design-principles.md#6-separate-the-notepad-from-the-answer-sheet)
first.

**Premise:** reasoning and answer come out of one budget, so a round can think
hard and say nothing. Rather than bin that work, the reasoning becomes a *pad*
handed back unclosed, and the model continues mid-sentence.

> **The pad lives only in `_think_pad`'s local variable. It never enters
> `history`** (`core.py:1199`), so it costs nothing after the turn and cannot
> move the rollover point. Keep it that way.

| function | |
|---|---|
| `_pad_headroom()` | `budget - estimate() - answer_cap - 2000` |
| `_headroom_check()` | pre-turn rollover when headroom < `MIN_THINK_HEADROOM` (4000) |
| `_think_pad(...)` | the resume loop |
| `_pad_tail(pad)` | the last ~2000 tokens **verbatim** — never summarised, because the model must resume *mid-sentence* |
| `_compact_pad(...)` | replaces the pad with `<checkpoint note> + tail`. Off by default; compaction is lossy |
| `_forced_conclusion(...)` | the fallback when `resume_mode` resolves to `off` |

**The resume loop is deliberately unbounded** (`core.py:1218`) — no round
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
directive in `history`** (`core.py:1419`), so a synthetic user message
permanently joins the conversation. Only on the non-resumable path.

---

## The four-layer system prompt

`Session.fresh_history()` (`core.py:539`):

```python
content = self.base_behavior                                    # models/base.md
if self.behavior:         content += "\n=== BEHAVIOR ===\n" + self.behavior
if self.session_behavior: content += SESSION_MARKER + self.session_behavior
if carryover:             content += CARRYOVER_MARKER + carryover
```

**The ordering is load-bearing.** `carryover()` (`core.py:573`) recovers layer 4
by partitioning on `CARRYOVER_MARKER` and taking everything *after* it. Anything
appended below would be read back as part of the handoff and re-appended on the
next rebuild — growing the prompt every turn.

Corollary: a session-behavior file containing the literal string
`=== CONTINUED SESSION ===` will silently corrupt this.

**Four places rebuild `history[0]`, and each must round-trip `carryover()`:**
`reload_session_behavior` (569), `apply_config` (634), `use_model` (1686),
`roll_over` (1548). `restore()` avoids the problem differently — by assigning
history *after* `use_model`, which would otherwise destroy the carryover.

Directives are computed from the **unstripped** base, so `strip_directives`
removes them from the prompt without removing the core's ability to send them
at the right moment.

Full story: [decisions/2026-09-30-session-behavior-layer.md](decisions/2026-09-30-session-behavior-layer.md).

---

## Rollover

`_after_turn` → `_maybe_roll` → `roll_over` (`core.py:1429-1562`).

`trip = budget * rollover_pct / 100`; `0` disables. Modes: `auto` rolls, `ask`
prompts (and will not re-ask until usage climbs another 5% of budget), `off`
warns once.

**The loop-breaker** (`core.py:1438`): after a successful roll, if the *fresh*
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

`SessionManager.reload` (`core.py:1855`) is the most consequential function in
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

`apply_config` (`core.py:602`) is deliberately **synchronous and
assignment-only**: all I/O happens in `reload` beforehand, because the deferred
path runs inside `run_turn`'s `finally` while a `CancelledError` is
propagating, and an `await` there can be interrupted again and leave the
session half-updated.

**Restart-only settings:** `core.bind`, `core.port`, `logging.file`.

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
and would otherwise stall every other session's stream.

`state/sessions/index.jsonl` holds three record types — `archive`, `journal`,
`rename`. **The index exists because a filename is not an identity**:
transcripts written before a rename keep the old name forever, and the id is
what joins them.

`purge_archives` (only `/new` calls it) **refuses an empty or all-zero id**,
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

See [protocol.md](protocol.md) for the full reference. The two structural
points:

- **The writer task is the sole owner of the socket for outbound traffic**, so
  a stalled peer cannot stall generation. Overflow drops the client with close
  code 1013 and it reconnects with `last_seq`.
- **`submit` runs as `create_task` and is never awaited inline**, so the reader
  stays live for `stop` and `rollover_reply` during a long turn.

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
7. **`_last_used` is only written on a successful answer** (`core.py:1110`) but
   read unconditionally by `_after_turn`. A turn that produced no answer
   evaluates rollover against the *previous* turn's figure.
8. **`persist()` writes `settled(history)` but does not modify the live
   history** — disk and memory intentionally disagree after an interrupted turn
   until `repair_history()` runs. Don't "fix" it.
9. **`/reload` is intentionally outside the turn lock** (`core.py:1607`).
   Taking it deadlocks, because `reload` defers busy sessions and would defer
   the one that asked.
10. **An unknown command issued during a turn reports "a turn is running"**,
    not "unknown command" — the lock check precedes the fall-through.
11. **The `for/else` at `core.py:1042`/`1059`** fires only when the tool-round
    budget is exhausted without a `break`. Easy to misread as an error branch.
12. **`extract_directive` returns `CONCLUDE_FALLBACK` for an empty *checkpoint*
    paragraph** (`core.py:397`) — the one place the two directives are
    conflated. A latent copy-paste.
13. **Session names are unvalidated at creation**; only `rename` validates.
    `session_behavior_path` is the double guard. If you add another place where
    a session name becomes a read path, replicate it.
