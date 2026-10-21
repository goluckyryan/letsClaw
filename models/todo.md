# TODO

Open work only — completed designs are documented where they live, in
[`docs/`](../docs/), and the commit that shipped them is the record of the
decision. The [README](../README.md) is the user and operator manual.

## Wanted, not built

The two worth scheduling; the README's
[Future Features](../README.md#future-features) is the full list, and also
names the Discord and sub-agent ideas.

* **Semantic search** — embed past messages and sessions and search them by
  meaning rather than keyword.
* **Memory read-back** — rollover writes `state/memory_store/long_term.md`,
  but a new window is seeded only from its own predecessor, never from the
  whole file.
