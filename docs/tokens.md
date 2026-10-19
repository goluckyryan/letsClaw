# Tokens: what gets counted, and whether the counter is right

**Who this is for:** anyone who reads the `⚡` line or the WebUI gauge and wants to
know what the numbers mean — and whether to believe them. No code required. The
code-level companion is [reasoning.md](reasoning.md), which covers the *output*
side (caps, the thinking pad, the answer reserve). This page is the *input* side:
what fills the context window, and how letsClaw measures it.

Every figure in the audit at the bottom was measured against the real server
(SGLang, Qwen3.8-27B, `192.168.203.132:8888`) on 2026-10-19, not estimated.

---

## 1. The words this page uses precisely

Most confusion about token counts is really confusion about these words. They
are used strictly from here on.

| word | what it means here |
|---|---|
| **history** | the list of messages **letsClaw** keeps on its own side: the system message, then every question, answer, tool call and tool result. It lives in the core's memory and on disk. It is the only part letsClaw can edit, trim or collapse. |
| **prompt** | what is actually **sent to the model on one request**: the history, plus the tool schemas, rendered into one block of text. The model gets this and nothing else — it remembers nothing between requests. |
| **tool schema** | the machine-readable description of one tool — its name, what it is for, and the arguments it takes. The model's menu. See the box below; there are four of them here, costing **595 tokens** on every round that offers tools. |
| **window** | the model's hard ceiling on how big a prompt can be. Here, **262,144 tokens** (`context_length` in `config.yaml`; the clients call it `budget`). Fixed by the model, not by letsClaw. |
| **used** | how much of the window the current prompt would take. This is what the `⚡` line and the WebUI gauge show as `used/budget`. |
| **turn** | one question from you and the final answer back. |
| **round** | one request-and-reply with the model. **A turn can take several rounds** — one per tool call, plus any resumed thinking. This distinction is the single most important idea on this page. |
| **floor** | what `used` reads with **no conversation at all** — the system message plus the tool schemas. The starting tax: **1,859 tokens** here, paid on every round, which you can never get below. See the box below. |
| **tokenizer** | the thing that chops text into tokens. Every model has its own, with its own vocabulary, so *the same sentence is a different number of tokens to different models*. There is no universal token. |
| **tiktoken** | OpenAI's tokenizer library, and the one letsClaw counts with. It is **not Qwen's tokenizer** — the real one lives on the server. letsClaw uses `cl100k_base` (the GPT-3.5/GPT-4 vocabulary) as a stand-in, because it is a pip install and the right one is not. This substitution is the source of one of the two counting errors in section 8; it turns out to be the *smaller* one. |

One shorthand you will see below, now that the words are pinned down:
*"the window grew by 427"* is loose for *"`used` went up by 427"*. The window
itself never changes size; the occupancy does.

And one relationship worth stating plainly, because everything else follows
from it:

```
      history  =  system message  +  the whole conversation so far
      prompt   =  history  +  tool schemas          ← sent on EVERY round
      used     =  the size of that prompt, in tokens
      window   =  the ceiling it must stay under    (262,144)
```

The system message is part of the history — it is literally the first message in
the list — so it is never a separate thing to add on. Note what is *missing*
from that first line: the model's reasoning. Thinking is generated, shown, and
then **dropped**; it never joins the history and is never re-sent.

### What a tool schema actually looks like

`exec`, exactly as your core sends it:

```json
{
  "type": "function",
  "function": {
    "name": "exec",
    "description": "Run a shell command and return its exit code, stdout and stderr.",
    "parameters": {
      "type": "object",
      "properties": {
        "command": {"type": "string",  "description": "Shell command to run"},
        "workdir": {"type": "string",  "description": "Working directory (default: current dir)"},
        "timeout": {"type": "integer", "description": "Max seconds to wait (default: 1200)"}
      },
      "required": ["command"]
    }
  }
}
```

A name, a description telling the model when to reach for it, and the arguments
it must fill in — a function signature plus its documentation, in JSON.

**Why it is re-sent on every round:** the model has no built-in knowledge that
`exec` exists; it was never trained on your tools. The only way it can call one
is if that block is in front of it *right now*. Leave the schemas out of a
request and the model is blind to its tools for that round — which is exactly
what letsClaw does on purpose during a thinking-pad resume.

What the four cost:

| tool | our count | |
|---|---|---|
| `exec` | 119 tok | |
| `read_file` | 65 tok | |
| `write_file` | 89 tok | |
| `list_dir` | 75 tok | |
| sum of the JSON | ~353 tok | what letsClaw counts |
| **what the server charges** | **595 tok** | measured |

The gap is not a rounding error, and it is the root of Finding 8. The server
does not pass your JSON through — it **re-renders** the schemas into Qwen's own
chat-template format, wrapping them in section markers and instructions on how
to emit a tool call. None of that scaffolding is visible to a counter that only
looks at `json.dumps()`, so letsClaw sees 353 where the model sees 595.

### The floor: what an empty session already costs

Add the two always-present pieces together and you get the floor:

```
  system message (your behavior .md files) ....... 1,264
  tool schemas (the 4 tools above) ...............   595
  ───────────────────────────────────────────────────────
  the floor — what an empty session already uses . 1,859
```

A session with nothing in it already reads `1859/262144`. And `/clear` does not
clear it: wiping the conversation returns you *to* the floor, not to zero,
because the behavior files and schemas reload immediately.

#### The floor is not a constant

It is easy to read the number above as a property of the install. It is not. It
is a property of **this session, right now**, and it is better thought of as a
step function — flat for long stretches, with a jump at each of the events
below.

Start with the fact that the "system message" is not one file. It is three
layers concatenated, plus the handoff:

```
  base.md  →  the model entry's behavior_file  →  models/sessions/<name>.md  →  carryover
```

The third layer is named for the session, so **two sessions on the same core
have different floors** if either has a file of its own. For Discord that means
one per channel.

Four things move it:

| what happens | what it does to the floor |
|---|---|
| **a rollover** | **raises** it — the handoff joins the system message, up to ~1,500 tokens. It does **not** stack: each handoff is written from the previous one and **replaces** it, so the floor steps up once and then stays roughly flat however many times you roll over. |
| **`/model`** | **changes** it — `use_model()` rebuilds the system message, and the middle layer *is* the model's own behavior file. A different model is a different layer 2. |
| **`strip_directives`** on a model entry | **lowers** it — `strip_directive_sections()` cuts sections out of base.md for models that do not need them. |
| **`/reload`** | **changes the schema half only.** See below. |

**The reload asymmetry is worth knowing.** A reload updates `tools_tok` on every
live session immediately, but deliberately leaves the system message alone — so
a session that was running when you reloaded is carrying **new schemas and an
old system message**, and will be until its next rollover, `/clear` or `/new`.

That is a deliberate trade, not an oversight: the system message is the prompt
prefix the server has already tokenised, and rewriting it mid-conversation
throws away the KV cache for every live session at once, to deliver a change
the session did not ask for at a moment it did not choose. An edited `base.md`
therefore waits for the next rebuild — each of which re-reads the files anyway.

What *is* fixed: nothing in the system message is computed when it is built. No
timestamp, no session id, no model name is interpolated — it is the content of
those files and nothing else, so the same files always give the same number.
The floor is constant *per rebuild of the system message*; the four rows above
are the four ways to trigger a rebuild.

How much the floor matters is entirely a function of the window it sits in:

| model | window | the floor is |
|---|---|---|
| `qwen38-local` (the default) | 262,144 | 0.71% |
| `qwen38-flash` | 524,288 | 0.35% |
| Argo Opus | 1,000,000 | 0.19% |
| **a 12k window** | **12,000** | **15.5%** |

On the big windows it is a rounding error. On a 12k window a sixth of the
context is gone before anyone says hello — which is why trimming behavior files
or turning tools off is a real lever there and a waste of effort here.

---

## 2. The same thing, in algebra

Everything above is one recurrence relation. If you would rather read it that
way, here it is — stated, then checked against the live server's own figures
before it is used for anything.

### Symbols

| | |
|---|---|
| $S$ | tokens in the system message (your behavior files) |
| $T$ | tokens the server **charges** for the tool schemas — **595** |
| $\tau$ | tokens letsClaw **counts** for those schemas — **353**. Not a typo; see below |
| $N$ | how many tools are offered — **4** here |
| $h(n)$ | true size of the history after turn $n$ |
| $m(n)$ | letsClaw's `tiktoken` count of that same history |
| $q(n),\ a(n)$ | the question and the final answer at turn $n$ |
| $r(n)$ | how many **rounds** turn $n$ took |
| $c(n,k),\ t(n,k)$ | the $k$-th tool call and its result, in turn $n$ |

#### Why $T$ and $\tau$ are different numbers for the same thing

Because they are not the same thing. $\tau$ is what letsClaw computes:

```python
count_tools(specs)  ==  count_text(json.dumps(specs))
```

— the tool specs dumped to a JSON string, and that string's length in tokens.
$T$ is what the server bills. They differ for **two independent reasons**:

1. **letsClaw is counting the wrong text.** `json.dumps()` produces a bare JSON
   array. The server never sends that. It feeds the specs through the model's
   *chat template*, which rewrites them into the form Qwen was trained to read —
   wrapped in a block, preceded by instructions on how to emit a call. That
   rendered text is longer than the JSON, and `json.dumps()` cannot see one
   character of the difference.
2. **letsClaw is counting with the wrong tokenizer** — `tiktoken`, not Qwen's.
   Even on identical text the two disagree.

Sending the four tools one at a time and watching `prompt_tokens` separates
them, and the structure is sharper than a single ratio suggests:

| tools sent | $\tau$ | server charges | difference |
|---|---|---|---|
| none at all | 0 | **0** | — |
| `exec` | 121 | 330 | +209 |
| `+ read_file` | 187 | 407 | +220 |
| `+ write_file` | 277 | 508 | +231 |
| `+ list_dir` | 353 | **595** | +242 |

The difference is not proportional — it starts at +209 for *one* tool and
creeps up by only ~11 per tool after that. Fitting those gives

$$T \;=\; \underbrace{198}_{\text{fixed block}} \;+\; \tau \;+\; \underbrace{11\,N}_{\text{per tool}}$$

which reproduces every subset tested — any single tool, pairs, and both
orderings of all four — to **within 2 tokens**. (Order does not matter; an
empty tool list costs exactly 0, so the fixed block appears only once at least
one tool is present.)

**So the ratio $T/\tau = 1.69$ is not a property of the tokenizer or the
template. It is an artifact of having four smallish tools.** That 198-token
block is a fixed cost amortised over whatever you offer: with one tool the
ratio is 2.7, and as tools are added it falls toward

$$\frac{T}{\tau} \;\longrightarrow\; 1 + \frac{11}{\bar s} \;\approx\; 1.13
\qquad (\bar s = \text{average tool size, } \approx 88 \text{ tokens here})$$

This matters for the fix in section 8: **anything that hardcodes 1.69 is
calibrated to today's tool list and silently wrong after you add a tool.**

### The history

The system message is the whole of the history before anyone speaks:

$$h(0) = S$$

and each turn appends its question, every tool call and result it made along the
way, and the final answer:

$$h(n) = h(n-1) \;+\; q(n) \;+\; \sum_{k=1}^{r(n)-1}\big[c(n,k) + t(n,k)\big] \;+\; a(n)$$

Note what never appears on the right-hand side: **reasoning**. It is generated
and discarded, so it contributes nothing to $h$ at any $n$.

### What is used, and what is sent

`used` is the history plus the schemas — the size of the next prompt:

$$U(n) = h(n) + T, \qquad\text{so}\qquad U(0) = S + T \equiv F \ \ (\text{the floor})$$

Strictly both $S$ and $T$ are themselves functions of $n$ — a rollover, a
`/model` or a `/reload` steps them, as section 1 sets out. They are written
here without the index because they are **piecewise constant**, and every
recurrence below holds within a piece. The jumps are the interesting part and
they are handled explicitly at the end of this section.

Within turn $n$, the prompt on round $k$ starts from last turn's window and
grows by each tool exchange:

$$P(n,1) = U(n-1) + q(n), \qquad P(n,k) = P(n,k-1) + c(n,k-1) + t(n,k-1)$$

### The bill

The input billed for turn $n$ is the sum over its rounds — **not** $U(n)$:

$$I(n) = \sum_{k=1}^{r(n)} P(n,k)$$

For a plain turn, $r = 1$ and the two nearly coincide:

$$I(n) = U(n-1) + q(n)$$

For a tool turn, so long as the tool traffic is small next to the window:

$$I(n) \;\approx\; r(n)\cdot U(n-1)$$

**This is the round multiplier, stated exactly.** A turn costs its window
*times its round count*.

### Checked against the server

None of the above is worth anything if it does not reproduce reality. Taking
$F = 1859$ and the measured questions, tool exchanges and answers from the
session in section 3, the recurrence predicts every prompt the server actually
received:

| quantity | by the formula | the server said |
|---|---|---|
| $U(0) = S + T$ | $1264 + 595$ | **1,859** ✓ |
| $P(1,1) = U(0) + q(1)$ | $1859 + 99$ | **1,958** ✓ |
| $P(2,1) = U(1) + q(2)$ | $2286 + 97$ | **2,383** ✓ |
| $P(2,2) = P(2,1) + c + t$ | $2383 + 283$ | **2,666** ✓ |
| $P(2,3) = P(2,2) + c + t$ | $2666 + 680$ | **3,346** ✓ |
| $I(2) = \sum_k P(2,k)$ | $2383 + 2666 + 3346$ | **8,395** ✓ |
| $P(19,1) = U(18) + q(19)$ | $10713 + 107$ | **10,820** ✓ |

Exact, not approximate — every one. The arithmetic of the context window really
is this simple; what is *not* simple is the counter's estimate of it, below.

### Why long sessions hurt: it is quadratic

Each turn adds roughly a constant $\delta$ to the window, so $U(n) \approx F + n\delta$.
Summing the bill over a session of $N$ turns at a typical $\bar r$ rounds each:

$$I_{\text{total}} \;=\; \sum_{n=1}^{N} I(n) \;\approx\; \bar r\left(N F + \frac{\delta N^{2}}{2}\right) \;=\; \mathcal{O}(N^{2})$$

**The cost of a session grows with the square of its length.** Doubling a
conversation quadruples what it cost to produce. This is the real reason
rollover and digests exist — both attack the $\delta N^2/2$ term by resetting
$h$, and that is worth far more than any saving on a single turn.

### What a digest and a rollover do to the recurrence

A digest replaces one turn's tool traffic with a summary $D(n) \le 600$:

$$h(n) = h(n-1) + q(n) + a(n) + D(n)$$

saving $\sum_k[c+t] - D(n)$ — large on a tool-heavy turn, nothing on a plain one.

A rollover at turn $R$ discards $h$ outright and restarts from a new floor,
carrying a handoff $\Lambda \lesssim 1500$ and the last exchange:

$$h(R) = \underbrace{S + \Lambda}_{\text{new system message}} + \; q(R) + a(R)$$

$\Lambda$ does not accumulate across rollovers — each handoff is written from
the previous one and **replaces** it — so after the $j$-th rollover the floor is

$$F_j = S + \Lambda_j \ + \ T, \qquad \Lambda_j \lesssim 1500 \ \text{ for every } j$$

not $S + \sum_j \Lambda_j$. The floor steps up once and then holds.

### The floor as a step function

Collecting the jumps from section 1, write $F(n) = S(n) + T(n)$. Both terms are
piecewise constant, and they step on **different** events:

| event at turn $n$ | $S$ | $T$ |
|---|---|---|
| rollover | $S + \Lambda_j$ | — |
| `/model` | new layer 2 | — |
| `/clear`, `/new` | re-read from disk | — |
| `/reload` | **unchanged until the next rebuild** | **steps immediately** |

The last row is the only one where the two halves move apart, and it is the
only case where $F$ changes without $h$ being rebuilt. Everywhere else a step
in $F$ coincides with a reset of $h$, which is why the recurrences above can
treat $F$ as a constant: each piece is exactly one window, and a new window
starts a new piece.

### The counter, and where it goes wrong

letsClaw's estimate, when the server withholds usage, is a single scale over
the lot:

$$\hat{U}(n) = 1.6\,\big(m(n) + \tau\big)$$

But the measured truth has **two different errors of different shapes** — the
prose term is off by a *factor*, the schema term by a *fixed amount*:

$$U(n) \;\approx\; \underbrace{1.05\,m(n)}_{\text{scaled}} \;+\; \underbrace{198 + \tau + 11N}_{\text{offset, from the fit above}}$$

A single multiplier cannot correct both. That is the whole bug, in one line.

Dividing one by the other gives the overshoot, and its limit is the whole bug:

$$\frac{\hat{U}(n)}{U(n)} \;=\; \frac{1.6\,(m + \tau)}{1.05\,m + T} \;\xrightarrow[\;m \to \infty\;]{} \; \frac{1.6}{1.05} \;=\; \mathbf{1.52}$$

At the floor $m \approx 1200$ it evaluates to 1.34; by $m \approx 10^4$ it has
reached 1.49 and is still climbing. Measured: **+34%** and **+47%**, and **+52%**
at $m \approx 2.5\times10^4$. The law predicts all three, which is how we know
the error is understood rather than merely tabulated.

**The fix follows directly:** use the two-term form. Across nine measured
checkpoints from an empty window to 26,000 tokens it holds to **±1.7%**, against
+34% to +52% for the scale in the code today — and it needs no safety multiplier,
because it is no longer wrong.

---

## 3. A worked example: one session, turn by turn

Rather than describe it, here is a real session, turn by turn. Every number
below was measured against the live server — a real request was sent and the
server's own `prompt_tokens` recorded. Nothing here is calculated or guessed.

### Turn 0 — the floor, before anyone has said anything

An empty session is not empty.

```
  system message only (base.md + default.md) ....... 1,264 tok
  + the 4 tool schemas ..............................  595 tok
  ────────────────────────────────────────────────────────────
  the window, with no conversation in it ........... 1,859 tok
```

**Lesson 1: it is not just "history + your prompt".** Every request also carries
the system message (your behavior files) and the full tool schemas. That ~1,859
is the floor — you pay it on every single request for the life of the session.

### Turn 1 — a plain question

```
  round 1 prompt  = floor + your question ........... 1,958 tok   ← INPUT billed
  ... model answers ...
  window afterwards ................................. 2,286 tok
```

> rounds: **1** · turn cost: **1,958 input** · window grew: **+427**

Here your mental model is exactly right: one turn, one request, input = the
whole window. Note the answer is *not* input yet — it becomes input next turn.

### Turn 2 — the same kind of question, but it uses two tools

```
  round 1  floor + history + question ............... 2,383 tok
  → model asks to run `exec`
  round 2  ...everything above + the tool result .... 2,666 tok
  → model asks to run `read`
  round 3  ...everything above + that result ........ 3,346 tok
  → model finally answers
  window afterwards ................................. 3,651 tok
```

> rounds: **3** · turn cost: **8,395 input** · window grew: **+1,365**

**Lesson 2 — the big one: one turn is not one request.** This turn cost **8,395
input tokens** while the window only grew by 1,365. Every round re-sends the
*entire* prompt from scratch; the model is stateless and there is no server-side
conversation. A 5-tool turn pays six times over.

The `⚡` line will report `context 3651/262144` for this turn. That is honest —
it is the window *size* — but it is **2.3× smaller than what the turn actually
cost**. letsClaw reports no per-turn input figure anywhere (Finding 10).

### ...16 more ordinary turns... → turn *n*

```
  window after turn 18 ............................. 10,713 tok
```

### Turn *n+1* — one more plain turn, on the bigger window

```
  round 1 prompt .................................. 10,820 tok   ← INPUT billed
  window afterwards ............................... 11,110 tok
```

> rounds: **1** · turn cost: **10,820 input** · window grew: **+397**

**Lesson 3: the cost of a turn is set by the conversation so far, not by the
turn.** Turn 1 and turn *n+1* asked an identically-sized question and grew the
window by about the same amount (427 vs 397) — but turn *n+1* cost **5.5× more
input**, because it had to carry 18 turns of history up with it. This is why
long sessions get expensive and slow even when you are not doing much: *the
whole conversation is re-sent, every round, forever.*

### What the counter said at each of those checkpoints

The same trace, with letsClaw's own estimator beside the truth:

| window after turn | truth | raw estimate | ×1.6 (what it shows) | overshoot |
|---|---|---|---|---|
| 0 — empty window | 1,859 | 1,553 | 2,484 | +34% |
| 1 — after a plain Q&A | 2,286 | 1,952 | 3,123 | +37% |
| 2 — after a 2-tool turn | 3,651 | 3,219 | 5,150 | +41% |
| 18 — "turn *n*" | 10,713 | 9,815 | 15,704 | **+47%** |
| 19 — "turn *n+1*" | 11,110 | 10,195 | 16,312 | **+47%** |

**Lesson 4: the estimate drifts the wrong way.** The raw count is consistently a
*little* low (5-17%), but the ×1.6 safety factor overcorrects — and the
overshoot *grows* with the conversation, exactly where being wrong is expensive.
Section 8 takes this apart.

> This estimate only appears when the server withholds usage, or just after a
> `/clear`, a rollover or a digest. On your SGLang server the normal figure is
> the measured one.

### One more correction: history is *not* "past history + digest"

A digest is not appended alongside the history. When it runs, it **replaces the
middle of one turn** — in turn 2 above, it would collapse the two tool calls and
their results into one ~200-token summary, while keeping your question and the
final answer verbatim. So that turn's 1,365-token footprint might shrink to
~700, with the full record still on disk in the journal.

It is also **off by default** — your `config.yaml` has no `digest_turns` key, so
nothing in your sessions is being digested today, and the trace above is what
you are actually paying.

---

## 4. The four kinds of token

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

## 5. What is actually sent, every request

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

### Which rounds carry what

Turn 2 of the worked example is the general case: **every** round re-sends the
whole prompt. Not every round carries the tool schemas, though, which is the one
part of the prompt that varies:

| round type | schemas sent? |
|---|---|
| an ordinary round in the tool loop | yes |
| the final round when the window is nearly full | no |
| a thinking-pad resume | no — tools are never offered mid-thought |
| a forced conclusion / checkpoint round | no |

Rounds that skip the schemas are ~595 tokens cheaper here. None of this is
visible in any UI figure: the `⚡` line reports the window *size* once per turn,
not the sum of the prompts the turn actually sent (Finding 10).

---

## 6. Digest and rollover — the two ways history shrinks

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

## 7. How letsClaw counts: two paths

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
`ESTIMATE_SCALE = 1.6` to be safe. Section 8 is mostly about this number.

The estimate is also what you see **after** any operation that reshapes history
— a digest, a rollover, `/clear`, a history trim — because each correctly
discards the stale measurement. So even on your server, the estimate is on
screen more often than you would think.

---

## 8. The audit

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

Read that 1.69 as a measurement of *this install*, not a constant: section 2
breaks the gap down tool by tool and finds it is $198 + 11N$ — a fixed block
plus a small per-tool cost — so the ratio depends on how many tools you offer
and falls toward 1.13 as you add them.

That is exactly what makes it dangerous here: the schema error is a **fixed
~242 tokens**, while `ESTIMATE_SCALE` is a **multiplier**. Correcting a fixed
error with a multiplier means the correction grows without bound as the
conversation does.

A second, controlled run — pure prose turns, carried further than the worked
example in section 3, which showed the same curve reaching +47%:

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
on an empty window and 5% on a full one.

**The fix, fitted rather than guessed.** One scale cannot correct two errors of
different shapes, so use two — one per term, as derived in section 2:

$$\hat{U}(n) \;=\; 1.05\,m(n) \;+\; T \qquad\text{in place of}\qquad 1.6\,\big(m(n) + \tau\big)$$

where $T$ is the schemas' **real** cost — ideally measured once at startup
(it is constant for the life of the process, and the server reports it on the
first request), or failing that $T \approx 198 + \tau + 11N$ from section 2.
**Do not use $T = 1.69\,\tau$:** that ratio is an artifact of this particular
four-tool list and drifts toward 1.13 as tools are added.

Checked against every measurement on this page — both runs, nine points, an empty
window through 26,000 tokens:

| point | letsClaw's raw $m(n)$ | truth | ×1.6 today | two-term | today's error | two-term error |
|---|---|---|---|---|---|---|
| floor | 1,200 | 1,859 | 2,485 | 1,857 | +34% | −0.1% |
| turn 1 | 1,599 | 2,286 | 3,123 | 2,276 | +37% | −0.5% |
| turn 2 | 2,866 | 3,651 | 5,150 | 3,606 | +41% | −1.2% |
| turn 18 | 9,462 | 10,713 | 15,704 | 10,532 | +47% | **−1.7%** |
| 4 turns | 4,209 | 4,985 | 7,299 | 5,016 | +46% | +0.6% |
| 16 turns | 13,123 | 14,205 | 21,562 | 14,376 | +52% | +1.2% |
| 40 turns | 24,669 | 26,385 | 40,035 | 26,499 | +52% | +0.4% |

**±1.7% worst case, against +34% to +52%.** (The two-term column uses $T = 595$,
this install's measured value.)

The deeper point is about *shape*, not calibration. Section 2 shows the schema
error is $198 + 11N$ — **a fixed block plus a per-tool constant**, not a
percentage. Correcting a fixed error with a multiplier is why 1.6 drifts: the
correction grows with the conversation while the error it corrects does not.
Re-tuning the multiplier would not fix that; only changing the form does.

A scale of 1.6 is not conservative. It is wrong by more than the error it was
added to correct, and wrong in a direction that gets worse exactly as the
number starts to matter.

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

## 9. What to trust, in one table

| the number | trust it? |
|---|---|
| `context N/262144` **without** `~` | ✅ the server measured it |
| `context ~N/262144` **with** `~` | ⚠️ likely **~50% too high** on a full window (Finding 7) |
| `out N tok` without `~` | ✅ server-reported, includes reasoning |
| `out ~N tok` | ⚠️ local estimate, ~5% low |
| `reasoning N tok` | ✅ ~5% low, never marked but fine |
| `user N` / `LLM N` | ✅ ~5% low, never marked |
| `N tok of tool schemas` | ❌ **41% low** — real cost here is 595, and the gap is a fixed ~198 + ~11/tool, not a percentage |
| `N this session` (output odometer) | ⚠️ mixed measured and estimated |
| what the turn **cost** in input | ❌ not reported anywhere |

**The short version:** the measured path — which is the one you are actually on —
is well built, and gets several subtle things right that are easy to get wrong.
The fallback estimate is miscalibrated in a way that gets worse as the window
fills, and the error is concentrated in one place (tool schemas) rather than
spread across the count. Fix the schemas and the global 1.6 multiplier stops
being necessary.
