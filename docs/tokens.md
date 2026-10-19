# Tokens: what gets counted, and whether the counter is right

**Who this is for:** anyone who reads the `⚡` line or the WebUI gauge and wants to
know what the numbers mean — and whether to believe them. No code required. The
code-level companion is [reasoning.md](reasoning.md), which covers the *output*
side (caps, the thinking pad, the answer reserve). This page is the *input* side:
what fills the context window, and how letsClaw measures it.

Every figure in the audit at the bottom was measured against the real server
(SGLang, Qwen3.8-27B, `192.168.203.132:8888`) on 2026-10-19, not estimated.

---

## 1. Your mental model, corrected

You said:

> in every turn, the history + user prompt will be sent to the LLM and accounted
> as input token. And in letsClaw, the history = past history + digest.

The first half is **right**, with one large omission. The second half is **not
how it works here**. Three corrections:

**(a) It is not just history + your prompt.** Every request also carries the
*system message* (the behavior files) and the *tool schemas*. On this setup
that is a fixed ~1,900-token floor before you have said anything at all.

**(b) One turn is not one request.** This is the big one. A turn that uses tools
or resumes a cut-off thought makes **several** requests, and *each one re-sends
the whole prompt from scratch*. A 5-tool-call turn pays the input cost five or
six times over. The `⚡` line's `context` figure shows the window *size*, not
what the turn *cost* — those are very different numbers on a tool-heavy turn.

**(c) History is not "past history + digest".** A digest does not get appended
alongside the history. When it runs, it *replaces the middle of one turn* — the
tool calls and their output — while keeping your question and the final answer
verbatim. And it is **off by default**; your `config.yaml` has no `digest_turns`
key, so nothing in your sessions is being digested today.

---

## 2. The four kinds of token

| kind | also called | what it is | who pays |
|---|---|---|---|
| **Input** | prompt tokens | everything you send *up* — system message, tool schemas, whole conversation | charged on **every** request, in full |
| **Output** | completion tokens | everything the model generates | charged once per request |
| **Reasoning** | thinking tokens | the model's private `<think>` block | a **subset of output**, not a fifth category |
| **Cached** | `cached_tokens` | the slice of your input the server already had in memory | a **subset of input** — cheaper/faster, but still occupies the window |

Two traps worth naming, because both bite people:

**Reasoning is inside output, not beside it.** Measured on your server: a
reasoning reply reported `completion_tokens: 110` with `reasoning_tokens: 94`
and a 24-character answer. The 94 is *part of* the 110. letsClaw's code assumes
exactly this, and the assumption is correct.

**Cached does not mean free of context.** A cached token is one the server did
not have to re-process — it is a *speed and cost* win, not a *space* win. It
still sits in the window and still counts toward the limit. This matters because
"I have 2,048 cached tokens" never means "I have 2,048 tokens of room back".

---

## 3. What is actually sent, every request

```
┌─ system message ────────────── 1 message, always index 0 ─┐
│  models/base.md                      (shared by all)      │
│  + === BEHAVIOR ===  models/default.md   (per model)      │
│  + === SESSION ===   models/sessions/<name>.md (optional) │
│  + === CONTINUED SESSION ===  handoff    (after rollover)  │
├─ tool schemas ──── 4 tools, re-sent in full every round ──┤
├─ the conversation ────────────────────────────────────────┤
│  user / assistant / assistant+tool_calls / tool results   │
└─ your new question ───────────────────────────────────────┘
```

All of it, every time. The model is stateless; there is no server-side
conversation. Prefix caching makes re-sending *cheap*, not unnecessary.

Two things deliberately **never** enter the history, and so are never re-sent:

- **Reasoning.** Thrown away at the end of each turn. This is a deliberate and
  good decision — a 20k-token thought kept in history would cost 20k tokens on
  every later turn, forever.
- **The thinking pad.** Rides along on resume requests only, then is discarded.

### The round multiplier, concretely

A turn with 3 tool calls on a window holding 20k tokens of conversation:

| round | why | input sent |
|---|---|---|
| 1 | the question | ~22k |
| 2 | after tool 1 | ~22k + result |
| 3 | after tool 2 | ~22k + 2 results |
| 4 | after tool 3 | ~22k + 3 results |
| | **total input for one turn** | **~90k** |

The `⚡` line will report `context ~22k/262144` for that turn. That is honest —
it is the window size — but it is roughly a quarter of what the turn actually
cost in input. **letsClaw does not total per-turn input anywhere.** If you are
tracking spend rather than space, this is the gap.

---

## 4. Digest and rollover — the two ways history shrinks

**Digest** (`conversation.digest_turns`, **default false**, not set in your
config). At the end of a turn that used tools, the tool calls and their output
are replaced by a short summary; your question and the final answer stay
verbatim. It only runs if the full record reached the journal first — the
context is never the only copy. Gated to ~600 tokens max.

**Rollover** (`rollover_at_percent: 90`). At 90% of the window the whole
conversation is archived to disk, the model writes itself a four-section
handoff, and a fresh window opens containing: the system message (now with the
handoff appended), plus at most the last question/answer pair. Everything else
is on disk, reachable by grep through the journal.

There is also a **pre-turn gate** that rolls over *before* a turn when there
would be no room left to think in — this is why a session can roll at 80-something
percent rather than exactly 90.

---

## 5. How letsClaw counts: two paths

**The measured path (normal here).** Your SGLang server reports real usage, so
letsClaw reads `prompt_tokens` straight off the response. Verified: SGLang
reports the **full** prompt regardless of caching — a request with 2,048 cached
tokens still reported `prompt_tokens: 2058`. So the window gauge is reading the
truth, and prefix caching does not corrupt it.

At the end of a turn the window size is recorded as:

```
used = (the prompt the server just measured) + (our count of the answer) + 4
```

Deliberately **not** `prompt + completion`, because completion includes
reasoning, which is discarded. Using prompt+completion would inflate the figure
and could even make it *fall* as the conversation grew. This is a subtle call
and the code gets it right.

**The estimate path (fallback).** When a server withholds usage, letsClaw counts
locally with `tiktoken` (`cl100k_base`, a GPT encoding) and multiplies by
`ESTIMATE_SCALE = 1.6` to be safe. Section 6 is mostly about this number.

The estimate is also what you see **after** any operation that reshapes history
— a digest, a rollover, `/clear`, a history trim — because each correctly
discards the stale measurement. So even on your server, the estimate is on
screen more often than you would think.

---

## 6. The audit

### Verdict

| # | area | verdict |
|---|---|---|
| 1 | reading the server's `prompt_tokens` | ✅ correct, and cache-safe |
| 2 | `used = prompt + answer`, not prompt + completion | ✅ correct, and subtle |
| 3 | excluding the thinking pad from window size | ✅ correct |
| 4 | invalidating the measurement after reshaping history | ✅ correct, all 6 sites |
| 5 | `completion_tokens` includes reasoning | ✅ assumption verified |
| 6 | measured/estimated honesty flags exist at all | ✅ rare and good |
| 7 | **`ESTIMATE_SCALE = 1.6` is miscalibrated** | ❌ **~52% over at scale** |
| 8 | **tool schemas undercounted by 41%** | ❌ the real source of error |
| 9 | **cached tokens never read** | ⚠️ available, discarded |
| 10 | **per-turn input cost never totalled** | ⚠️ not measurable today |
| 11 | honesty flags inconsistent across clients | ⚠️ cosmetic but misleading |

### Finding 7 + 8 — the estimate is wrong in both directions at once

Measured against the real tokenizer, same messages, with and without tools:

| what | letsClaw's count | server's truth | truth ÷ estimate |
|---|---|---|---|
| conversation text only | 1,201 | 1,265 | **1.05** |
| the 4 tool schemas | 353 | 595 | **1.69** |
| both together | 1,554 | 1,860 | 1.20 |

So the tokenizer substitution is nearly harmless — `cl100k_base` is within **5%**
of Qwen's tokenizer on prose, much better than the "10-20%" the module header
claims. **Essentially all the error is in the tool schemas**, where
`json.dumps(specs)` misses the chat template's rendering scaffolding and comes in
**41% low** (the server's figure is 1.69× ours).

That matters because the schema error is a **fixed ~242 tokens**, while
`ESTIMATE_SCALE` is a **multiplier**. Correcting a fixed error with a multiplier
means the correction grows without bound as the conversation does:

| conversation | raw estimate | ×1.6 | truth | ×1.6 overshoots by |
|---|---|---|---|---|
| empty | 1,556 | 2,489 | 1,862 | +34% |
| 4 turns | 4,562 | 7,299 | 4,985 | +46% |
| 16 turns | 13,476 | 21,561 | 14,205 | **+52%** |
| 40 turns | 25,022 | 40,035 | 26,385 | **+52%** |

The scale was evidently calibrated on a near-empty window, where fixed template
overhead dominates and 1.6 looks defensible. By the time the window is full —
exactly when the number is load-bearing — the true ratio has settled at **1.05**
and 1.6 is overstating the window by half.

**What this costs, on a server without usage reporting:** rollover trips at 90%
of 262,144 = 235,929. With the 1.6 scale that fires when the raw count hits
147,455 — a window whose *true* size is about 155,000, or **59% full**. The
session archives itself and throws away a handoff's worth of continuity **31
percentage points early**, giving up roughly **80,000 usable tokens**.

The code comment says the stack is under-counted "by ~30%". Measured, it is 20%
on an empty window and 5% on a full one. The shape of the fix is to correct the
tool schemas where the error actually is (×1.69, or just measure it once at
startup) and drop the global multiplier to something near 1.1 — a scale of
1.6 is not conservative, it is wrong by more than the error it corrects.

> Scope note: this path is **inactive on your SGLang server**, which reports
> usage. It governs the fresh-window figure after `/clear`, after a rollover,
> after a digest, and any server that withholds usage.

### Finding 9 — the cached figure is on the wire and thrown away

Your server reports it. Measured, same prompt sent twice:

```
run 1:  prompt_tokens 2058,  prompt_tokens_details: null
run 2:  prompt_tokens 2058,  prompt_tokens_details: {"cached_tokens": 2048}
```

No code in the project reads `prompt_tokens_details`, `cached_tokens`, or any
equivalent — the fields are parsed off the response and dropped on the floor,
in both the OpenAI-compatible and the llama.cpp paths. This is not a correctness bug
(the window figure is right either way, see Finding 1), but it is the single
most useful diagnostic letsClaw is currently discarding: cache hit rate tells you
whether a config change just invalidated the server's prefix cache and quietly
made every turn slower.

Related: the server also reports its own `reasoning_tokens`, which letsClaw
ignores in favour of counting reasoning locally with tiktoken. The local count
is good enough and has the advantage of updating live during the stream — but
note the server's figure was *inconsistent* under truncation in testing
(`completion_tokens: 5` alongside `reasoning_tokens: 9`), so the local count may
well be the more trustworthy of the two. Not a defect.

### Finding 10 — per-turn input cost is not measurable

`total_output` is an odometer of everything generated. There is no counterpart
for input, and input is the larger number by far on a tool-heavy turn (section
3). Each round's `prompt_tokens` is already in hand — only the last one is kept.
Summing them into a per-turn `input_tokens` would be a small change and would
close the gap between "how full is the window" and "what did that turn cost".

### Finding 11 — the honesty flags disagree with each other

letsClaw marks estimated figures with `~`, which is genuinely better than most
tools do. But the convention is applied unevenly:

- Two flags for one concept with **opposite polarity**: the per-turn event sends
  `measured` (true = exact), `/info` sends `estimated` (true = guess).
- In the WebUI, the text slab tests `e.measured ? …` while the gauge on the very
  next line tests `measured === false` — so a missing flag prints `~` in the text
  and no `~` on the gauge, for the same number in the same frame.
- `/info`'s gauge hardcodes "estimated" while the text line two rows above
  honours the real flag — they can contradict each other on screen.
- `user_tokens`, `llm_tokens`, `reasoning_tokens` and `tools_tokens` are *always*
  local estimates and *never* carry a `~`, while sitting on the same line as a
  `context ~…` that does. The natural reading is that the unmarked ones are
  measured. They are not.
- `output_total` is a sum of measured and estimated turns by construction, and is
  never marked.
- Percentages floor in the Python clients and round in the browser, so the two
  can disagree by 1% on the same turn.

---

## 7. What to trust, in one table

| the number | trust it? |
|---|---|
| `context N/262144` **without** `~` | ✅ the server measured it |
| `context ~N/262144` **with** `~` | ⚠️ likely **~50% too high** on a full window (Finding 7) |
| `out N tok` without `~` | ✅ server-reported, includes reasoning |
| `out ~N tok` | ⚠️ local estimate, ~5% low |
| `reasoning N tok` | ✅ ~5% low, never marked but fine |
| `user N` / `LLM N` | ✅ ~5% low, never marked |
| `N tok of tool schemas` | ❌ **41% low** — real cost is ~1.69× this |
| `N this session` (output odometer) | ⚠️ mixed measured and estimated |
| what the turn **cost** in input | ❌ not reported anywhere |

**The short version:** the measured path — which is the one you are actually on —
is well built, and gets several subtle things right that are easy to get wrong.
The fallback estimate is miscalibrated in a way that gets worse as the window
fills, and the error is concentrated in one place (tool schemas) rather than
spread across the count. Fix the schemas and the global 1.6 multiplier stops
being necessary.
