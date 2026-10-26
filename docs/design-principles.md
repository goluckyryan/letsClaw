# Design principles

The rules this codebase actually follows, with the evidence for each. They are
written as rules because the point is to keep following them: a change that
breaks one of these is not automatically wrong, but it is a decision, and it
should be a deliberate one.

The [README](../README.md) explains *what* letsClaw does. This file explains
*why it is shaped this way*, so a change lands with the grain instead of
against it.

---

## Vocabulary

Four words below are used as if they were defined, and three of them name
different things that all sound like "the conversation". The one that causes
real mistakes is `history`:

| term | what it is | lives |
|---|---|---|
| `history` | the message list sent to the model each round — **this is the context** | memory; `history[0]` is the system message |
| transcript | the message list archived at rollover | `state/sessions/*.json` |
| journal | reasoning, every tool call, every **full** tool result, written as the turn runs | `state/sessions/*.md` |
| live file | the session, so a restart does not lose it | `state/live/*.json` |

**`history` is not a disk record.** It is the prompt. Shrinking it costs the
model what it can see, not what was kept — principle 7 is about the journal,
which is why reasoning can be absent from one and complete in the other.

Two things are context but not history: the **tool schemas**, sent as the
`tools` parameter and counted once per core, and the **thinking pad**, re-sent
as a prefill each resume round and gone at the end of the turn. So:

```
context = history + tool schemas + (within a turn) the pad
```

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
| `## Checkpoint` | `extract_checkpoint_directive()` | a full notepad is about to be compacted |
| `## Forced conclusion` | `extract_conclude_directive()` | the server cannot resume a cut-off thought |

So the wording of the machinery is tunable without touching code. That is the
principle: **anything that is really a prompt should live in a file the user
can edit and `/reload`, not in the source.**

The corollary is that machinery must not *also* be prompt.
`strip_directive_sections()` cuts the whole `# Directives` group out of the
base layer for every model, leaving extraction working — because a hosted
assistant reads the whole prompt as standing instruction and obeys "this is not
the final answer" every turn, and because a local thinking model that merely
ignores the copy is still paying for it in tokens. It was once opt-in
(`strip_directives` per model); the key is gone.

## 3. No hidden token injection

The context is the behavior files plus the conversation. Nothing else. No
framework preamble, no injected tool preamble beyond the schemas, no silent
memory retrieval.

This is why the token counts in the `⚡` line can be trusted, and it is the
thing to protect hardest when adding a feature: a feature that quietly adds
tokens to every prompt has broken the one promise the README opens with.

Taking tokens away is bound by the same rule, which is why the turn digest
([principle 18](#18-elide-only-what-can-be-reconstructed)) is a *replacement*
and not an addition: it is counted like any other message, it is visible in
the conversation as the assistant message it is, and the turn it stands in for
is gone rather than hidden. An elision the `⚡` line cannot see would break
the promise in the other direction.

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

Every session carries an 8-hex `session_id`, minted once in `Session.__init__()`,
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
- the **answer sheet** (`Session._answer_reserve()`: 10% of the free window,
  floored at `MIN_ANSWER_ROOM`, ceilinged by `max_output_tokens`, clamped by
  the room actually left) is worked out at the moment the thinking stops, and
  the core then closes the thought itself with `</think>`

  It is a share rather than a fixed block because a fixed `max_output_tokens`
  reserved a third of a 100k window permanently: the pre-turn gate fired at
  61% while every indicator said 90%.

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

This principle has a dual, which is what lets the journal pay for itself:
once a thing is recorded it no longer has to be carried. See
[principle 18](#18-elide-only-what-can-be-reconstructed).

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
- Tool-markup parsing is **confined to the raw transport** (`ToolMarkupFilter`).
  On the chat endpoint the server parses tool calls properly, so the same
  markup arriving as text is the model *writing about* a tool call — prose in
  an answer — and executing that would be a real hazard.
- A bad `rollover_mode` logs and uses `auto` (`Session.__init__()`).

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
- **A half-finished turn is never written.** `repair_history()`
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

Read-only commands (`/info`, `/behavior`, bare `/model`) come back as
a `response` to the asking client only, not broadcast to everyone attached.
Bare `/model` is read-only in the strong sense: it is answered *above* the busy
guard, so you can see what you could switch to while a turn is running. Passing
a name is a switch, and a switch waits.

## 17. Attaching is a connection, not a handshake

There is no attach message. The session name is a query parameter on the
socket, and `hello` — the first event on every connection — carries the
snapshot back. **So reconnecting is the same operation as connecting**, which
is what makes the Discord bot's idle-detach and the WebUI's auto-reconnect
cheap.

`?model=` applies **only when the attach is what creates the session**;
re-attaching never switches a live one, since that would move the conversation
out from under anyone else attached to it.

## 18. Elide only what can be reconstructed

Principle 7 says record what cannot be rebuilt. This is the same rule from the
other end: `history` is not an accumulator, and anything still in it should
still be doing work.

Raw reasoning is never there — the notepad lives in `_think_pad` and dies with
the turn. A turn's tool traffic — the calls, their arguments, and the results
they returned — is collapsed to a short digest once the journal holds it
verbatim. The pad's tail is an *input* to that digest, deliberately: a digest
written without it can describe what happened and not what it was for. So with
`digest_turns` on, a paraphrase of the reasoning does join history — counted
and visible like any other message, never injected behind the count. Across four measured sessions that traffic was
91-95% of the window, against 2-8% for the answers a person actually reads.

The user's prompts and those answers are never elided. They are the only
things in a window that cannot be recovered any other way: the journal can
reproduce what a command printed, but not what someone meant to ask.

Two consequences, both load-bearing:

- **The collapse is gated on the record, never assumed.** `jot` never raises,
  by principle 12 — so a journal that filled the disk on the turn's first tool
  call is indistinguishable from one that recorded it perfectly, unless
  something checks. Dropping the only copy on that evidence is not a degraded
  turn, it is silent total loss. See `Session.journal_ok()` and the block
  high-water mark in `_digest_turn`.
- **What replaces it says where the rest went.** Every digest ends with the
  `grep` line that reaches its own turn in the open journal segment, because a
  summary the model cannot drill into is just amnesia it does not know it has.

## 19. The agent decides; asking is the last resort

The point of a turn is that it finishes. A model that stops to ask *"shall I do
A or B?"* has not paused the work — it has ended it, and the difference is
invisible from the outside, which is why this has to be stated as a rule rather
than assumed as good sense.

**The mechanism is the reason it is a rule.** The agent loop in `Session._turn()`
is `while True:` around one test: keep going while the model asked for a tool.
The moment it emits text instead, the turn is over. Nothing distinguishes a
finished answer from a question — there is no `endswith("?")` anywhere in the
core, and there could not be a useful one, since a good answer can end with a
question and a refusal can end with a period. So the loop cannot catch it, the
reasoning machinery cannot catch it, and no config key can. **The only lever is
the prompt** — which makes this the clearest case of [principle
2](#2-behavior-is-markdown-and-some-of-that-markdown-is-machinery): the rule is
`## How you work` in `models/base.md`, tunable with `/reload`, and if it is
weakened there is no backstop behind it.

What `base.md` therefore commits to, in this order:

| rule | what it forecloses |
|---|---|
| Check before you answer | answering from memory when a tool would have settled it |
| Decide, don't ask | stopping at a fork — investigate both branches with tools, pick one, say which and why the others are worse |
| Ask only when you genuinely cannot proceed | questions as a substitute for looking. Three cases and no more: it would destroy data, it would cost money, or the fact lives only in the user's head and no tool reaches it |
| Finish the whole task before you answer | reporting back part-way to check in |

**"Say which you picked and why the others are worse" is load-bearing, not
politeness.** A decision the reader cannot audit is indistinguishable from a
guess, and [principle 7](#7-record-what-cannot-be-reconstructed)'s RULED OUT
section exists for the same reason: a rejected approach that is never written
down gets tried again by whoever comes next — in the next window, or by the
same model one rollover later.

**The honest limit.** This principle governs the model's *choices*; it does not
govern the room available to make them. The loop stops on `_pad_headroom()
< MIN_TOOL_ROUND_ROOM` and asks for words, and by
[principle 20](#20-rollover-belongs-to-the-turn-not-the-round) nothing mid-turn
can roll over. An unfinished task at that wall is cut off, and the fresh window
starts on the *next* turn rather than resuming this one. `digest_turns` is the
mitigation, because it is what keeps the wall from arriving mid-task at all —
which is the practical reason it exists.

## 20. Rollover belongs to the turn, not the round

Nothing rolls over while a turn is in flight. `roll_over()` has exactly three
call sites, and every one sits at a turn boundary:

| call site | when | gated on |
|---|---|---|
| `_headroom_check()` | **before** a turn starts | `rollover_before_think`, `mode == auto`, `rollover_pct`, `rollover_blocked` |
| `_after_turn()` | after the tool loop has fully drained | the trip point, and `rollover_mode` via `_maybe_roll()` |
| `command()` — `/rollover` | when a person asks | **`self.busy` only** |

The boundary is not a convenience, it is a correctness requirement: rolling
over mid-loop would archive a `tool_call` whose `tool_result` had not been
written, and an unanswered tool call is the exact shape that
[`settled()`](../source/core.py) exists to unwind because it **400s forever
after** ([principle 13](#13-a-crash-may-cost-the-running-turn-and-nothing-more)).
The comment on the `_after_turn` call says it plainly: *"Rollover is evaluated
only here: the tool loop has fully drained, so no tool_call/tool_result pair can
be split."* Keep it that way.

**"Automatic" and "manual" are not the same gate, and the difference is
deliberate.** Automatic rollover is subject to `rollover_mode` — `auto` rolls
and says so, `ask` prompts, `off` warns once — to the configured percentage, and
to the `rollover_blocked` guard a failed archive sets. Manual rollover is
subject to none of those: `roll_over()` never reads `rollover_mode`, the command
passes its own `trip` rather than consulting the threshold, and it *clears*
`rollover_blocked` first, because someone who just freed the disk has said so by
typing the word. A person asking is the one event that overrides a policy
setting, and that is the point of having both.

**What "any time" means, precisely.** Manual means any time *between* turns, in
any mode, at any window size. It does not mean during a turn: the `self.busy`
guard refuses it, deliberately sharing that guard with `/clear` and `/new`,
which destroy more. The route to a rollover while a turn is running is `/stop`,
which unwinds the unfinished turn through `repair_history()` first, then
`/rollover`. Two keystrokes rather than one, because the intermediate state is
a conversation with no dangling tool call — which is the thing the whole
principle protects.

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
  a path that is *read*: `session_behavior_path()` slugs the
  name and refuses anything that does not land directly in `session_dir`.
- **Archive filenames are not disambiguated.** Two rollovers of one session
  inside the same second overwrite. That needs a rollover whose handoff never
  runs, and is accepted rather than guarded. The *journal* name is
  disambiguated with `-2`, because there a collision would silently append a
  new window to a closed segment.
- **`/static/` serves the whole `web/` directory** — `app.js`, `index.html`,
  `style.css` and the vendored KaTeX, nothing else. The test helpers that used to
  sit in `web/` (`test_md.mjs`, `browser_test.py`, `shot.py`) live in `tests/`,
  which the core does not serve. Harmless while loopback-bound; worth knowing
  before exposing the port.
