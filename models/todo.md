# TODO

Open work only. Completed designs move to [`docs/decisions/`](../docs/decisions/)
as dated records; the codebase itself is documented in [`docs/`](../docs/).

---

## Bugs

* **`config.yaml` has `max_messages: 888888`.** The default is 8, and the
  comment next to it says *"longer answers go up as answer.md instead of
  flooding the channel"* — which is the **inverse** of what the value does.
  `discord_client.py:606` uses the `answer.md` fallback only when
  `len(chunks) > max_messages`, so at 888888 it can never fire and a long
  answer goes out as forty messages. Almost certainly a debugging leftover.
  (`config.yaml` is gitignored, so this is a local fix, not a commit.)

## Documentation gaps

* **`strip_directives` is undocumented in the README.** It is described in
  `config.example.yaml` and in
  [docs/design-principles.md](../docs/design-principles.md#2-behavior-is-markdown-and-some-of-that-markdown-is-machinery),
  but appears zero times in `README.md`. It is the setting that makes hosted
  assistants answer at all, so an operator needs to find it.

## Testing

* **The Discord live test is still not automated.** A real bot from the dev
  portal, Message Content Intent on, driving a real channel. Everything below
  the gateway is proven by `test_discord.py` (fakes) and
  `test_discord_live.py` (a real core, no Discord); the gateway itself is
  exercised only by hand. This is the one gap that cannot be closed with
  fakes. See
  [the Discord record](../docs/decisions/2026-09-25-discord-client.md#the-live-test-still-not-automated).

* **Nothing checks that the three clients agree.** Each of `/info`,
  `/behavior`, `/models`, `/reload`, `rollover_ask`, `session_state` and
  `stats` has two or three independent renderers, and the three command tables
  have already drifted. Adding a field means editing up to three places with
  nothing to catch a miss. See
  [clients.md](../docs/clients.md#what-is-shared-and-what-is-not) — whether to
  fix it is a genuine trade, not an obvious win.

## Known rough edges

Carried from the code read; each is currently *accepted*, not scheduled.

* **`extract_directive` returns the conclude fallback for an empty checkpoint
  paragraph** (`core.py:397`) — a latent copy-paste, harmless today because
  the section is non-empty.
* **A session pinned to a model dropped from the config keeps a live engine
  that is no longer in `_engines`**, so `SessionManager.close()` will not close
  it at shutdown.
* **Archive filenames are not disambiguated** — two rollovers of one session
  inside the same second overwrite. The journal's name *is* disambiguated.
* **The terminal client has no reconnect, no `last_seq` and no auth handling.**
  Fine for a local REPL; the first thing to fix if it becomes more.
* **`PROTOCOL_VERSION` is hard-coded in `web/app.js:37`** while the Python
  clients import it. A bump means editing two places.

## Wanted, not built

The README's [Future Features](../README.md#future-features), restated here so
they are in one place.

* **Semantic search** — embed past messages and sessions and search them by
  meaning rather than keyword.
* **Memory read-back** — rollover writes `state/memory_store/long_term.md`,
  but a new window is seeded only from its own predecessor, never from the
  whole file.
* **Discord draft streaming** — live-edited messages as the answer builds,
  instead of one post at `turn_end`.

Both of the first two put tokens in a prompt that the user did not write, so
whatever shape they take has to stay compatible with
[principle 3](../docs/design-principles.md#3-no-hidden-token-injection) —
visible and accounted for, not silent.

## Rejected, deliberately

Not oversights — listed so they are not re-proposed as new ideas.

* **Multi-account Discord.**
* **Per-*user* behavior inside a shared channel.** One channel is one session
  and one conversation, so there is nowhere to hang it. `origin` records who
  typed but does not fork the conversation; making it do so would mean one
  channel holding N conversations — a different feature.
* **`chunkMode: "newline"`** — the half of openclaw's chunker that serves
  draft streaming. Revisit only if draft streaming is built.
