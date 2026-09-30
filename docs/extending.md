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
Each file runs its own cases and reports. None need network or a bot token
except `test_discord_live.py`, which starts a real core on a temp config
(still no Discord).

| file | covers |
|---|---|
| `test_chunk.py` | the fence-aware chunker, incl. CJK and the real 2000 boundary |
| `test_clear_new.py` | `/clear` vs `/new` — what each destroys |
| `test_discord.py` | gate, routing, rendering, all on fakes |
| `test_discord_live.py` | a real core over the real protocol |
| `test_journal.py` | the journal |
| `test_session_behavior.py` | the four-layer prompt, path safety, rename |
| `test_session_index.py` | `index.jsonl` and the id registry |
| `test_think_pad.py` | the notepad loop |
| `test_tool_markup.py` | raw-transport tool-call recovery |

> ⚠️ **`tests/` is in `.gitignore`.** A fresh clone has no test suite. If you
> are picking this up from a clone and `tests/` is missing, that is why — ask
> for it rather than assuming there was never one.

There is no CI. Run the affected files by hand before committing.

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
The three file tools use `to_thread` for exactly this. If you spawn a process,
copy `exec`'s `start_new_session=True` and `_kill_group` handling so a
cancellation does not leave it detached.

Tool schemas are counted into the prompt budget **once per core**
(`SessionManager.__init__`), not per session. A verbose description is paid for
by every session, every turn.

## Add a command

**`Session.command`** (`core.py:1566`), plus a renderer in each client that
should show it.

Decide first whether it is **read-only**. Read-only commands go *before* the
`if self.lock.locked()` check and their reply is unicast to the asking client
only ([principle 16](design-principles.md#16-clients-render-nothing-optimistically)).
Everything that mutates goes after it, and returns
`{"ok": false, "error": "a turn is running…"}` when busy.

`/reload` is deliberately in the read-only group even though it changes
everything — taking the turn lock there deadlocks, because `reload` defers busy
sessions and would defer the one that asked.

**The trap:** there are **three command tables** — `chat.py`, `web/app.js`,
`discord_client.py` — and they have already drifted. Nothing tells you if you
miss one. See [clients.md](clients.md#what-is-shared-and-what-is-not).

If the command mutates session state that other attached clients should see,
emit a `session_state` event as well as returning the reply.

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

`build_engine` (`core.py:137`) monkey-patches nine policy attributes onto the
engine instance after construction: `context_length`, `max_output`,
`turn_timeout`, `reasoning`, `answer_time_reserve`, `max_pad_compactions`,
`resume_mode`, `reasoning_effort`. **They are not `LLMEngine.__init__`
parameters**, so grepping `llm_engine.py` for `max_pad_compactions` finds
nothing. That is where to add a tenth.

Schema quirk: `default_model` is a **sibling** of the model entries, not a
top-level key, and `known_models` filters it out by name.

If the model is a hosted assistant rather than a local thinking model, consider
`strip_directives: true` — the `## Forced conclusion` text reads as a standing
instruction to some hosted models and can suppress the answer entirely
([principle 2](design-principles.md#2-behavior-is-markdown-and-some-of-that-markdown-is-machinery)).

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

Bump `PROTOCOL_VERSION` in `core.py:39` **and** `web/app.js:37`. The two Python
clients import it; the browser hard-codes it.

New event types are cheap — clients ignore what they do not know. Changing an
existing event's shape is not: three renderers, no test that they agree.

If you add an event that should survive a reconnect, check
`Session.emit`'s buffering rules (`core.py:488`): `reasoning` and
`reasoning_stat` are excluded entirely, and consecutive `text` deltas are
coalesced.

## Change what is persisted

Adding a field to the live-file payload: add it in `persist()` (`core.py:692`)
and read it in `load_state()` (`core.py:728`) with a default.

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
- If you changed a decision rather than an implementation, add a record under
  [`decisions/`](decisions/) rather than only a commit message.
