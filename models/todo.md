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

## Improvements

* **Pre-turn rollover threshold is too low to protect a turn.**
  `_headroom_check()` rolls over only when `_pad_headroom()` is below
  `MIN_THINK_HEADROOM` (4,000). On a 100k window that trips at ~89.9k used —
  the same line as the 90% `rollover_at_percent` that `_after_turn()` uses — so
  a session can sit between the two and roll over at neither. Seen in
  dudus-den-general (Nov 4, turn 21): it ended at 88,368 / 100,000 with 5,536
  tok of thinking room, so the next turn starts with less than one thinking
  round (`max_tokens` 8,192) and room for about one tool round
  (`_min_tool_round_room()` 4,400) before the loop asks for a final answer.
  Proposed: roll before the turn unless the room fits one thinking round plus
  one tool round —
  `max(MIN_THINK_HEADROOM, lap_cap + self._min_tool_round_room())`, where
  `lap_cap` is `engine.extra_params["max_tokens"]` as in `_think_pad()` (12,592
  here; it rolls at ~84k used instead of ~89.9k). The lighter variant is
  `max(MIN_THINK_HEADROOM, lap_cap)`, one thinking round only. Change only the
  gate's threshold, not `_pad_headroom()` itself: the pad wall reads that same
  number (docs/reasoning.md, invariant 1). That session also had `/digest off`,
  which is what let one turn fill the window; `/digest collapse` is the
  existing mitigation.
