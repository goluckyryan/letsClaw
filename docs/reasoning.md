# Reasoning: tokens, walls, and where the thinking goes

The README's [Reasoning: the Notepad and the Answer
Sheet](../README.md#reasoning-the-notepad-and-the-answer-sheet) explains the
notepad as a user-facing idea. This document is the mechanical one: which
number caps which request, every way thinking can stop, where the reasoning
ends up, and which invariants a change here must not break.

Read it if you are tuning a model so a long thought survives, or if you are
about to touch `_answer_reserve`, `_pad_headroom`, `_headroom_check` or
`_think_pad`.

---

## 1. Two budgets, not one

There are exactly two token numbers in play, and conflating them is the
mistake this document exists to prevent.

| | `max_tokens` | the answer reserve |
|---|---|---|
| set by | config, per model | computed, per round |
| caps | **one round's total output** — on a thinking model that is reasoning *and* answer together | **the closing round**, which runs past `</think>` so every token buys answer |
| config knob | `max_tokens` | `max_output_tokens` is its **ceiling**, not its value |
| typical | 4,096–8,192 | 4,096 up to `max_output_tokens` |

`max_tokens` is a chunk size. A round cut off mid-reasoning is *resumed*, not
re-asked, so this number does not limit how much the model may think in total
— it only decides how many instalments the thinking arrives in. Smaller is not
cheaper: the pad is re-sent as a prefill every round, so halving it roughly
quadruples the prompt tokens processed for the same thinking.

**Nothing caps total reasoning in a turn.** The pad loop is unbounded in rounds
on purpose (`_think_pad()`): *"There is no round budget, because a count is not
a limit the machine actually has."* What stops it is time, context, or the
model running dry — all three of them real.

### Which round runs under which cap

| round | what it is | cap |
|---|---|---|
| first round | the opening request of a turn | `max_tokens` |
| tool round | came back asking to run a tool | `max_tokens` |
| resume round | continues a cut-off thought, pad handed back unclosed | `max_tokens` (`lap_cap`, in `_think_pad()`) |
| checkpoint round | condenses a full pad so thinking can continue | **the answer reserve** |
| closing round | thinking is shut with `</think>`, the model writes the reply | **the answer reserve** |
| forced conclusion | the server cannot resume; the directive goes in and the round re-runs | `max(answer reserve, max_tokens)` (`_forced_conclusion()`) |

## 2. The answer reserve, exactly

`Session._answer_reserve()`. Four clamps, in this order:

```
free     = budget - used_now()
want     = max(ANSWER_RESERVE_SHARE * free, MIN_ANSWER_ROOM)   # 10%, floor 4096
want     = min(want, max_output_tokens or max_tokens)          # ceiling
return     max(ABSOLUTE_ANSWER_FLOOR, min(want, free - PAD_CONTEXT_MARGIN))
```

with `ANSWER_RESERVE_SHARE = 0.10`, `MIN_ANSWER_ROOM = 4096`,
`PAD_CONTEXT_MARGIN = 2000`, `ABSOLUTE_ANSWER_FLOOR = 256` (module constants at the top of `core.py`).

Three consequences that are not obvious:

- **The ceiling wins against the floor.** A server capped below 4,096 cannot be
  talked into writing more by reserving more.
- **The room wins against both.** On a window smaller than the floor — a 12k
  context two-thirds full — reserving 4,096 reserves space that is not there,
  and the closing round would ask for a prompt plus an answer that together
  overflow. Clamped here, the pre-turn gate closes instead.
- **`ABSOLUTE_ANSWER_FLOOR` is never 0**, because some servers read 0 as "no
  limit" — the one reading under which an overflow is guaranteed.

### `max_output_tokens` only binds sometimes

It is a ceiling on a share, so it is **inert whenever 10% of the free window is
below it**. On a 100k window with `max_output_tokens: 32768`, the ceiling does
nothing until fewer than 32,768 tokens have been used; after that the 10% share
is the smaller number and the knob is slack. Raising it then buys nothing, and
lowering it buys nothing either.

This is why three notices in the code that say *"raising `max_output_tokens`
may help"* are usually wrong: they fire precisely when the reserve was pinned
by the share, the floor or the room clamp, none of which the ceiling can lift.
The advice that applies is **roll over**, or lower `rollover_at_percent`.

## 3. The pad wall

```
_pad_headroom() = budget - used_now() - _answer_reserve() - PAD_CONTEXT_MARGIN
```

Thinking continues while `_pad_headroom() - count_text(pad) > 0`.

Two things about `used_now()` matter here:

- It prefers the **server-measured** prompt size (`_last_used`) and only falls
  back to the local estimate. Which one is in force is reported at startup, so
  an estimate is never mistaken for a measurement. The estimate is calibrated
  rather than padded — a flat `×1.6` here used to make a half-full window read
  as full, which rolled sessions over to relieve pressure that was not there.
- `tally` publishes `_last_used` **mid-turn**, on every round whose prompt was
  exactly the current history (`history_shaped=True`, in `tally()`). A tool
  result landing between two pad rounds therefore moves the wall immediately.
  The corollary is an invariant — see §7.

**The pre-turn gate is not independent policy.** `_headroom_check()` rolls
the session over before a turn when
`_pad_headroom() < MIN_THINK_HEADROOM` (4,000). It reads the same
`_pad_headroom()` the wall does, and must keep doing so: when the two were
allowed to differ there was a band where the gate waved a turn through, the
wall then allowed zero thinking rounds, and the closing round asked for more
than the window held — a 400 that discarded the turn.

The gate's threshold is **absolute**, roughly
`MIN_ANSWER_ROOM + MIN_THINK_HEADROOM + PAD_CONTEXT_MARGIN ≈ 10,100` tokens of
free window. As a percentage it therefore depends entirely on window size:

| `context_length` | gate trips at about |
|---|---|
| 1,000,000 | 99% |
| 262,144 | 96% |
| 100,000 | 90% |
| 32,768 | 69% |
| 12,000 | 16% |

On a 100k window it lands on `rollover_at_percent: 90` and the headroom wall
and the rollover tick stop being two different walls. On a small window it is
still a much earlier second wall, and that is the configuration to watch.

The gate is also **skipped entirely** unless `rollover_mode == "auto"` **and**
`rollover_before_think` **and** `rollover_pct != 0`. A session can reach the
last of those on its own: `_after_turn` auto-disables rollover when even a
fresh window already trips the threshold. In that state a turn may start with
no thinking room, the pad loop allows ~zero rounds, and nothing says so.

## 4. Every way thinking stops

Eight exits. The first is the only one that loses work.

| # | exit | notice | work lost |
|---|---|---|---|
| 1 | **`resume_transport()` is `off`** — the server cannot prefill, so `_think_pad()` goes straight to `_forced_conclusion()` | `reasoning was cut off and this server cannot resume it` | **the whole seed round's reasoning** |
| 2 | time — `turn_timeout - answer_time_reserve` reached | `thinking stopped at the N s answer reserve` | none; concludes from the full pad |
| 3 | context, compaction exhausted — `compactions >= max_pad_compactions`, which a **negative** setting switches off entirely | `thinking filled the context` | none |
| 4 | context, pad too small to compact — `count_text(pad) <= PAD_TAIL_TOKENS * 2` (4,000); everything would survive as the verbatim tail anyway | `…too little to compact` | none |
| 5 | a checkpoint failed **and** the pad's tail was no shorter than the pad — `_compact_pad()` returned `""` and `_pad_tail()` could not shrink it | — | none: *"concluding from a complete pad is the fallback, losing it is never"* |
| 6 | the model answered — it closed the block itself and the text was complete | — | none |
| 7 | the model stopped without text and without being cut off | — | none |
| 8 | no progress — cut off at the cap having written nothing, so the pad cannot grow and every further round would be identical | `reasoning stopped making progress` | none |

Separately, the round loop in `_turn()` ends the turn's *work* independently
of any of this, on the same rule the pad uses and for the same reason: when
`_pad_headroom()` drops below `MIN_TOOL_ROUND_ROOM` (4,000 — one clipped tool
result plus the message and arguments carrying it), it stops asking for work
and asks for words: `no room left for another tool round — asking for a final
answer`. It reads `_pad_headroom()` rather than a figure of its own, which is
invariant 1 in §7: a third name for one number is how the gate and the wall
drifted apart before.

Exit 5 deserves a note, because it used to be the worst of them. A checkpoint
that came back empty ended the turn's thinking outright — with
`max_pad_compactions: 10`, one bad note on the first cycle cost the other
nine. It now falls back to keeping the pad's tail (`_pad_tail()`,
`PAD_TAIL_TOKENS`) and thinking on. That is lossier than a real checkpoint —
the older reasoning is dropped mechanically instead of being distilled — but
the tail is the resume point, which is the part that has to survive verbatim
anyway. Only when even the tail cannot shrink the pad does the turn close,
and it closes by concluding from a complete pad rather than by losing it.

## 5. Where the reasoning goes

**Not into history.** The pad lives in `_think_pad` and nowhere else; it is
re-sent as a prefill each resume round and is gone at turn end. It costs
nothing afterwards and cannot move the rollover point. Rounds carrying it are
tallied with `history_shaped=False` so they do not corrupt the window
measurement either.

**Into the journal, every round.** `state/sessions/*.md`, written as the turn
runs, under greppable headers `## turn N {round|resume|closing} M`
(`call_round()`). This is the only durable copy — see design principle 7.

**Into the digest, in paraphrase, when `digest_turns: true`.** `_digest_turn`
passes the last `DIGEST_PAD_TAIL_CHARS` of the pad to
`build_turn_digest`, because a digest written without it *"can describe what
happened and not what it was for"*. The digest **is** appended to history. It
is counted like any other message, so principle 3 holds — but "reasoning is
never in history" is true of raw reasoning only.

### How reasoning is parsed off the wire

Two channels. Both transports read both, through one shared scanner:

| transport | dedicated channel | inline `<think>` |
|---|---|---|
| `chat_with_tools()` | `reasoning_content` | `InlineThinkSplitter`, per delta and on the whole message |
| `_raw_complete()` | none — /completion has no such field | `InlineThinkSplitter`, seeded `start="reasoning"` |

`InlineThinkSplitter` is one class on purpose. There were two scanners for
this tag, they disagreed at the boundaries, and the disagreement was a real
bug: a close tag ending exactly on a chunk boundary leaked the tag's own
newlines into the answer. Its three entry states are the difference between
the transports — `deciding` (chat: a tag may or may not be first),
`reasoning` (raw, growing a pad: the prefill already opened the block) and
`passthrough` (raw, closing round: already past the close tag).

llama.cpp's `--reasoning-format` decides which channel the server uses:

| mode | thoughts arrive in | effect |
|---|---|---|
| `auto` / `deepseek` | `message.reasoning_content` | correct |
| `none` | `message.content` | caught by the splitter and kept out of history |
| `deepseek-legacy` | both | caught, and not double-counted |

**The splitter engages only on a stream that *opens* with the tag.** That is
what a template emits, and the restriction is what lets an answer that merely
writes about these tags — an ordinary thing to do in this repo — through
untouched.

The startup probe (`LLMEngine.capabilities()`) acts on what it finds rather
than only reporting it, and the useful verdict is the negative one. The
splitter defaults **on**, because its two failure modes are not comparable: a
trace let into history is permanent and invisible, while a tag stripped from
an answer is visible at once. A `separate` verdict is the only proof that the
server has a channel of its own, and it is what switches the splitter off.
`silent` and `unknown` are not proof and leave it armed.

Under `none` or `deepseek-legacy` the probe still warns, because a server
nobody fixed goes on making letsClaw recover its thinking from markup — which
works, but is a guess where there could be a field.

## 6. Tuning so a long thought survives

In rough order of effect:

| want | do | not |
|---|---|---|
| more total thinking | raise `context_length`; turn on `digest_turns` so turns start near-empty | ~~lower `max_output_tokens`~~ — inert unless it is already below 10% of free |
| thinking to survive a full pad | `max_pad_compactions: 10`, or **any negative number for no limit at all** (`0` means *never compact* — the opposite) | |
| fewer round-trips for the same thinking | raise `max_tokens` | |
| a longer final answer | raise `max_output_tokens` — but only helps if 10% of your free window already exceeds the old value | |
| never silently truncated | `--no-context-shift` on llama.cpp, so an overflow is a hard error rather than amnesia | ~~`--context-shift`~~ |
| reasoning out of history | leave `--reasoning-format` unset (`auto`) — the splitter covers you either way, but then nothing has to be inferred from markup | ~~`none`~~, ~~`deepseek-legacy`~~ |

Known gaps, as of this writing:

- **Exit 1 is the one that still loses work.** A non-resuming server discards
  the seed round's reasoning and caps the turn at one round of `max_tokens`.
  Nothing here can fix that from the client side, so verify your server
  resumes before relying on long thoughts — `/info` reports the transport.
- An unlimited `max_pad_compactions` is bounded only by the turn clock and by
  the pad having to shrink on every cycle. On a server whose checkpoints are
  poor, that is a lot of rounds spent before the clock notices.
- The gate's absolute ~10,100-token threshold is a much earlier second wall on
  a small window than on a large one (see the table in §3), and nothing scales
  it. A 32k window trips it at 69%.

## 7. Invariants for anyone changing this

1. **The gate and the wall read the same number.** `_headroom_check` is a
   prediction of what `_pad_headroom` will do. Decoupling them makes the gate
   lie, and the failure mode is a 400 that discards the turn.
2. **The reserve is read *after* the pad loop, not before it**.
   A tool can run mid-pad and move `used_now()`; a cap captured at the top of
   `_think_pad` would be stale, which is invariant 1 broken by another route.
3. **Every path that changes what the next prompt contains must clear
   `_last_used`.** Because `tally` publishes mid-turn, a stale measurement
   describes a window that no longer exists and triggers a rollover to relieve
   pressure already relieved. Most of those paths reshape `history`:
   `repair_history()`, `_digest_turn()`, the `max_history` trim in
   `_after_turn()`, `roll_over()`, `/clear`, `/new`. Two do not, and are the
   easy ones to miss — `use_model()` rebuilds the system message from another
   model's layers *and* changes the budget the measurement is compared
   against, and `apply_config()` leaves the history alone but can change the
   tool schemas, which are the other half of every prompt.
4. **The pad never enters history.** Rounds carrying it tally with
   `history_shaped=False`.
5. **`_pad_tail` is verbatim.** The last ~2,000 tokens are never summarised,
   because the model has to resume *mid-sentence*.
6. **On any failure, keep the pad.** Concluding from a complete pad is the
   fallback; losing it is never.

## See also

- [README — Reasoning: the Notepad and the Answer Sheet](../README.md#reasoning-the-notepad-and-the-answer-sheet) — the same machinery for a reader who is not editing it
- [design-principles.md #6](design-principles.md) — why the notepad and the answer sheet are separate at all
- [design-principles.md #7, #18](design-principles.md) — record what cannot be reconstructed; elide only what can
- [architecture.md](architecture.md) — where these functions sit
