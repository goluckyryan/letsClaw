# letsClaw — developer docs

**For someone about to change this code**, human or agent. The
[top-level README](../README.md) is the front page — what it is, how to install
it, how to drive it, and where to go from here. Everything that needs the
source open to be useful lives here instead: the module map, the protocol
reference, the per-client internals, and the tuning arithmetic. The split is
deliberate — a reader who has never opened `source/` should be able to stop at
the README and still know how to use the thing.

## Read in this order

1. **[architecture.md](architecture.md)** — the module map, the turn lifecycle,
   the thinking pad, the behavior layers, rollover, the digest, config reload,
   the disk layer. Start here. It ends with a numbered list of traps that is
   worth reading even if you skip the rest.
2. **[design-principles.md](design-principles.md)** — the 20 rules the codebase
   actually follows, each with its evidence, plus an honest section on where
   they are strained. Read this before proposing a change, so it lands with the
   grain.
3. **[protocol.md](protocol.md)** — the WebSocket and HTTP reference. Needed if
   you touch `server.py`, any client, or any event.
4. **[clients.md](clients.md)** — what the terminal, browser and Discord
   clients each do, and what they duplicate.
5. **[extending.md](extending.md)** — recipes: add a tool, a command, a client,
   a model; change the prompt, the protocol or the persisted shape. Also how to
   run the tests.
6. **[reasoning.md](reasoning.md)** — reference for the token side of the
   thinking pad: which cap applies to which round, how the answer reserve is
   derived, every way thinking can stop, and the invariants a change there must
   not break. Read it before touching `_answer_reserve`, `_pad_headroom`,
   `_headroom_check` or `_think_pad`.
7. **[tokens.md](tokens.md)** — the input side, in plain language: what fills
   the window on every round, why one turn is several prompts, and how the
   counter behaves — which figures are the server's truth, which are
   estimates, and how accurate the estimates are. Written to be readable
   without the source open.

There is no separate record of decisions. A feature's *why* lives with its
*what*: the shipped explanation is the doc section that describes the code, and
[design-principles.md](design-principles.md) is where a rule and its reasoning
are kept together. What is not built stays in `models/todo.md`, and the commit
message is the record of why something was rejected — so a decision worth
keeping is worth writing into a doc.

## The shortest possible orientation

A long-lived core process (`source/server.py` + `source/core.py`) holds every
conversation. Three thin clients attach over one WebSocket protocol and hold no
state of their own. The system prompt is assembled from Markdown files in
`models/`. When the context fills, the core archives the conversation, has the
model write itself a handoff, and starts a fresh window carrying it.

**The one rule that matters more than the rest:** `Session.emit()` is
synchronous and must never `await`. The model's streaming callback runs inside
the SSE read loop, so blocking there stalls every session at once.

## What is not in here

- **Installation, the slash-command reference, what each client is for** — the
  [top-level README](../README.md). It is the manual; this is the workshop.
- **Every configuration key** — `config.example.yaml`, which is the template
  you copy, and therefore the one place a key cannot be out of date. The
  README names the sections; the comments in the template say what each key
  does and what happens if you set it wrong.
- **`config.yaml`** — gitignored; it holds the API key and the Discord bot
  token. Do not commit it or quote it.
- **`tests/`** — also gitignored, so a fresh clone has no test suite. That
  includes the three suites that police *these* pages —
  `test_doc_pointers.py`, `test_doc_claims.py` and `test_command_surface.py` —
  so the checks that keep the docs honest do not travel with a clone: a fork
  inherits the prose and none of the gates. See
  [extending.md](extending.md#running-the-tests).
- **`docs/readme-split-plan.md`** — the plan that moved most of the top-level
  README's technical detail here, **carried out in full on 2026-10-30**. Also
  gitignored, so unlike the rest of this list it is not a category of thing the
  repo omits but one file this machine happens to keep: the working note behind
  the split, never committed by request. Its line numbers are the README's
  *before* the split, so it reads as history even where it survives — the
  sections it calls out as not-yet-existing (a `## Digest` in
  `architecture.md`, a `### What base.md commits the model to`) all exist now.
  Everything durable from it was written into the pages above; nothing here
  depends on it.
- **`docs/webcam-voice-plan.md`** — a plan, not a description of shipped code,
  and so not in the reading order above. It names functions and devices that do
  not exist yet, which is what a plan is for; `test_doc_pointers.py` therefore
  excludes `*-plan.md` by name. It becomes checkable when it becomes
  architecture.
- **Open work** — `models/todo.md`.
