# letsClaw — developer docs

**For someone about to change this code**, human or agent. The
[top-level README](../README.md) is the user and operator manual — how to
install it, configure it and drive it. These pages are the other half: how it
is built, why it is built that way, and where the edges are.

## Read in this order

1. **[architecture.md](architecture.md)** — the module map, the turn lifecycle,
   the thinking pad, rollover, config reload, the disk layer. Start here. It
   ends with a numbered list of traps that is worth reading even if you skip
   the rest.
2. **[design-principles.md](design-principles.md)** — the 17 rules the codebase
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

[`decisions/`](decisions/) holds dated records of features that shipped: what
was decided, what was rejected, and what deliberately was not built. They were
moved out of `models/todo.md` when the work landed.

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

- **Installation, configuration keys, command reference, tuning** — the
  [top-level README](../README.md) and `config.example.yaml`.
- **`config.yaml`** — gitignored; it holds the API key and the Discord bot
  token. Do not commit it or quote it.
- **`tests/`** — also gitignored, so a fresh clone has no test suite. See
  [extending.md](extending.md#running-the-tests).
- **Open work** — `models/todo.md`.
