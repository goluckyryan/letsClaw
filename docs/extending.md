# Extending letsClaw

Recipes. Each one names the files you touch and the trap that goes with it.
Read [design-principles.md](design-principles.md) first — most of these
recipes are short because a principle already decided the hard part.

---

## Running the tests

```bash
.venv/bin/python tests/test_chunk.py            # and each of the others
```

**They are standalone scripts, not a pytest suite** — pytest is not installed.
Each file runs its own cases and reports, and
`.venv/bin/python tests/run_all.py` runs the lot. None need network or a bot
token except `test_discord_live.py`, which starts a real core on a temp config
(still no Discord).

A selection, not the whole directory — there are around 45 files:

| file | covers |
|---|---|
| `run_all.py` | the runner: every file above, with the environment-gated ones skipped and named |
| `test_answer_reserve.py` | the answer reserve, swept across every configured model — the gate's verdict must match the wall's at every point |
| `test_chunk.py` | the fence-aware chunker, incl. CJK and the real 2000 boundary |
| `test_clear_new.py` | `/clear` vs `/new` — what each destroys |
| `test_discord.py` | gate, routing, rendering, all on fakes |
| `test_discord_live.py` | a real core over the real protocol |
| `test_doc_pointers.py` | **these docs against the source** — every function, class and constant named in them must still exist, and raw file:line pointers are budgeted at zero |
| `test_inline_think.py` | `InlineThinkSplitter`, at every chunk boundary |
| `test_journal.py` | the journal |
| `test_server_caps.py` | the server probe — reasoning channel, window, usage |
| `test_session_behavior.py` | the four-layer prompt, path safety, rename |
| `test_session_index.py` | `index.jsonl` and the id registry |
| `test_think_pad.py` | the notepad loop |
| `test_tool_markup.py` | raw-transport tool-call recovery |
| `test_turn_digest.py` | the per-turn digest, and every path that refuses it |

> ⚠️ **`tests/` is in `.gitignore`.** A fresh clone has no test suite. If you
> are picking this up from a clone and `tests/` is missing, that is why — ask
> for it rather than assuming there was never one.

There is no CI. Run the affected files by hand before committing — and
`test_doc_pointers.py` after touching anything in `docs/`, since it is the
only test that fails because of a *rename* rather than a behaviour change.

---

## Add a tool

**One file: `source/tools.py`.**

Write an `async def` handler, wrap it in `Tool(name, description, parameters,
handler, max_output)` inside `build_tools`, add it to the dict at the bottom,
and add its name to the default `allow` list if it should be on by default.
The final line is `return [tools[k] for k in tools if k in allow]`.

`Tool.run_full` returns `(full, clipped)` — the core journals the full text and
gives the model the clipped one. Your handler just returns a string; argument
parsing, `TypeError` from a bad signature, and any other exception are all
caught and turned into a string the model can read.

**The trap:** if your handler blocks — disk, a subprocess, anything
CPU-bound — it must go **off the event loop**. The core is shared; one slow
read stalls every session's token stream
([principle 14](design-principles.md#14-the-core-is-shared-so-nothing-may-block-it)).
The four file tools use `to_thread` for exactly this. If you spawn a process,
copy `exec`'s `start_new_session=True` and `_kill_group` handling so a
cancellation does not leave it detached.

Tool schemas are counted into the prompt budget **once per core**
(`SessionManager.__init__`), not per session. A verbose description is paid for
by every session, every turn. Measure it rather than guessing: send the same
prompt to the server with and without the new schema and diff `prompt_tokens`.
`show_image`, one sentence of description and two parameters, costs **121** —
see [tokens.md](tokens.md).

## Add a command

**`Session.command()`**, plus a renderer in each client that
should show it.

Decide first whether it is **read-only**. Read-only commands go *before* the
`if self.lock.locked()` check and their reply is unicast to the asking client
only ([principle 16](design-principles.md#16-clients-render-nothing-optimistically)).
Everything that mutates goes after it, and returns
`{"ok": false, "error": "a turn is running…"}` when busy.

`/reload` and `/restart` sit above that check even though they change
everything — they are manager-level, not session-level, and taking the turn lock
there deadlocks because `reload` defers busy sessions and would defer the one
that asked. See [protocol.md](protocol.md) for the three groups.

**The trap:** a command is not one listing but **eight**, and they have already
drifted. Nothing tells you if you miss one:

| | |
|---|---|
| `source/core.py` | the branch in `Session.command` — the only one that does anything |
| `source/chat.py` | `COMMANDS` (the help line and tab completion both read it) |
| `web/app.js` | `COMMANDS`, and a toolbar handler if it gets a button |
| `web/index.html` | that button |
| `source/discord_client.py` | `COMMAND_WORDS` — **miss this one and `!cmd` is sent to the model as chat** — `HELP`, and `_register_commands` for the native slash picker |
| `README.md` | the terminal command table, the Mermaid `Cmds` node, the Discord prose list |

`tests/test_discord.py` pins the slash tree, so Discord's three are the only
ones a test will catch for you. See
[clients.md](clients.md#what-is-shared-and-what-is-not).

If the command mutates session state that other attached clients should see,
emit a `session_state` event as well as returning the reply.

## Add an HTTP route

**`build_app()` in `source/server.py`**, plus a handler beside the others.

Pick the guard before you write the handler, because it is the decision the
reviewer will check first:

| | |
|---|---|
| read-only | `_read_guard(request)` — auth only |
| anything that writes | `_guard(request)` — auth **and** same-origin |
| open on purpose | neither, and say in the docstring why it holds no secrets |

One call either way, so which gate a route takes is a grep and not a pattern
match. Call the named guard rather than inlining `if not _authorised(request)`:
six routes did that before, and an inlined check is one a later edit can
change in one place and not the other five.

`_authorised` is the project's single answer to "who is asking". Do not write a
header-only or query-only variant for one route: a second definition is how two
of them end up disagreeing about the same request. The query-token form exists
because an `<img>` cannot send a header; a browser-driven write is gated by
`_same_origin` instead.

If two routes answer the same question about a path, extract it. `/files` has
three — read, list, delete — and they share `_files_target()` for exactly this
reason: a listing that offers a file the read route refuses is a tile that is
broken forever, and a delete that resolves a path the read route would not is a
hole. One function, three callers, no way for them to drift apart.

**The trap, the same shape as the command one above:** a route is not one
listing either. Nothing tells you if you miss one:

| | |
|---|---|
| `source/server.py` | `build_app`'s `add_routes` — register a path's methods **adjacently**, or aiohttp builds a second resource for the same pattern. It resolves either way, which is what makes this easy to leave wrong |
| `source/server.py` | the module docstring's HTTP line |
| `docs/protocol.md` | the route table — method, auth, and what it returns |
| `README.md` | the route table, and the auth paragraph under it if the route is an exception to how the token travels |
| `docs/architecture.md` | the `_host_guard` paragraph, if it is a read route that exposes anything |
| `tests/` | `test_files.py` is the model: an aiohttp test server, no core and no browser. Every refusal is a case, and a delete test asserts the **disk**, not just the status — a 404 from a handler that already unlinked passes a status-only check |

Blocking work goes through `asyncio.to_thread`, like everywhere else: a
`scandir` over someone's photo library stalls every session's token stream
([principle 14](design-principles.md#14-the-core-is-shared-so-nothing-may-block-it)).
Cap anything that returns a list, and say so in the body — `/files` sends
`total` and `truncated` beside its 500.

## Add a client

Start from [protocol.md's "Writing a fourth client"](protocol.md#writing-a-fourth-client).
The minimum is: open the socket, read `hello`, send `submit`, accumulate `text`
deltas until `turn_end`. The Discord bot ignores reasoning, stats and all tool
traffic and is a complete client.

Do include, because the existing three learned these the hard way:

- **reconnect with `last_seq`**, and treat close code **1013** as "reconnect
  now" rather than an error
- **render nothing optimistically** — draw your own message when `turn_start`
  echoes it
- `reasoning=0` if you will never draw the thinking; `reasoning_stat` still
  arrives
- `origin` if your session is shared by several people

**Do not teach the core about your client.** If you need per-client behavior,
encode it into the session name — that is what the Discord bot does, and it is
why `models/sessions/` works for all three
([principle 4](design-principles.md#4-key-on-the-session-name-not-on-the-clients-identity)).

## Add a model

**`config.yaml` only** — no code. Add an entry under `models:`; the keys are
documented in `config.example.yaml`. Then `POST /reload` or `/reload`.

`build_engine()` monkey-patches eight policy attributes onto the
engine instance after construction: `context_length`, `max_output`,
`turn_timeout`, `reasoning`, `answer_time_reserve`, `max_pad_compactions`,
`resume_mode`, `reasoning_effort`. **They are not `LLMEngine.__init__`
parameters**, so grepping `llm_engine.py` for `max_pad_compactions` finds
nothing. That is where to add a ninth.

Schema quirk: `default_model` is a **sibling** of the model entries, not a
top-level key, and `known_models` filters it out by name.

Nothing extra is needed for a hosted assistant: the `# Directives` group in
`base.md` is stripped from every model's prompt, so the `## Forced conclusion`
text cannot be read as a standing instruction and suppress the answer
([principle 2](design-principles.md#2-behavior-is-markdown-and-some-of-that-markdown-is-machinery)).
The per-model `strip_directives` key that used to arrange this is gone.

## Change the behavior prompt

Edit `models/base.md` (all sessions), `models/<model>.md` (one model), or
`models/sessions/<name>.md` (one session), then `/reload` — the reads are
cached, so a running core will not notice otherwise.

**Two sections of `base.md` are machinery, not prose:** `## Checkpoint` and
`## Forced conclusion` are lifted out and sent at the moment each applies.
Renaming those headings breaks the extraction silently — it falls back to a
built-in string.

**Never let a behavior file contain the literal `=== CONTINUED SESSION ===`.**
`carryover()` partitions on it, so the prompt would grow every turn.

## Change the protocol

Bump `PROTOCOL_VERSION` in `core.py` **and** `web/app.js`. The two Python
clients import it; the browser hard-codes it.

New event types are cheap — clients ignore what they do not know. Changing an
existing event's shape is not: three renderers, no test that they agree.

If you add an event that should survive a reconnect, check
`Session.emit()`'s buffering rules: `reasoning` and
`reasoning_stat` are excluded entirely, and consecutive `text` deltas are
coalesced.

## Change what is persisted

Adding a field to the live-file payload: add it in `persist()`
and read it in `load_state()` with a default.

> ⚠️ **Do not bump `LIVE_SCHEMA` to do this.** `load_live` skips every file at
> a different version, so a bump silently discards every user's live
> conversation. `session_id` was added additively for exactly this reason.
> Bump only when the old shape is genuinely unreadable.

---

## Before you commit

- Run the affected test files.
- **`config.yaml` is gitignored and holds the API key and the bot token.**
  Never commit it, never paste its contents into a doc or an issue. Changes to
  configuration go in `config.example.yaml`.
- `models/sessions/*.md` are gitignored too (except the README) — their
  contents go straight into system prompts.
- If you changed a *decision* rather than an implementation, say the why in
  the doc section that describes the behaviour — not only in the commit
  message. There is no `decisions/` directory: a decision lives beside the
  thing it decided, in [design-principles.md](design-principles.md) or the
  relevant page, or it is lost.
