# Design principles

The rules this codebase actually follows, with the evidence for each. They are
written as rules because the point is to keep following them: a change that
breaks one of these is not automatically wrong, but it is a decision, and it
should be a deliberate one.

The [README](../README.md) explains *what* letsClaw does. This file explains
*why it is shaped this way*, so a change lands with the grain instead of
against it.

---

## 1. The core owns the conversation; clients are windows onto it

The core (`source/server.py` + `source/core.py`) is a long-lived service
holding every session's history, model and rollover state. The terminal, the
WebUI and the Discord bot are three separate processes that attach over one
WebSocket protocol and hold no conversation state of their own.

**Consequences that are load-bearing:**

- Closing a client mid-answer does not stop the turn. Reattach with `last_seq`
  and the missed output is replayed.
- Several clients can attach to one session and all see the same stream.
- Restarting the Discord bot costs nothing, because the session on the core's
  side *is* the conversation.

**The test of whether you are still following it:** the core must not import
`discord.py`, and it does not. It has no idea Discord exists. If a feature
needs the core to learn what kind of client is asking, look for a way to key it
on something the core already has — see principle 4.

## 2. Behavior is Markdown, and some of that Markdown is machinery

The system prompt is assembled from files, not from string literals in Python:

```
models/base.md                    every session
models/<model>.md                 per model entry
models/sessions/<session>.md      per session
<the rollover handoff>            after a rollover
```

Two sections of `models/base.md` are not prompt text at all — they are
templates the core lifts out and sends at the moment each applies:

| section | extracted by | sent when |
|---|---|---|
| `## Checkpoint` | `extract_checkpoint_directive` (`core.py:367`) | a full notepad is about to be compacted |
| `## Forced conclusion` | `extract_conclude_directive` (`core.py:362`) | the server cannot resume a cut-off thought |

So the wording of the machinery is tunable without touching code. That is the
principle: **anything that is really a prompt should live in a file the user
can edit and `/reload`, not in the source.**

`strip_directives` (per model) keeps those two blocks *out* of the prompt while
leaving extraction working — because a hosted assistant reads the whole prompt
as standing instruction and obeys "this is not the final answer" every turn,
whereas a local thinking model ignores it.

## 3. No hidden token injection

The context is the behavior files plus the conversation. Nothing else. No
framework preamble, no injected tool preamble beyond the schemas, no silent
memory retrieval.

This is why the token counts in the `⚡` line can be trusted, and it is the
thing to protect hardest when adding a feature: a feature that quietly adds
tokens to every prompt has broken the one promise the README opens with.

## 4. Key on the session name, not on the client's identity

Per-session behavior files are `models/sessions/<session name>.md`. The Discord
bot names each session after its channel (`<server>-<channel>`), so that file
*is* the per-channel behavior file — **without the core knowing what a channel
is**.

A design keyed on Discord channel ids would have been the obvious one and would
have been worse: it would have excluded `terminal.md` and every WebUI session,
and it would have put Discord's vocabulary inside the core.

The same reasoning picks a **directory over a config map**: sessions spring
into existence on attach, so there is no list to register them in. Dropping the
file in is the whole operation, and `ls models/sessions/` is the list.

**Generalise it:** when a client-specific feature is wanted, ask what the core
already holds that the client can encode into. Usually it is the session name.

## 5. Identity is the session id; the name is a label

Every session carries an 8-hex `session_id`, minted once (`core.py:456`),
unchanged by a rename, written into the live file, into every archive's
filename, and into `state/sessions/index.jsonl`.

The id **leads** the archive filename because it is the half that cannot
change: `ls state/sessions/a3f91c2e_*` finds every archive of one conversation
whatever it was called at the time. Grouping by name scatters one conversation
across two prefixes.

The name is still in the filename because an id alone tells you nothing at a
glance — but **nothing parses it.** It is for you, not the code.

## 6. Separate the notepad from the answer sheet

A thinking model left alone spends one allowance on both working-out and
answer, and a hard problem gets you a wall of half-finished thought and no
reply. letsClaw splits them:

- the **notepad** grows across resume rounds, bounded only by time and context
- the **answer sheet** (`max_output_tokens`) is held back, untouched, until the
  core closes the thought itself with `</think>`

Closing the thought is doing two jobs: it guarantees the answer budget is
really separate, and it guarantees an answer at all, because a model inside an
open thought will often never come out.

**There is deliberately no round count limit.** Time, context and the model
having nothing left to say are real constraints; a count is not one the machine
actually has.

## 7. Record what cannot be reconstructed

The JSON archive is the message list, and two things are structurally missing
from it: **the reasoning** (never enters history, by design) and **untruncated
tool output** (clipped to `max_output_chars` before it enters history).

So the journal exists — a `.md` file written as the turn runs, one block per
round, holding both in full. Without it a rolled-over window could see what the
previous one *concluded* but not what it *did*, and would re-do it.

The same principle one level up is the **RULED OUT** section in the rollover
handoff. A dead end is neither a settled fact nor outstanding work, so without
a section of its own it survives nowhere — and the fresh window walks back into
an approach the old one already eliminated. *Losing a fact costs a
re-derivation; losing a rejection costs a loop.*

## 8. Pick the format by who reads it

| format | used for | why |
|---|---|---|
| Markdown | the journal, behavior files, handoffs | read by you at a shell and by the model via `grep`/`sed`. `read_file` has no offset and truncates, so a 400 KB file must be reachable one header-line at a time |
| JSON | live session files, archives | loaded back by code |
| JSON Lines | `state/sessions/index.jsonl` | an append never rewrites what is there, so two sessions archiving at once cannot lose each other's entry; a torn write costs one line; and it is greppable |
| TSV | `logs/session_ID.log` | session names contain spaces (`solaris daq`), so tabs |

The journal being Markdown rather than JSON is the clearest case: its readers
are a human and a model, neither of whom can load a megabyte into context.

## 9. Fail toward the safe side

Every default and every parse failure resolves to the more restrictive option:

- `discord.users` is a **required** allowlist and the default empty list
  **refuses everybody**, including in guild channels. A bot is a remote shell.
- An unreadable `mention_only` warns and falls back to `true`, so a typo never
  opens the bot up.
- **A tool call that was cut off is never run.** `rm -rf /tmp/scratch`
  truncated to `rm -rf /` is the accident this prevents. The call is recorded
  empty and the model is told.
- Tool-markup parsing is **confined to the raw transport** (`llm_engine.py:20`).
  On the chat endpoint the server parses tool calls properly, so the same
  markup arriving as text is the model *writing about* a tool call — prose in
  an answer — and executing that would be a real hazard.
- A bad `rollover_mode` logs and uses `auto` (`core.py:428`).

## 10. The reflexive keystroke is the safe one

Three commands end a window and they are named by how much they destroy:

| | destroys |
|---|---|
| `/clear` | the conversation. Archives untouched |
| `/rollover` | nothing — archives it, writes a handoff, carries the thread |
| `/new` | the conversation **and every archive this session id ever wrote** |

`/new` is the destructive one precisely because `/clear` is the word people
type without thinking. The purge is matched on session id, so no other
session's files can be caught.

## 11. Validate by doing, and commit nothing until it works

`POST /reload` re-reads the config and **builds every engine** before applying
anything. A half-finished edit — the normal state of a file open in an editor —
comes back as `400` with the parser's complaint and the core carries on
untouched.

Building the engines *is* the validation, deliberately: it is the same code
path a real turn takes, so it catches a missing `base_url` that a schema check
would wave through.

Two refinements worth preserving:

- **A setting a session changed for itself is left alone.** The test is whether
  it still holds what the *old* config said — so a session that auto-disabled
  its own rollover is not dragged back into the loop it just escaped.
- The reply names what moved, key by key, so a reload says what it *did*.

## 12. Bookkeeping must never cost a turn

Journal writes never raise: losing the record is worth a log line, never a
turn, and one failure closes the segment rather than warning once per block
forever. Index writes only warn. The index is a convenience over the
transcripts, never a dependency.

Related: `session_id` was added to the live-file payload **without** bumping
`LIVE_SCHEMA`, because a bump makes `load_live` skip every file written before
it. Schema bumps discard data; only bump when the old shape is genuinely
unreadable.

## 13. A crash may cost the running turn and nothing more

- Live files are written by **atomic rename**, so a `kill -9` mid-write leaves
  the previous good copy rather than a truncated one.
- **A half-finished turn is never written.** `repair_history` (`core.py:586`)
  delegates to `settled()` so memory and disk agree: an unanswered tool call
  (which 400s forever after), a dangling tool result, or an unanswered question
  is unwound before saving. What comes back is always a conversation you could
  pick up.

## 14. The core is shared, so nothing may block it

- The three file tools do their reading and writing **off the event loop** —
  in a shared core one slow read would otherwise stall every session's stream.
- Each `exec` gets **its own process group**, so a timeout, a `/stop` or a core
  shutdown kills the children too instead of leaving a detached shell behind.
- `exec` runs in `tools.workdir` (default: the letsClaw directory) because the
  core is a daemon — its cwd is wherever `./serve.sh` was launched from, which
  is not the shell you were sitting in.
- One turn at a time per session: a second submit is refused with `busy` rather
  than silently queued behind a context its sender never saw.

## 15. One definition of where the repo is

`source/paths.py` holds `REPO_ROOT` and a `resolve()` that takes config paths
as-is when absolute and root-relative otherwise. Everything the code reads
lives at the root — `config.yaml`, `models/`, `web/`, `state/`, `logs/` — while
the code lives in `source/`.

**Anything needing a repo path uses those rather than computing its own from
`__file__`.** The scripts at the root run `source/server.py`, which puts
`source/` on the import path, so modules import each other by plain name
(`import core`) with no package machinery.

## 16. Clients render nothing optimistically

Your own message appears when the core echoes it back in `turn_start`, not when
you press enter. So every client attached to a session shows the same
transcript in the same order — including yours.

`tool_call` is emitted *before* the tool runs, so a slow `exec` is visible
rather than silent.

Read-only commands (`/info`, `/behavior`, bare `/model`, `/models`) come back as
a `response` to the asking client only, not broadcast to everyone attached.

## 17. Attaching is a connection, not a handshake

There is no attach message. The session name is a query parameter on the
socket, and `hello` — the first event on every connection — carries the
snapshot back. **So reconnecting is the same operation as connecting**, which
is what makes the Discord bot's idle-detach and the WebUI's auto-reconnect
cheap.

`?model=` applies **only when the attach is what creates the session**;
re-attaching never switches a live one, since that would move the conversation
out from under anyone else attached to it.

---

## Where the principles are known to be strained

Honest notes, not excuses:

- **Tools are unconfined by design.** `exec` runs shell commands and the file
  tools reach any path the process can. Anyone who can open a WebSocket to the
  core can run commands on the machine. The mitigations are `core.bind` on
  loopback, `core.token`, and — for the bot, which bridges past both —
  `discord.users`.
- **`?session=` is not validated on attach**, so a session named
  `../../etc/passwd` can exist. The guard is at the point where a name becomes
  a path that is *read*: `session_behavior_path` (`core.py:1781`) slugs the
  name and refuses anything that does not land directly in `session_dir`.
- **Archive filenames are not disambiguated.** Two rollovers of one session
  inside the same second overwrite. That needs a rollover whose handoff never
  runs, and is accepted rather than guarded. The *journal* name is
  disambiguated with `-2`, because there a collision would silently append a
  new window to a closed segment.
- **`/static/` serves the whole `web/` directory**, so the test helpers are
  reachable under it. Harmless while loopback-bound; worth knowing before
  exposing the port.
