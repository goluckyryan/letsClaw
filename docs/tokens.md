# Tokens: what gets counted, and how accurately

**Who this is for:** anyone who reads the `⚡` line or the WebUI gauge and wants to
know what the numbers mean — and whether to believe them. No code required. The
code-level companion is [reasoning.md](reasoning.md), which covers the *output*
side (caps, the thinking pad, the answer reserve). This page is the *input* side:
what fills the context window, and how letsClaw measures it.

Every figure on this page was measured against the real server (SGLang,
Qwen3.8-27B, `192.168.203.132:8888`) on 2026-10-19, not estimated. This page
describes what the code does **now**; it is not a changelog, and where a number
here disagrees with the running system, the system is right and the page is
stale.

---

## 1. The words this page uses precisely

Most confusion about token counts is really confusion about these words. They
are used strictly from here on.

| word | what it means here |
|---|---|
| **history** | the list of messages **letsClaw** keeps on its own side: the system message, then every question, answer, tool call and tool result. It lives in the core's memory and on disk. It is the only part letsClaw can edit, trim or collapse. |
| **prompt** | what is actually **sent to the model on one request**: the history, plus the tool schemas, rendered into one block of text. The model gets this and nothing else — it remembers nothing between requests. |
| **tool schema** | the machine-readable description of one tool — its name, what it is for, and the arguments it takes. The model's menu. See the box below; there are ten of them here, costing **1,530 tokens** locally on every round that offers tools (more at the server — see the box below). |
| **window** | the model's hard ceiling on how big a prompt can be. Here, **262,144 tokens** (`context_length` in `config.yaml`; the clients call it `budget`). Fixed by the model, not by letsClaw. |
| **used** | how much of the window the current prompt would take. This is what the `⚡` line and the WebUI gauge show as `used/budget`. |
| **turn** | one question from you and the final answer back. |
| **round** | one request-and-reply with the model. **A turn can take several rounds** — one per tool call, plus any resumed thinking. This distinction is the single most important idea on this page. |
| **floor** | what `used` reads with **no conversation at all** — the system message plus the tool schemas. The starting tax: **1,979 tokens** here, paid on every round, which you can never get below. See the box below. |
| **bill** | how many input tokens a turn, or a whole session, actually **sent**. Not the same as `used`: `used` is the window measured once, while every round of a turn re-sends the whole of it. The bill is the larger number, always, and on a tool-heavy turn it is several times larger. `/info` shows the session's as `📥 Input`. |
| **cache hit rate** | the share of the bill the server answered out of its KV cache instead of re-reading. A **cost and speed** figure, never a space figure — see section 4. `/info` shows it as `💾 Cache`, and omits the line when the server reports nothing. |
| **tokenizer** | the thing that chops text into tokens. Every model has its own, with its own vocabulary, so *the same sentence is a different number of tokens to different models*. There is no universal token. |
| **tiktoken** | OpenAI's tokenizer library, and the one letsClaw counts with. It is **not Qwen's tokenizer** — the real one lives on the server. letsClaw uses `cl100k_base` (the GPT-3.5/GPT-4 vocabulary) as a stand-in, because it is a pip install and the right one is not. It is one of the three corrections section 8 applies, and the smallest of them. |

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
        "timeout": {"type": "integer", "description": "Max seconds to wait (default: 1200)"},
        "reason":  {"type": "string",  "description": "Why, in one short phrase"}
      },
      "required": ["command", "reason"]
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

What the ten cost:

| tool | our count | of which `reason` |
|---|---|---|
| `exec` | 141 tok | 23 |
| `read_file` | 88 tok | 23 |
| `write_file` | 112 tok | 23 |
| `list_dir` | 97 tok | 22 |
| `show_image` | 132 tok | 23 |
| `search_and_inspect` | 262 tok | 22 |
| `inspect_file` | 183 tok | 23 |
| `inspect_symbol` | 169 tok | 23 |
| `edit_file` | 190 tok | 23 |
| `run_and_capture` | 156 tok | 23 |
| sum of the JSON | 1,530 tok | what letsClaw counts |
| **what the server charges** | **not re-measured** | was 715 at 462 local, five tools |

**`reason` is the other one worth watching.** Every tool carries it
(`tools.py:REASON_KEY`) and no handler ever receives it: it exists so a client
that will not show the command can still say why the call was made, which is
what the Discord bot does with it. The field is four words of description and
one `required` entry, and it still costs **228 tokens on every request of every
tool-enabled session** — 1,302 → 1,530 — because it is paid once per tool,
ten times over. Spelling out what a good reason looks like in the field itself
would pay that fraction per tool again; `models/base.md` carries the sentence
once instead. That is the whole lesson of this page in one field: the per-tool
slot is the expensive place to explain anything, and the system message,
sent once, is the cheap one.

The server figure below has **not** been re-measured since. 715 was the charge
when the local count was 462, for five tools; the re-render scales with the
JSON, so at 1,530 local for ten tools expect roughly 2,350–2,450, but do not
quote it until someone diffs `prompt_tokens` for real. Measured numbers and
extrapolated ones do not go in the same table.

`show_image` is the newest and the one worth watching, because it is the one
whose schema was written after this page existed. It adds **121 tokens to every
request of every tool-enabled session, forever** — measured by diffing
`prompt_tokens` for the same prompt with and without it. That is why its
description is one sentence: four tools cost 594 at the server, five cost 715
— both measured before `reason` existed — and the gap is paid on every round
whether a picture is wanted or not. Drop it from `tools.allow` and you get
those 121 back.

The gap is not a rounding error, and it is why the schema term in section 8
is measured from the server rather than counted locally. The server does not
pass your JSON through — it **re-renders** the schemas into Qwen's own
chat-template format, wrapping them in section markers and instructions on how
to emit a tool call. None of that scaffolding is visible to a counter that only
looks at `json.dumps()`, so letsClaw saw 462 where the model saw 715. Those
two are the pre-`reason` pair, and they are quoted together on purpose: the
ratio is the useful part, and it is the only part that survives a change to
the schemas.

### The floor: what an empty session already costs

Add the two always-present pieces together and you get the floor:

```
  system message (your behavior .md files) ....... 1,264
  tool schemas (the 10 tools above) ..............   715   ← pre-`reason`, five of them
  ───────────────────────────────────────────────────────
  the floor — what an empty session already uses . 1,979
```

Both figures are server-measured and **both are out of date** — they predate
`reason` and the five search and edit tools added since. The local counts now
stand at 1,302 → 1,530 for the schemas and 1,953 for the behavior files
(`base.md` 1,753 plus `default.md` 200, counted with
`token_counter.count_text`); the floor has risen by whatever the pair
re-renders to. The arithmetic above is kept as it was rather than quietly
patched with a guess — a floor is the one number on this page that is worth
nothing if it is estimated. Re-measure it the way section 8 says to, by
reading `prompt_tokens` off an empty session, and replace both lines at once.

A session with nothing in it read `1979/262144` when that was measured. And `/clear` does not
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
| **the `# Directives` group in base.md** | **does not raise it** — `strip_directive_sections()` cuts the group out of the base layer for every model, so the two directive templates cost nothing in the prompt and are billed only on the turn the core actually sends one. |
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
| $T$ | tokens the server **charges** for the tool schemas — **715** |
| $\tau$ | tokens letsClaw **counts** for those schemas — **462**. Not a typo; see below |
| $N$ | how many tools are offered — **5** here |
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

Sending the tools one at a time and watching `prompt_tokens` separates
them, and the structure is sharper than a single ratio suggests:

| tools sent | $\tau$ | server charges | difference |
|---|---|---|---|
| none at all | 0 | **0** | — |
| `exec` | 120 | 329 | +209 |
| `+ read_file` | 186 | 406 | +220 |
| `+ write_file` | 276 | 507 | +231 |
| `+ list_dir` | 352 | 594 | +242 |
| `+ show_image` | 462 | **715** | +253 |

The difference is not proportional — it starts at +209 for *one* tool and
creeps up by only ~11 per tool after that. Fitting those gives

$$T \;=\; \underbrace{198}_{\text{fixed block}} \;+\; \tau \;+\; \underbrace{11\,N}_{\text{per tool}}$$

which reproduces every subset tested — any single tool, pairs, and both
orderings of all four — to **within 2 tokens**. (Order does not matter; an
empty tool list costs exactly 0, so the fixed block appears only once at least
one tool is present.)

The last row is the fit's best evidence: `show_image` was added long after
those constants were measured, and the formula called its 715 **exactly**,
with nothing refitted. A fifth tool was never part of the data, so this is a
prediction the server confirmed rather than a curve drawn through its own
points. The earlier rows moved by 1 since they were first taken — `exec`'s
schema quotes the `exec_timeout` default, which changed from 1200 to 120.

**So the ratio $T/\tau = 1.69$ is not a property of the tokenizer or the
template. It is an artifact of having four smallish tools.** That 198-token
block is a fixed cost amortised over whatever you offer: with one tool the
ratio is 2.7, and as tools are added it falls toward

$$\frac{T}{\tau} \;\longrightarrow\; 1 + \frac{11}{\bar s} \;\approx\; 1.13
\qquad (\bar s = \text{average tool size, } \approx 88 \text{ tokens here})$$

This matters in section 8: **anything that hardcodes 1.69 is calibrated to
today's tool list and silently wrong after you add a tool.**

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

$I(n)$ is not only algebra: each round's server-reported `prompt_tokens` is
summed over the turn and rides the `stats` event as `input_tokens`. It
is not rendered on the `⚡` line — the choice was a session total, section 8 —
but the per-turn figure is on the wire for anything that wants it,
and it is measured, never estimated. A round whose server withheld usage
contributes nothing rather than a guess, and the event's `input_measured` flag
says whether every round of the turn was counted.

### Checked against the server

None of the above is worth anything if it does not reproduce reality. Taking
$F = 1859$ and the measured questions, tool exchanges and answers from the
session in section 3, the recurrence predicts every prompt the server actually
received:

> **These are the figures from that session, not from today's install.** It ran
> on the four-tool configuration, so its floor was $1264 + 594$, not the
> $1264 + 715$ a fifth schema makes it now. They are left as they were measured
> rather than shifted by 121 — a recorded run that is adjusted to match a later
> config is no longer evidence of anything. What the recurrence claims is that
> it reproduces the server *whatever* $F$ is, and the only way to show that is
> to let a real $F$ stand with its own numbers.

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

$I_{\text{total}}$ is a number you can read. It is exactly what the session
odometer `total_input` accumulates, printed by `/info` as `📥 Input`, so the
$\mathcal{O}(N^2)$ claim above is **observed, not just derived** — watch
the gauge crawl while that line sprints. It is also the only place the cost of
a misfiring rollover shows up: a window rolled at half full still bills every
round it took to get there, and `used` resets while the odometer does not.

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

### The counter, and what it has to correct

When the server withholds usage, letsClaw has to estimate the window from its
own local count. That count differs from the server's in **three ways, and the
three have three different shapes**:

| the error | why it exists | shape | grows with |
|---|---|---|---|
| per **token** | cl100k is not the server's tokenizer | a ratio $\alpha$ | how much text there is |
| per **message** | the chat template wraps every message in role markers and separators | a constant $\beta$ each | how many messages there are |
| per **prompt** | the schemas are *rendered into* the template, not dumped as JSON | a fixed block $T$ | nothing at all |

Saying that needs two symbols $m(n)$ cannot provide, because $m(n)$ already has
a guess baked into it — the naive count charges 4 tokens of framing per message
and 3 at the end:

| | |
|---|---|
| $\mu(n)$ | the cl100k count of the message **contents** alone, with no framing |
| $\nu(n)$ | how many messages are in the history |

so that $m(n) = \mu(n) + 4\nu(n) + 3$. Then the truth is

$$U(n) \;\approx\; \underbrace{\alpha\,\mu(n)}_{\text{per token}} \;+\; \underbrace{\beta\,\nu(n)}_{\text{per message}} \;+\; \underbrace{T}_{\text{per prompt}}$$

and a least-squares fit of $\alpha$ and $\beta$ over nine measured windows
spanning 1,800 to 17,000 tokens gives

$$\alpha = 1.0510, \qquad \beta = 10.79, \qquad \text{worst residual } \mathbf{0.60\%}$$

**A single multiplier cannot correct three errors of three different shapes** —
scale the lot by one number and the per-message and per-prompt terms are scaled
too, so the result is a straight line fitted to a curve and the only question is
where it crosses. So the code uses three constants, each multiplying the term
whose error it actually describes:

$$\hat{U}(n) \;=\; \text{PROSE\_SCALE}\cdot\mu(n) \;+\; \text{MESSAGE\_FRAMING\_TOKENS}\cdot\nu(n) \;+\; T$$

with `PROSE_SCALE` = 1.05 and `MESSAGE_FRAMING_TOKENS` = 11 — the fitted 10.79,
rounded **up**, because an estimate that reads slightly high rolls the window
slightly early, and that is the cheap direction to be wrong in. $T$ is measured
from the server wherever it reports a prompt size, and falls back to
$198 + \tau + 11N$ — `SCHEMA_BASE_TOKENS` + $\tau$ + `SCHEMA_PER_TOOL_TOKENS`
$\times N$ — where it does not.

Across the same nine checkpoints this lands within **0.5%**. Section 8 has the
figure at every point, and the alternative forms that were considered and
rejected.

---

## 3. A worked example: one session, turn by turn

Rather than describe it, here is a real session, turn by turn. Every number
below was measured against the live server — a real request was sent and the
server's own `prompt_tokens` recorded. Nothing here is calculated or guessed.

It ran when `tools.allow` held four tools, so its floor is 1,859 rather than
the 1,979 a fifth schema makes it today. Every figure in this section is 120
tokens below what the same session would cost now, and they are left that way
on purpose: these are measurements, and a measurement edited to match a later
config stops being one. The shapes are what the section is for — the floor,
the round multiplier, the quadratic — and none of them depend on which number
$F$ happens to be.

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
cost**. The per-turn figure is on the `stats` event and rendered nowhere;
what `/info` shows is the session total these turns add up to (section 8).

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

The same trace, with letsClaw's own estimator beside the truth and beside the
uncorrected local count it starts from:

| window after turn | truth | raw count | the estimate |
|---|---|---|---|
| 0 — empty window | 1,859 | 1,553 | **1,865** (+0.3%) |
| 1 — after a plain Q&A | 2,286 | 1,952 | **2,291** (+0.2%) |
| 2 — after a 2-tool turn | 3,651 | 3,219 | **3,662** (+0.3%) |
| 18 — "turn *n*" | 10,713 | 9,815 | **10,805** (+0.9%) |
| 19 — "turn *n+1*" | 11,110 | 10,195 | **11,218** (+1.0%) |

**Lesson 4: the corrections are not a safety factor.** The raw count is
consistently a *little* low, and the gap is not one error but three. The
estimator carries no safety margin at all — each of its three constants
corrects a measured error of a known shape, and what is left is under 1%. A
single blanket multiplier would read right at one window size and wrong at
every other; section 8 has the arithmetic.

The residual that remains is **positive and grows slowly with message count**
— +0.3% at the floor, +1.0% by turn 19. That is the deliberate rounding of the
per-message term from 10.79 up to 11 showing itself: this trace is 40 short
messages, so it pays that rounding 40 times. It reads *high*, which rolls the
window a fraction early rather than a fraction late.

> This estimate only appears when the server withholds usage, or just after a
> `/clear`, a rollover or a digest. On your SGLang server the normal figure is
> the measured one.

### One more correction: history is *not* "past history + digest"

A digest is not appended alongside the history. When it runs, it **replaces the
middle of one turn** — in turn 2 above, it would collapse the two tool calls and
their results into one ~200-token summary, while keeping your question and the
final answer verbatim. So that turn's 1,365-token footprint might shrink to
~700, with the full record still on disk in the journal.

It ships **off by default**, but your `config.yaml` now sets
`digest_turns: true` — with one exception, `argo-opus5`, whose model entry
overrides it back to false. So the trace above is what you pay **on argo**; on
any of the Qwen models a tool-heavy turn is collapsed as described.

---

## 4. The four kinds of token

| kind | also called | what it is | who pays |
|---|---|---|---|
| **Input** | prompt tokens | everything you send *up* — system message, tool schemas, whole conversation | charged on **every** request, in full |
| **Output** | completion tokens | everything the model generates | charged once per request |
| **Reasoning** | thinking tokens | the model's private `<think>` block | a **subset of output**, not a fifth category |
| **Cached** | `cached_tokens` | the slice of your input the server already had in memory | a **subset of input** — cheaper/faster, but still occupies the window |

Three traps worth naming, because all three bite people:

**Reasoning is inside output, not beside it.** Measured on your server: a
reasoning reply reported `completion_tokens: 110` with `reasoning_tokens: 94`
and a 24-character answer. The 94 is *part of* the 110. letsClaw's code assumes
exactly this, and the assumption is correct.

**Cached does not mean free of context.** A cached token is one the server did
not have to re-process — it is a *speed and cost* win, not a *space* win. It
still sits in the window and still counts toward the limit. This matters because
"I have 2,048 cached tokens" never means "I have 2,048 tokens of room back".

This is also why the cache figure is reported where it is. `/info` prints it as
a percentage of *input billed*, never as anything subtracted from the gauge —
the gauge is a space measurement and the cache has no business in it. Section 8
has the mechanics; the division of labour is the point here.

**Zero cached and no cache figure are different facts.** A server that says
`cached_tokens: 0` is telling you the prefix missed — something rewrote the
front of your prompt and the whole window was re-read. A server that says
nothing is telling you nothing. letsClaw keeps them apart: an unreported figure
is `None`, stays out of the denominator, and the `💾 Cache` line is omitted
rather than printed as `0%`. A silent server and a cold cache would otherwise
look identical, and only one of them is a problem.

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

Rounds that skip the schemas are ~715 tokens cheaper here. None of this is
visible in the `⚡` line, which reports the window *size* once per turn rather
than the sum of the prompts the turn actually sent. `/info`'s input odometer is
where that sum lands, across the whole session (section 8).

---

## 6. Digest and rollover — the two ways history shrinks

**Digest** (`conversation.digest_turns`, ships **false**, on in your config
except on `argo-opus5`, whose model entry overrides it). At the end of a turn
that used tools, the tool calls and their output
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

**Which number fires them is the estimate, more often than the measurement.**
Both gates read `used_now()`, and that falls back to the estimate after every
operation that reshapes history — including a rollover itself. So the first
round of a *brand new* window is judged by the estimator, and the question "is
this fresh window already past the trip point?" is asked of a figure nobody
measured. An estimator reading high answers yes on a window that is genuinely
half empty — rollover fires again immediately, or disables itself for the rest
of the session. So the estimator's accuracy is a correctness property, not a
display one: **the estimate is the rollover decision**, and section 8 gives its
measured error.

---

## 7. How letsClaw counts: two paths

**The measured path (normal here).** Your SGLang server reports real usage, so
letsClaw reads `prompt_tokens` straight off the response. Verified: SGLang
reports the **full** prompt regardless of caching — a request with 2,048 cached
tokens still reported `prompt_tokens: 2058`. So the window gauge is reading the
truth, and prefix caching does not corrupt it.

Three more numbers now come off the same usage block, and none of them touches
the gauge: `completion_tokens`, `cached_tokens` and `reasoning_tokens`. The
first two feed the session odometers of finding 10 and the hit rate of finding
9; the third is carried beside letsClaw's own `tiktoken` count of the reasoning
text rather than replacing it, because the two disagree on a truncated round —
measured against SGLang, one reply reported `completion_tokens: 5` next to
`reasoning_tokens: 10`. Each field is `None` on a server that omits it, and
`None` is propagated rather than flattened to zero.

At the end of a turn the window size is recorded as:

```
used = (the prompt the server just measured) + (our count of the answer) + 4
```

Deliberately **not** `prompt + completion`, because completion includes
reasoning, which is discarded. Using prompt+completion would inflate the figure
and could even make it *fall* as the conversation grew. This is a subtle call
and the code gets it right.

**The estimate path (fallback).** When a server withholds usage, letsClaw counts
locally with `tiktoken` (`cl100k_base`, a GPT encoding) and corrects that count
toward the server's own tokenizer with the three constants of section 2.
Section 8 takes that path apart and gives its measured accuracy.

The estimate is also what you see **after** any operation that reshapes history
— a digest, a rollover, `/clear`, a history trim — because each correctly
discards the stale measurement. So even on your server, the estimate is on
screen more often than you would think.

---

## 8. The three numbers in detail

Section 2 gives the algebra and section 7 gives the two paths. This section is
the measurement behind each: how accurate the estimate is, what the cache
figure means, what the input odometer counts, and where the `~` convention
still misleads. The estimate section ends with the alternative forms that were
considered and rejected, because the project's first design principle is that a
chosen solution comes with the ones it beat.

### 8a. The estimate, and why it has three constants

Measured against the real tokenizer, same messages, with and without tools:

| what | letsClaw's local count | server's truth | truth ÷ local |
|---|---|---|---|
| conversation text only | 1,201 | 1,265 | **1.05** |
| the 5 tool schemas | 462 | 715 | **1.55** |
| both together | 1,663 | 1,980 | 1.19 |

The tokenizer substitution is nearly harmless: `cl100k_base` is within **5%**
of Qwen's tokenizer on prose. **Most of the gap is in the tool schemas**, where
`json.dumps(specs)` misses the chat template's rendering scaffolding and comes
in **35% low** — the server's figure is 1.55× ours.

Read that 1.55 as a measurement of *this install*, not a constant: section 2
breaks the gap down tool by tool and finds it is $198 + 11N$ — a fixed block
plus a small per-tool cost — so the ratio depends on how many tools you offer
and falls toward ~1.12 as you add them. It was 1.69 at four tools and 1.55 at
five, which is the point: the ratio moved while the formula did not.

This is why the schema term is an **offset and not a ratio**. A fixed ~253-token
shortfall corrected by a multiplier grows without bound as the conversation
does, and it is the full window — not the empty one — where the number is
load-bearing.

The third shape is per-message, and it is the easiest to miss. Correct the
prose by a ratio and the schemas by an offset, and the result still runs low —
**−0.2% at the floor, drifting to −2.4%** as the window grows. A pure scale
cannot produce a drift, so the residual was fitted for shape:

$$\text{prose\_true} = a\cdot\mu + b\cdot\nu \qquad\Longrightarrow\qquad a = 1.0510,\; b = 10.79$$

worst residual **0.60%**. The per-token scale is 1.05 and the drift is $b$:
the naive count charges 4 tokens of framing per message where the real
template charges about 11, so a shortfall of ~7 tokens is paid once per
message — and conversations grow by messages. Hence three constants, not two.

That naive count is the baseline this corrects, not code you will find:
`count_message`/`count_messages` were deleted once nothing called them, and
`corrected_messages` builds from `count_content` up for the reason its
docstring gives — scaling the 4-token guess by the prose ratio arrives at 4.2
where the template charges 11.

#### What the schema term reads

$T$ is the schemas' **real** cost. It is measured rather than assumed: the
engine solves $T = \text{prompt\_tokens} - \text{corrected prose}$ the first
time a round goes past `note_schema_cost()` on a small enough window, and
caches it for the life of the process. "Small enough" is `SCHEMA_FIT_MAX_PROSE`
— the subtraction inherits the prose correction's error over the *whole*
window, so at ±1% a 4,000-token prompt contributes ±40 tokens of slop to a
block that is a few hundred, while a 90,000-token one would contribute ±900 and
the answer would be noise. Where no such observation exists — which is
precisely the withheld-usage case the estimate is load-bearing for — it falls
back to $198 + \tau + 11N$.

**Do not use $T = 1.69\,\tau$:** that ratio is an artifact of this particular
four-tool list and drifts toward 1.13 as tools are added. The formula is a
fixed block plus a per-tool constant, and only the formula survives a change in
tool count.

Measured against the live server at the nine checkpoints of section 3's trace
and its tool-heavy continuation:

| checkpoint | truth | the estimate |
|---|---|---|
| empty window | 1,859 | **1,865** (+0.3%) |
| after a plain Q&A | 2,286 | **2,291** (+0.2%) |
| after a 2-tool turn | 3,651 | **3,662** (+0.3%) |
| after turn 3 | 5,394 | **5,420** (+0.5%) |
| after turn 4 | 7,072 | **7,109** (+0.5%) |
| after turn 5 | 8,833 | **8,877** (+0.5%) |
| after turn 6 | 10,762 | **10,787** (+0.2%) |
| after turn 7 | 12,691 | **12,703** (+0.1%) |
| after turn 8 | 14,704 | **14,672** (−0.2%) |
| after turn 9 | 16,635 | **16,575** (−0.4%) |

**Worst case 0.5%.** The schema block on this install: formula says 715, the
server charges 715 — exact. (Those checkpoints were taken on the four-tool
configuration — see section 3 — so each is 120 tokens below today's. The
percentages are what the table is for, and a uniform shift in $F$ leaves them
where they are.)

#### What was rejected, and why

**A single multiplier over the lot.** One scale, applied to prose and schemas
together, is the cheapest thing that could work and it is what a
quickly-calibrated counter tends to be. It cannot work here: with a fixed
schema offset inside it, the overshoot ratio has a *limit* of
$\text{scale}/\alpha$ and climbs toward it as the window fills, so the constant
reads right at one window size and wrong at every other. Calibrate it on a
near-empty window — where template overhead dominates — and it overstates a
full one by half. Rejected on shape: changing the number moves the crossing
point, changing the form removes the curve.

**Raising `PROSE_SCALE` to ~1.08.** The apparent ratio $U/\mu$ climbs from
1.063 at the floor to 1.092 by the ninth checkpoint, so 1.08 splits the
difference and the table looks fine. It is the same mistake one order smaller:
a per-token constant papering over a per-message error, reading high on a few
long messages and low on many short ones — which is to say low on exactly the
long tool-heavy conversations that approach the wall. Rejected because the fit
shows the residual is per-message.

**A global safety multiplier as a knob.** Keeping one scale at 1.0 "in case we
need it" is an invitation: "bump it to 1.3" is the obvious answer to the next
overflow, and it is wrong in the way above. There is no such knob. The real
safety knobs exist and are tunable — `rollover_at_percent`,
`PAD_CONTEXT_MARGIN`, `max_output_chars` — and `PROSE_SCALE` is itself a
defensible one, in the right place, multiplying the single term that genuinely
scales.

**An online least-squares fit.** Instead of fixed constants, have the engine
accumulate $(\mu, \nu, \text{prompt\_tokens})$ triples and re-solve as it goes.
Tested on the nine checkpoints: it converges to **±2.2%**, worse than the fixed
form, because real conversations do not give the fit enough spread in $\nu$ at
fixed $\mu$ to separate the two terms — the design matrix is nearly singular
and the solution swings. It also adds persistent state that has to be
invalidated on every model and template change. Rejected: more machinery,
worse answer.

**Tightening the two margin constants now that the estimate is accurate.** An
accurate estimator looks like a reason to reclaim `PAD_CONTEXT_MARGIN` and
`MIN_TOOL_ROUND_ROOM`. It is not. `PAD_CONTEXT_MARGIN` is the *only* slack in
the closing invariant — since $\text{pad} = \text{free} - \text{reserve} - M$,
the worst case the design permits is exactly $\text{used} + \text{pad} +
\text{reserve} = \text{budget} - M$, so at $M = 0$ the worst case is the
server's hard limit and a one-token error is an HTTP 400. Neither constant is
compensation for estimator error: both read `used_now()`, which on a reporting
server is the exact measurement, and both are sized against the fact that it
describes the *previous* round's prompt. `MIN_TOOL_ROUND_ROOM` is a floor under
a figure derived from `DEFAULT_MAX_OUTPUT`, because that is configurable and a
constant with the default hardcoded into it would be wrong by the same factor
you raised it by.

> Scope note: this path is **inactive on your SGLang server**, which reports
> usage. It governs the fresh-window figure after `/clear`, after a rollover,
> after a digest, and any server that withholds usage.

### 8b. The cache figure

Cache hit rate is the one direct diagnostic for prefix-cache invalidation: a
sudden drop means something rewrote the front of the prompt and every session
quietly lost its KV cache, paying full price and full latency for input the
server used to have in hand. Nothing else on screen shows that.

`ChatResult` carries `cached_tokens` and `reasoning_tokens` off every
transport, the session keeps a running `total_cached` beside a `cached_input`
denominator, and `/info` prints the hit rate in all three clients. It never
touches the window figure, which is right with or without it — section 4 has
why a cached token still occupies the window.

**Three servers, three spellings of zero** — each measured, the same prompt
twice:

| server | cold | warm |
|---|---|---|
| SGLang | `prompt_tokens_details: null` | `{"cached_tokens": 2752}` of 2759 |
| llama.cpp (`/v1`) | `{"cached_tokens": 0}` | `{"cached_tokens": 2755}` of 2759 |
| Argo | neither key, ever | — |

So *absent*, *null* and *0* all occur and they do not mean the same thing. A
missing figure is read as `None`, never as zero, because zero is a claim that
the cache **missed** and an operator would act on it. The rate therefore gets
its own denominator: only rounds that came with a figure are in it, and `/info`
omits the line entirely rather than printing `0%` about a server that said
nothing.

That leaves one ambiguity — SGLang's `null` is a cold prefix, not a silence.
It is resolved by a latch: once an engine has reported a figure even once, a
later `None` from it is read as zero and the round counts against the rate.
The latch resets on `/model`, or a switch to a silent server would show every
one of its rounds as a miss and the rate collapsing to zero — an alarm about
nothing, raised by the very number that exists to raise it honestly.

**The trap, on the raw transport.** llama.cpp's `/completion` endpoint returns
a field called `tokens_cached`, sitting directly beside the `tokens_evaluated`
and `tokens_predicted` that become `prompt_tokens` and `completion_tokens`.
It is the obvious one to take and it is the wrong one. Measured:

| | cold | warm |
|---|---|---|
| `tokens_cached` | 2706 | 2706 |
| `timings.cache_n` | **0** | **2700** |

`tokens_cached` is how full the KV cache is *after* the request — prompt plus
prediction — so it is identical on a cold call and never moves. A reader would
have seen a permanent 100% hit rate and the diagnostic would have been worse
than absent, since it would have looked like it was working. `timings.cache_n`
is the reuse, and is what the raw path reports.

`reasoning_tokens` is carried too, and deliberately **beside** the local
tiktoken count rather than in place of it. The server's figure contradicts
itself on a truncated round — measured on the live server, one reply reported
`completion_tokens: 5` alongside `reasoning_tokens: 10`, a part larger than its
whole. The local count is of the text that actually arrived, which is the
honest thing to display; the server's rides the `stats` event beside it so the
disagreement is visible rather than hidden by a choice.

### 8c. The input odometer

`total_output` is an odometer of everything generated, and `total_input` is its
counterpart — the larger number by far on a tool-heavy turn (section 3),
because a turn re-sends the whole window on every round. Without it the only
input figure anywhere is `used`, which is the window measured once and says
nothing about what the conversation has cost.

`total_input` sits beside `total_output`, is persisted with it, and is printed
by `/info` in all three clients:

```
📤 Output: 48,210 tok generated in this session
📥 Input: 1,284,900 tok sent in this session
          (every round of every turn — the window is re-sent each time)
💾 Cache: 94.1% of that input served from the server's KV cache
```

**Every round counts, resumed ones included.** The window figure deliberately
ignores a resumed round — its prompt carries a thinking pad that will not exist
by the end of the turn — but that prompt was sent and it was billed, so it
belongs in the total. Without this the odometer would quietly become a second
copy of `used`, which is the one thing it exists not to be.

**No estimate, ever.** There is no local fallback for a round whose server
withheld usage: the prompt a round actually sent is not reconstructable on this
side — the pad never reaches history and the schema block is the server's
rendering, not ours. An unreported round therefore adds
nothing and flips `input_exact`, and the figure prints as `≥N`. The `≥` is not
the `~` used elsewhere on purpose: a tilde marks a guess that could land either
side, while this is a sum with holes in it and can only be low.

`input_exact` latches false for the life of the session rather than being
recomputed each turn. A later fully-measured turn cannot supply what an earlier
round never said, so a total that was once incomplete stays labelled
incomplete.

**Why a session total and not a per-turn line.** The per-turn figure rides on
the `stats` event and is rendered by nobody. It is a number you can do nothing
about once the turn has run, and the `⚡` line is already dense; what a long
conversation is *spending* is the actionable one. It is also the number that
makes a misfiring rollover visible — rolling a half-full window throws away
context and the input bill is where that shows up.

The odometers live in the live payload **without a `LIVE_SCHEMA` bump**, which
would make `load_live` discard every existing conversation. A session restored
from a file written before they existed starts its input count at zero and
under-reports its own past, which is much the smaller harm.

### 8d. Where the `~` convention breaks down

letsClaw marks estimated figures with `~`, which is better than most tools do.
The convention is applied unevenly, and these are the places it currently
misleads:

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
| `context ~N/262144` **with** `~` | ✅ calibrated estimate, within ~1% (section 8a) |
| `out N tok` without `~` | ✅ server-reported, includes reasoning |
| `out ~N tok` | ⚠️ local estimate, ~5% low |
| `reasoning N tok` | ✅ ~5% low, never marked but fine |
| `user N` / `LLM N` | ✅ ~5% low, never marked |
| `N tok of tool schemas` | ⚠️ the **raw JSON** cost, 41% below what the server charges — the window figure uses the corrected one, this line does not |
| `N this session` (output odometer) | ⚠️ mixed measured and estimated |
| `N tok sent in this session` (input odometer) | ✅ server-measured; `≥` means some rounds withheld usage |
| `N% ... served from the server's KV cache` | ✅ server-measured; the line is absent when no server reported one |
| what a **single turn** cost in input | ⚠️ on the `stats` event, rendered nowhere by choice |

**The short version:** on a server that reports usage — which is the path you
are actually on — the window figure is the server's own, and the subtle calls
around it (answer excluded from completion, pad excluded from the window,
measurement discarded whenever history is reshaped) are made correctly. On a
server that withholds it, the estimate is within about 1% from an empty window
to 17,000 tokens, because the three errors it has to correct are corrected
separately, each in the shape it actually has. No safety multiplier is
involved, and that is deliberate: a blanket multiplier cannot be right at more
than one window size.
