# Voice output — plan

Status: **planned** (2026-11-06; revised four times the same day — module design, relocation to the listener's machine, the scope below, and the two requirements added last: **stop mid-speech** and **speak a presentation rather than a reading**). Carved out of [webcam-voice-plan.md](webcam-voice-plan.md) as its own first slice: letsClaw speaks its answers. No microphone, no camera.

Goal: the WebUI speaks its answers, through the speakers of the machine the browser is on — in a form worth *listening to*, and interruptible.

Two requirements set the shape of everything after "Scope":

- **Speech is a presentation, not a reading.** The answer on screen is the artifact and it stays complete; what is spoken is a script derived from it, which refers to a table instead of reciting cells and skips an equation instead of saying "backslash frac". The full text is never replaced.
- **Speech must be stoppable in the middle.** One control, and it has to kill audio that is already buffered *and* audio still in flight.

## Scope (2026-11-06)

**WebUI only.** The terminal client and the Discord bot do not speak, and no separate voice process exists.

That decision is what makes this small. A module that speaks for every session existed for one reason: terminal and Discord had no other way to be heard. Take them away and the browser — which is the thing the person is looking at, on the machine with the speakers — can do the whole job itself. What remains is one route on the core and about a hundred lines in `app.js`.

| | previous draft | now |
|---|---|---|
| processes | `source/voice_client.py` + piper + `aplay` on the module's host | none; the browser is the player |
| where sound comes out | wherever the module runs | the browser's machine, by construction |
| who holds the on/off | the core, per session, persisted | the browser, per session, in `localStorage` |
| new command | `/voice` in the core, 8 listings | `/voice` handled in the page, like `/reasoning` |
| audio on the wire | none (module synthesized locally) | one WAV per spoken answer, to one tab |
| core state | `Session.voice_on`, persisted, broadcast | none |
| what is spoken | the answer, cleaned of markup | a **spoken script** derived from it — see "Presenting" |
| stopping | barge-in on the next turn only | barge-in **plus** an explicit stop that works mid-answer |

## The mechanisms, measured

Both were probed in headless Chrome over CDP with real input events under the strict desktop autoplay policy, not assumed.

| probe | result |
|---|---|
| `<audio>`.play() with no input at all | **rejected** `NotAllowedError` |
| play after a trusted **click** | `playing` |
| play **1.5 s** after the click, from a timer | `playing` |
| play after a **keypress**, and **8 s** later | `playing`, `playing` |
| `navigator.userActivation.hasBeenActive` after a keypress | `true` — and it stays true |
| `speechSynthesis.speak()` with no input | **`error: not-allowed`** |
| voices in Chrome on this box | 19, **all cloud** (`localService: false`), default `Google Deutsch` |
| piper, in-process | load 0.7 s / 136 MB; 359 chars → 0.79 s synth, 20.8 s audio, 894 KB WAV |
| piper, CLI subprocess | **~1.0 s** per call including interpreter and model load |
| piper on a 527-char spoken script | 0.93 s synth → **31.6 s** audio, 1394 KB WAV |

**The autoplay rule is satisfied by typing.** Chrome grants *sticky* activation to a document that has ever seen a gesture, and a keypress in the composer counts. The realistic sequence — type a question, wait, hear the answer — needs no click anywhere. This is the single most important measurement in the document: it is what makes voice a feature that works on first use rather than one that needs a click-to-enable ritual.

**Browser-native TTS is not acceptable as the primary voice.** `speechSynthesis` is the zero-dependency option and it was measured rather than dismissed, but on Linux every one of Chrome's 19 voices has `localService: false`: they are Google's. Answering a question from a private local conversation by posting its text to a third party, over the network, with a German voice as the default, is not a trade-off to make quietly. It stays a documented fallback for a machine that has no piper; it is not what ships.

So: **the core synthesizes with piper, the browser plays it.**

## Why the audio-on-the-wire concern from the earlier draft no longer applies

The previous draft rejected shipping audio from the core because a 1680-char answer is 6.9 MB base64 and aiohttp's `max_msg_size` is 4 MB. That objection was about pushing audio **into the event stream**, where `emit()` fans a copy out to every subscriber of the session and the cap applies.

`POST /speak` is a different mechanism and the objection does not transfer:

- the audio is a **response body to the one tab that asked** — no fan-out, no copy per subscriber, no memory multiplier on the core
- `max_msg_size` bounds *websocket messages*; `client_max_size` (1 MB) bounds *request* bodies. The request here is the text; the response has no such limit
- the text is capped before synthesis anyway (see the cleaner), so the size is bounded by design rather than by a cap being raised

One utterance is ~900 KB over a LAN — tens of milliseconds, and only for a session whose voice is on.

## Presenting, not reading

A voice that recites an answer is not useful, because the parts that make an answer worth having on screen — tables, equations, code, paths — are exactly the parts that cannot survive being read aloud. So the spoken thing is **a different text**: a short script derived from the answer, in the register of someone presenting it. The answer on screen is untouched and stays complete; voice is a second rendering, not a replacement.

That means a second model pass, which is the one thing this design used to claim was unnecessary. It is not a new mechanism: `build_turn_digest` already issues a side request on its own message list ("the real history is never touched and this request never becomes part of the conversation"), and `_summarise` already holds the no-thinking hint with its plain retry. The rewrite is a third caller of that pair, and it inherits the same guarantee — **nothing here enters history.**

Measured against the live local model, on a real answer containing a table, a display equation, a code fence and a path:

| input | out | result |
|---|---|---|
| 490-char answer (table + \\(t=n/\\tau\\) + fence + `./run.sh`) | 112 tok / 3.6 s | "…The table above compares load times for different Qwen models… Throughput is calculated using the formula provided… You can run the specific script shown…" — correct, no cells recited, no LaTeX read aloud |
| a 4-line code block, nothing else | 83 tok / 2.0 s | described the function's behaviour rather than reading `def f(x):` |
| a 2-row table, nothing else | 76 tok / 1.8 s | **found something to say** ("compares two specific values") instead of stalling — the case that most often breaks a rule-based cleaner |
| prose + table | 83 tok / 3.3 s | kept the two prose claims, referred to the table |

The behaviour on degenerate input matters more than it looks: a rule-based cleaner returns `[]` for an answer that is *only* a table, which means silence with no explanation. A presenter never does that — it says what the table is for. That is the qualitative reason for the pass; the table/formula handling is what it does for free.

### Cost, and how it is bounded

The pass costs 2–5 s of model time, which is latency in front of every spoken answer. Measured:

| engine | thinking hint | latency | note |
|---|---|---|---|
| local llama.cpp (`qwen38-local`) | on | 2.0–3.6 s | |
| remote SGLang (`qwen38-dgsSpark`) | on | 5.4 s | 0 reasoning tokens |
| remote SGLang | **off** | **7.4 s** | 302 reasoning tokens burned the whole `max_tokens` and returned nothing usable |

The hint is load-bearing, and `_summarise` already sends it — another reason to reuse that function rather than write a fourth call site. The remote row is why `voice.script` can be turned off: 5 s before the first word is a fair price for a table-heavy answer and a poor one for "yes, that's right".

So `voice.script` takes three values:

- `always` — every answer is rewritten.
- `auto` (default) — rewrite only when there is something to present. The test is cheap, deterministic, and runs on the **raw** answer: **skip the pass when the raw text is under `script_min_chars` (280) and carries no fence, no table row (`|`-led line), and no math delimiter** (`$`, `\(`, `\[`). A short prose answer is already a presentation; paying 5 s for it is the wrong trade. Raw, not cleaned, for two reasons: the cleaner *removes* fences and table pipes, so a gate that tested cleaned text could never see the thing it is looking for; and the cleaned text being empty while the raw text is not is precisely the degenerate case worth a pass — a code-only or table-only answer cleans to nothing, and the measurement above is what a presenter does with those (it finds something to say) versus what the cleaner does (silence).
- `never` — read the cleaned answer, as the previous draft did. 0.9 s to first audio, and the table problem comes back.

### Who does it, and where

The rewrite needs an engine, and engines live in the core's `SessionManager`. The page cannot reach one, so the pass is server-side — and that settles the route shape:

```
POST /narrate   {text, session}   ->  {"script": "..."}      (may fail -> page falls back)
POST /speak     {text}            ->  audio/wav              (never touches the model)
```

Two routes rather than one, because they have opposite failure modes and the page needs to survive them separately:

- `/narrate` is a model call: it can take 5 s, time out, or come back with something wrong. Its failure must **not** stop audio — the page falls back to speaking the cleaner's own output and says so once.
- `/speak` is pure CPU and must stay fast and dependable. Folding the model call into it would let a stalled remote model hold a synthesis worker, and would make a route that needs only piper also need a live inference server.
- Split this way, `/speak` is testable with a fake synthesizer and no engine at all, and `/narrate` with a fake engine and no piper.

`/narrate` picks the engine in this order: `voice.model` if set, else **the engine of the session named in the request** — the model that wrote the answer should be the one that presents it — else `models.default_model`. The session lookup is `mgr.sessions.get(name)`, read-only: `mgr.get()` *creates* a session, and a narrate request must never invent one. An unknown or absent name is not an error; it falls through to the default.

### What the script must not do

Three rules go in the prompt, each because a measurement or a reading of the answer shows what happens without it:

- **Never recite a table, cell by cell** — say what it compares. Reading `| Q6_K | 41 s |` aloud produces "Q 6 underscore K 41 s", which is worse than silence.
- **Never read markup, LaTeX, paths or command lines as prose.** `\[ t = \frac {n}{\tau} \]` spoken literally is noise; refer to "the formula above". (A path is the exception the measurement found: `./run.sh -motion-only -b` *is* pronounceable-ish, and piper renders it in 6.5 s of audio, so the rule is "refer to the command shown" rather than a ban.)
- **Keep every claim and every number you do state accurate.** A presentation that invents a figure is worse than a reading. This is the one rule that is a correctness constraint rather than a style one, and it is why `temperature` is 0.2 and the pass is capped at `script_max_tokens` (300) — a budget that fits 60–90 words and refuses to run long.

The prompt is a module-level constant beside `digest_messages`, not a behavior file: it is a mechanical instruction to re-render text, and putting it in `models/` would invite a person to edit their voice's *identity* when all they want is different pacing. Length and voice choice stay in `voice:` config.

### Numbers

The rewriter naturally spells numbers out ("thirty-two gigabytes", "twenty-seven point four"), which is good — but it is not a rule worth paying tokens for. Measured directly: piper's phonemizer reads `32 GB of VRAM` into 8.7 s of audio and the spelled-out version into 9.4 s, i.e. **it already says the number correctly either way.** The prompt asks for numbers only where they carry the point; it does not ask for spelling, and the cleaner leaves digits alone.

## Core: two routes

```
POST /speak      {text: "..."}          ->  audio/wav          (or 503 with a reason)
POST /narrate    {text, session}         ->  {"script": "..."} (or 503 / 504 with a reason)
```

Both take `_guard`, not `_read_guard`: `/speak` costs CPU bounded by `max_chars`, `/narrate` costs inference bounded by `max_answer_chars` and `script_max_tokens`. Both get the same both-checks treatment as `config_save` and `rename_session`.

`/narrate` specifics:

- The text it is given is the **raw answer**, capped at `voice.max_answer_chars` (4000) — generous enough for a real answer, still a bound on what gets stuffed into a prompt. Over it → `400`, and the page speaks the cleaned text instead of the answer's head. Deliberately *not* pre-cleaned: the cleaner drops fences and table pipes, and a presenter handed text with the tables removed has nothing left to say "the table above compares" about. It needs to see the structure to refer to it — which is exactly what the measurement above showed it doing.
- It calls a new `build_spoken_script(engine, answer, max_tokens=...)` placed in `source/session.py` beside `build_turn_digest` and `_summarise`, which it reuses rather than duplicating — the same reason `build_turn_digest` reuses it: "the hint is a guess about one server's chat template, so the day it needs a second key or a different fallback is the day two copies become one right and one wrong." The request carries no turn and must not take the session's lock; `mgr.sessions.get(name)` is a read, and `mgr.get()` would *create* a session and must not be used here.
- Timeout: `voice.script_timeout` (12 s). A stalled remote model must not hang a spoken answer indefinitely; on expiry the route answers 504 and the page falls back. This is the whole reason `/narrate` is separate — a fallback the page can act on needs a response code to act on.

`/speak` specifics:
- `_guard`, not `_read_guard`: it costs CPU and reads the model file, so it takes the same both-checks treatment as `config_save` and `rename_session`.
- Body is `{text}`; empty or whitespace-only after cleaning → `400`, not silence. A client that sent nothing to speak should hear about it.
- Text length capped server-side at `voice.max_chars` **before** synthesis, so a hostile or buggy client cannot ask the core to synthesize a megabyte and pin the event loop's worker thread. The cap is a **per-request** bound, not a content limit: the page splits a long answer into sentence-aligned chunks and asks once per chunk, so the whole answer is spoken. See "Long answers".
- Synthesis runs on `asyncio.to_thread` — it is 0.05–0.8 s of CPU in a native library, which is exactly the shape `to_thread` exists for. A turn must never be slowed by someone's browser wanting to hear the last answer.
- One piper voice loaded lazily on first use and kept for the process's life (0.7 s, ~136 MB). Never on startup: a core with voice off must not pay it.
- **`piper` is imported lazily, the way `token_counter.py` imports `tiktoken`** — inside a `try/except ImportError`, with the failure recorded as a reason string. The core must boot and serve the WebUI with no piper installed; that is the difference between an optional feature and a new hard dependency.
- When piper or the model file is missing: `503 {"ok": false, "reason": "..."}` with the reason naming what to install or download. The WebUI turns that into one notice and stops offering the button.

### Capability, so the button is never a lie

`/info` gains `"voice": {"available": bool, "reason": str, "max_chars": int, "max_chunks": int, "script": "always" | "auto" | "never", "script_min_chars": int, "max_answer_chars": int}` — the same field `_digest_state()` sits beside, fetched by the WebUI on attach for exactly this purpose. `available` is "piper imported and the model file exists", computed once and cached.

Every number the page needs to obey rides along, because the **core's** config owns them and a **page** that hardcoded 400 would silently disagree with a core retuned to 200 — it would send 400-character requests and every one would be refused. The page falls back to its own default for any field that is absent, which is what an old core returns.

`script` rides too, so a `/reload` that turns the rewrite off is obeyed by an open tab rather than needing a refresh.

`/health` is deliberately **not** the source: it is unauthenticated, and "voice is available" is not something to advertise to anyone who can open a socket.

### Config

`voice:` joins `SECTION_KEYS` in `source/config.py` **and** `config.example.yaml` — the two are pinned against each other, so a section in one and not the other fails.

```yaml
voice:
  enabled: true                        # the /speak route answers; false -> 503 always
  model: "voices/en_US-lessac-medium.onnx"   # the piper voice file
  max_chars: 400                       # most any one /speak request may carry
  max_chunks: 8                        # most any one turn may ask for
  speed: 1.0                           # piper length_scale
  gain: 1.0                            # piper volume
  script: auto                         # always | auto | never — present, or read?
  script_model: ""                     # model entry for the rewrite; "" = the session's own
  script_min_chars: 280                # `auto`: under this, and plain, just read it
  script_max_tokens: 300               # the script's budget (~60-90 words)
  script_timeout: 12                   # seconds before the page gives up and reads
  max_answer_chars: 4000               # most any one /narrate request may carry
```

Eleven keys, all read by the core, none by any other process — which is the whole point of the scope change: with no module, there is no second machine's opinion about what a session wants.

`model` and `script_model` are two different kinds of model and the names are deliberately not shared: `model` is a `.onnx` file path, `script_model` is a name in the `models:` registry. The reader who confuses them configures a TTS engine to look for a GGUF. `script_model: ""` means "whatever session wrote this answer is running", which is the useful default — the model that had the thought should be the one that phrases it — and it is also the cheapest, because it reuses an engine that is already built rather than forcing a second one up.

`DEFAULT_CONFIG` is left alone. A config with no `voice:` block gets `enabled: true` and the default model path, which is the behaviour a fresh install should have: the route exists, and if piper is missing it says so with a reason. Generating a section nobody asked for is not a bootstrap, it is noise.

`requirements.txt` gains `piper-tts` under **optional**, in the company of `discord.py`, with the same wording habit: the core imports it lazily and does not require it.

## WebUI: the button, the switch, the queue

### Where the switch lives, and why not in the core

In the browser, per session, in `localStorage` — `letsclaw.voice.<session>`, the same key shape `letsclaw.sent.<name>` already uses, for the same reason written down next to it: what you want read aloud in one conversation has no business applying to another.

This is the part the scope change moves, so it deserves the reason rather than just the decision. The previous draft put the flag in the core because *several machines* had to agree about it — a module on B, a tab on B, a phone, all needing one answer, and only the core can hold a fact three machines read. With one consumer, that argument is gone, and what is left is the ordinary property of a display preference: it belongs to whoever is looking.

Consequences, all of them improvements:

- **no core state, no persistence, no migration.** `LIVE_SCHEMA` untouched, `describe()` untouched, `persist()` untouched.
- **no command surface.** `/voice` joins `/reasoning` in the page's local handling and `NOT_CORE_COMMANDS` in the command-surface check — the list that already carries `reasoning` for exactly this reason ("a display toggle, local to whichever client drew it"). `chat.py`, the Discord help text, the slash tree, `index.html`'s titles and README's command table all stay untouched, because none of them can speak. `/voice stop` is a fourth argument to the same local branch, and it belongs there for the same reason: stopping is an act on this tab's audio, which is the only audio there is.
- **a tab that cannot hear does not show a lit button.** With a core-side flag, a session with no piper behind it still reads "on" everywhere and stays silent. With the flag in the browser, the button is painted only when `/info` says voice is available.
- the terminal and Discord simply do not see the switch, which is correct: they have no speakers to turn on.

What is given up is the property the previous draft bought with the core-side flag: a fresh tab on another machine does not inherit the choice. That is the right trade here for the same reason the flag is in the browser — the choice is about a piece of hardware, and a different browser is a different piece of hardware.

### The button

| piece | change |
|---|---|
| `web/index.html` | `<button id="btn-voice" class="tog" title="...">🔊 voice</button>` beside `#btn-digest`. **Labelled, not icon-only** — the header carries a comment saying so, because emoji fall back to tofu on a machine with no emoji font. The label text stays `voice` in every state so the row never changes width |
| `web/app.js` | `S.voice`, read from `localStorage` on load and written on toggle; painted from it, and from `/info`'s `voice.available` |
| `web/app.js` `COMMANDS` | `/voice` — this list is a hard gate, not help text |
| `web/style.css` | **nothing.** `header button.on` already exists, and the row wraps rather than overflows (measured below) |

Four states, not two, because a toggle that cannot tell you why it is inert is the worst kind — and because the button is also the stop control:

| state | label | meaning |
|---|---|---|
| off | `🔇 voice` | answers are not spoken |
| on | `🔊 voice` | answers are spoken, this tab |
| speaking | `⏹ voice` | clicking **stops this answer** and leaves the setting alone |
| unavailable | `🔇 voice` greyed, `title` = the reason from `/info` | no piper or no model on the core; clicking it prints the reason instead of toggling |

The `speaking` state is what makes stop discoverable without a twelfth button, and it is honest: the button says what clicking it will do *now*. It is driven by the queue's emptiness, not by `audio.paused`, because the gap between chunks is `speaking` and a control that flickers to 🔊 at every seam is worse than one that lies about nothing.

`/voice on`, `/voice off`, `/voice stop` and bare `/voice` all work through the same local branch that handles `/reasoning`, and the notice they print follows its wording (`🔊 voice on — this tab`), naming the tab because that is the scope.

### Only the tab that asked speaks

`localStorage` is shared across tabs of an origin, so a flag there would make **every** tab on a session speak the same answer — two tabs, two voices, one slight offset apart. That is not a corner case; the sidebar exists precisely because people open several sessions and views at once.

The fix is already in the file. `turn_start` is handled with:

```js
if (S.weAsked) follow = true;
S.weAsked = false;
```

with a comment explaining that `turn_start` is a broadcast and re-asserting the follow unconditionally was a bug — a question typed in one tab, or in Discord, yanked a second tab back to the bottom. Voice has the identical problem and the identical answer, with one wrinkle worth naming: **`S.weAsked` is consumed at `turn_start`, and speech happens at `turn_end`.** So the flag must be captured into the turn object as it is created —

```js
S.turn = newTurn();
S.turn.asked = S.weAsked;      // consumed below; speech needs it at turn_end
```

— and `turn_end` speaks only `if (S.turn && S.turn.asked)`. A turn started in another tab, or by Discord, or by the terminal, is rendered everywhere and spoken nowhere. That is the correct default and it needs no cross-tab protocol.

It also means a second tab on the same session can be the silent one while the first speaks, which is the behaviour the follow-cascade fix established.

### Stopping mid-speech

Stopping is not the same act as turning voice off, and conflating them produces the button that people stop using: someone cut off by a long answer wants the next answer spoken, and someone who has had enough wants silence until they say otherwise. Two controls, one of them transient:

| | what it does | lasts |
|---|---|---|
| **stop** | kills the current speech, now | this answer only |
| **off** (`/voice off`, or the button while idle) | no further answers are spoken | until turned back on, per tab |

Stop has to reach three places, and missing any one of them leaves audio playing after a click that looked like it worked:

1. **the element that is playing** — `pause()`, then `src = ""` and revoke the object URL. `pause()` alone stops output but leaves the buffer held; the drop is what guarantees nothing resumes.
2. **the queue** — the array of chunks not yet fetched or played, cleared. Without this the next chunk starts a quarter-second after the "stop".
3. **the requests in flight** — one `AbortController` per turn, held on `S.turn`, aborted on stop. This covers both `/narrate` and every `/speak`: a 5-second rewrite still in the air must not deliver a script to a turn the person has already abandoned, and a WAV still downloading must not play after its queue is gone.

The third is the one that is easy to get wrong and the reason the controller is per-turn rather than per-request: an `abort()` on a controller that has already been replaced aborts the wrong thing.

**Where the control lives.** The voice button *becomes* the stop control while speech is playing — `⏹` with a title saying so — and returns to 🔊/🔇 when the queue drains. This is the media-button idiom (a play control that becomes a pause control), and it is chosen over a twelfth header button on purpose: a transient button appearing and disappearing shifts the status chip sideways every answer, which is both a layout wobble and a target that moves. Clicking `⏹` stops and does **not** change the setting; the notice says `⏹ stopped` so the difference between "I cut this one off" and "I turned it off" is legible after the fact.

Two more ways in, both free once the function exists: `Escape` stops speech when no overlay is open (the existing Escape listener already walks `overlays`; the new call goes after that loop, so a panel that is open keeps its meaning), and `/voice stop` in the composer. All three reach the same `stopSpeech()`.

`<audio>` is kept rather than Web Audio: `pause()` + `src = ""` is a stop, and the Web Audio path (`decodeAudioData` + `AudioBufferSourceNode.stop(0)`) buys sample-accurate silence at the cost of a decode step and a second playback implementation. If the build finds `<audio>`'s stop perceptibly laggy, that swap is the fix — recorded under open edges rather than pre-built.

### The queue

One `<audio>` element, one object URL at a time, one promise chain:

1. `turn_end` → take `el._raw` → clean it (below) → if the cleaned text is empty **and the gate below says no pass**, do nothing. Cleaned-empty with a gate that says *yes* is the code-only or table-only answer, and it goes to the pass rather than to silence.
2. **Decide whether to present** (below) → `POST /narrate` with the **raw** answer if worth it, with the turn's `AbortController`. A 503/504/404 or an aborted response means **fall back to the cleaned text from step 1** — never to no speech. One notice per session per page load, not per turn.
3. `spokenChunks(script || cleaned, max_chars, max_chunks)` → the queue. If that list comes back empty — the pass failed *and* the cleaner found nothing — say `🔇 nothing to read` in the notice channel and send no request.
4. `POST /speak` per chunk with `authHeaders()` and the same controller; the first response is awaited, the rest are prefetched one ahead so the chain never waits on the network at a seam.
5. `response.blob()` → `URL.createObjectURL` → play.
6. Revoke the previous URL before replacing it. Revoke on `onerror` too.
7. A new `turn_start` while something is playing: `stopSpeech()` — the same function the button calls, not a second copy of its three steps. **Barge-in** is stop plus the new turn's queue, which is why it costs nothing extra.
8. A 404 means an old core; the button stops offering voice for the page's life and says so once. A 503 prints its reason once and marks the button unavailable, so a broken voice is discovered on the first answer, not repeated on every one.

The text to speak is `el._raw` from the turn's own element — the unrendered markdown the streaming path already accumulates, which is exactly the string the cleaner wants.

**Whether to present** is decided in the page, from `/info`'s `voice.script`, because the page is what holds the answer's shape and the decision must be cheap enough to make on every turn:

| `voice.script` | page behaviour |
|---|---|
| `always` | `/narrate` every answer |
| `auto` | `/narrate` only if the **raw** answer is ≥ `script_min_chars` **or** contains a fence, a `|`-led table row, or a math delimiter; otherwise speak the cleaned text directly |
| `never` | never call `/narrate` |

`auto`'s test is deliberately dumb and local, and it runs on the raw answer for the reason given in "Presenting": the cleaner deletes the very markers the gate looks for. It is not trying to judge whether the answer is *interesting* — it is asking whether there is anything in it that cannot be read aloud. A 90-character prose answer fails that test and gets its 0.9 s path; a table gets the pass.

The script comes back as **one string**, and `spokenChunks` runs on it afterwards. Chunking after the rewrite rather than before means the presenter sees the whole answer, which it must to say "the table above compares" — a per-chunk rewrite would lose the referent.

### The cleaner

JavaScript, in `app.js`, beside `renderMd`: `spokenChunks(raw) -> [string]`.

- drop fenced code blocks entirely — a fence read aloud is noise
- drop inline markup: `**`, `__`, backticks, `#` headings, table pipes
- links keep their text, lose the URL
- drop emoji and symbol-plane characters
- collapse whitespace
- **split into chunks of at most `max_chars`**, breaking at the last sentence boundary that fits, or the last clause boundary if a single sentence is longer, or mid-word only as a last resort. A chunk never starts mid-word.
- empty result → `[]` → silence, and no request

Returning a list rather than one string is what makes the cap harmless: the page asks once per chunk and the queue plays them in order, so a 1200-character answer is spoken in full over three requests instead of truncated to one. The server's `max_chars` then means exactly what it should — the most any single request may cost — and nothing more.

**Tested in node, the way `test_md.mjs` tests `renderMd`** — that suite already extracts a DOM-free section of `app.js` with `new Function` and asserts on it, which is exactly the shape `spokenChunks` has. This is why the cleaner is JavaScript rather than a server-side helper: it runs next to the renderer whose output it undoes, and it can be tested without a browser, a core, or a network.

### Why not reuse `renderMd` and strip tags

Tempting and wrong. `renderMd` is built to make markup *safe to look at*, not *pleasant to hear*: it escapes, it keeps table structure, it renders maths to KaTeX spans whose accessible text is not the sentence, and it wraps code in `<pre>` that a text-extraction pass would read as one long unbroken line. The cleaner's job is the opposite of the renderer's — remove the structure rather than present it — and it starts from `_raw` for that reason.

## Long answers

An answer longer than `max_chars` is **spoken in full**, as several requests.

The reason to chunk rather than truncate is that the cap exists to protect the core from a megabyte of synthesis, not to decide how much of your answer is worth hearing. Truncating would let a safety limit silently eat content — the worst kind of silence, because the page looks like it worked.

Measured on a 743-character answer (8 sentences), against the same model:

| measurement | value | what it decides |
|---|---|---|
| whole paragraph, one call | 1.77 s synth → 40.4 s audio | chunking costs nothing in total synth time |
| 8 sentences, one call each, +8 frames silence between | 1.80 s synth → 40.3 s audio, 1734 KB WAV | splitting is free; the seam is inaudible |
| first sentence alone | 0.17 s | **why chunking is the better default**: the first request answers in a fifth of a second, so speech starts while the rest is still being synthesized |

So the queue is filled with chunk 1's audio first, played while chunks 2 and 3 are fetched — the pipeline the streaming path already uses for text. Total audio is identical to one call; the only cost is one HTTP request per chunk, and the `max_chars: 400` default means a 40.4 s answer is 3 requests.

Two guards keep this bounded:

- `voice.max_chunks` (default 8) caps how many requests **one turn** may make, and the page says so when it stops: "spoke 8 of 23 parts". A runaway answer is still a bounded number of requests, so the per-request cap does the CPU work and this does the queue-length work.
- `stopSpeech()` clears the queue whole — on barge-in, on `turn_start`, and on an explicit stop — so a long answer cannot leak speech into the next turn.

The rewrite pass changes these numbers rather than the mechanism. A 743-character answer becomes a ~527-character script, which is 2 chunks instead of 3, and 31.6 s of speech instead of 40.4 s — **presenting shortens the queue as a side effect**, because it drops what could not be said usefully anyway. `max_chunks` measured against scripts, not raw answers, is therefore roomier than it looks: 8 chunks is roughly 3 200 characters of script, about six minutes of speech.

## Where the previous draft's pieces went

| previous draft | now |
|---|---|
| `source/voice_client.py` | gone — no process to run |
| `run_voice.sh`, `voice_probe.py` | gone |
| `serve.sh` starting a module | gone; `serve.sh` is untouched |
| `Session.voice_on`, `_voice_state()`, `_effective_voice()` | gone |
| `/voice` in `Session.command` | gone — handled in the page |
| `describe()` gaining `"voice"` | gone — nothing polls it for this |
| persist / `load_state` / `LIVE_SCHEMA` | untouched |
| `chat.py`, Discord help, slash tree | untouched |
| the cleaner's rules | kept, rewritten in JavaScript |
| barge-in | kept, and generalized into `stopSpeech()` — the same call serves barge-in, the button and `Escape` |
| "core changes: none" | **no longer true** — presenting needs a model pass, so the core gains `/narrate` as well as `/speak` |
| `aplay` / `device:` / PipeWire | gone — the browser owns the output device |
| the echo risk for the wake-word phase | unchanged; that phase has its own problem |
| piper measurements | kept, and now they size a route rather than a daemon |

## Two-machine setups

Nothing to configure. The WebUI is already served by the core and already opens its own websocket, so "machine B runs the WebUI" is the ordinary case of pointing a browser at `http://A:8770`. Voice lands on B's speakers because the browser does, and the audio crosses the network as a response body to that one tab.

The rewrite pass runs on **A** too, which is the right side: A is where the engines live and where the inference already happens, so B needs no model, no key and no GPU — a laptop with no piper and no llama.cpp gets the same voice as the box it is talking to. The cost of that placement is that the 2–5 s pass is paid on A's inference queue, which is the same queue a turn is waiting on; `script: auto` exists partly to keep that from happening on every reply.

The one consequence worth stating: **B's browser must be able to reach the core**, which for a non-loopback bind means `core.token` becomes the only thing between a stranger on the LAN and an unconfined `exec` tool. Voice does not create that exposure — `bind` does — but voice is a plausible reason to set `bind`, and the README must not let that happen quietly.

## Measurements that shaped this

| probe | result | what it decides |
|---|---|---|
| `<audio>`.play(), no input | rejected `NotAllowedError` | a page cannot speak before any input |
| play after a trusted click | `playing` | a click unlocks the document |
| play 1.5 s after the click, from a timer | `playing` | websocket-driven playback works |
| play after a **keypress**; 8 s later | `playing`; `playing` | **typing the question is enough** — no click ritual |
| `userActivation.hasBeenActive` after a keypress | `true`, and it persists | the unlock is sticky, not transient |
| `speechSynthesis.speak()`, no input | `error: not-allowed` | browser TTS is gated the same way |
| Chrome voices here | 19, all `localService: false`, default `Google Deutsch` | browser TTS = Google's cloud; rules it out as primary |
| piper in-process | load 0.7 s, 136 MB resident, 176 MB peak; RTF ≈ 0.05 | load once per core process, lazily |
| piper CLI subprocess | ~1.0 s per call | why the core keeps the voice loaded instead of forking |
| piper, 359-char answer | 0.79 s synth → 20.8 s audio, 894 KB WAV | one utterance's size on the wire |
| piper, 1680-char answer | 123 s audio, 6.9 MB | why `max_chars` bounds **one request**, and why a long answer arrives as several |
| piper, 743-char answer as 8 sentence calls | 1.80 s synth → 40.3 s audio (vs 1.77 s / 40.4 s in one call) | chunking costs **nothing** in total; splitting is free |
| piper, first sentence of that answer alone | 0.17 s | why chunking is the better default: speech can start in a fifth of a second |
| piper, 527-char **script** | 0.93 s synth → 31.6 s audio, 1394 KB WAV | what a presented answer actually costs on the wire |
| piper, digits vs spelled-out numbers | 8.7 s vs 9.4 s audio for the same sentence | piper's phonemizer already says `32 GB` right — no need to spend tokens spelling numbers |
| rewrite pass, local llama.cpp | 2.0–3.6 s (76–112 tok out) | the latency `voice.script: auto` exists to avoid paying on short answers |
| rewrite pass, remote SGLang, hint **on / off** | 5.4 s (0 reasoning tok) / **7.4 s** (302 reasoning tok, output unusable) | `_summarise`'s no-thinking hint is load-bearing, so reuse it rather than write a fourth call site |
| rewrite of a table+formula+fence+path answer | correct script; "the table above compares…", "the formula provided", "the specific script shown" | the pass does the thing it is for, unprompted-by-example |
| rewrite of a **code-only** answer | described the function's behaviour instead of reading `def f(x):` | degenerate input is the case a rule-based cleaner cannot handle |
| rewrite of a **table-only** answer | found something to say ("compares two specific values") | a rule-based cleaner returns `[]` here — silence with no explanation |
| header, clone button appended, 1440/1280/1100/900 px | `scrollWidth == clientWidth` at all four; joins the row down to 1100, wraps at 900 | an 11th header button needs **no CSS change** |
| `client_max_size` (aiohttp default) | 1 MB, on the **request** body | the request carries text; the response carries the WAV |

## Tests

**Core** — new suite, fast tier, hermetic (no model file, no engine):

- `piper` missing → `/speak` answers 503 with a reason, `/info` says `available: false`, and **the core boots and serves normally** — the lazy-import guarantee, asserted
- model path configured but absent → the same 503, different reason
- `voice.enabled: false` → 503 even when piper is importable
- a monkeypatched fake synthesizer returns fixed bytes → `/speak` replies `audio/wav`, non-empty, and the bytes are what the fake produced
- text over `max_chars` → `400`, and **the fake receives no call at all**. Not truncated: a silent cut on the server is the exact failure chunking exists to prevent, and the page is the only legitimate client, so an over-length request is a bug and should be reported as one
- `max_chars` at the boundary (exactly the limit) → synthesizes
- empty / whitespace-only text → 400
- unauthenticated → 401 with a token set; `_guard` means both header and Origin are checked, on **both** routes
- a 1 MB request body limit is not tripped by a 400-char text (guards against a future change that sends audio-shaped payloads)

`/narrate`, with a fake engine standing in for `engine.chat` — the pattern `test_turn_digest.py` already uses, including `manager.engine_for`:

- a fake engine returning a fixed script → `{"script": ...}`, and the fake was called **once**, with the no-thinking hint in `extra_body`
- `voice.script: never` → 503, and **the fake is never called** — the config switch is real, not a page-side convention
- text over `max_answer_chars` → 400
- the fake raises, or returns `""` → 503 with a reason, **not** a 200 with an empty script: the page distinguishes "nothing to say" from "the pass failed" by status code, and an empty 200 would silently speak nothing
- a `session` name that does not exist → still answers, on the default engine, and **`mgr.sessions` is unchanged afterwards** — the `get()`-creates-a-session trap, asserted rather than trusted
- `script_model` set → that engine is the one asked; unset → the session's own
- the script is capped: a fake returning 4 000 characters comes back at or under a bound the page can chunk, so a runaway pass cannot smuggle an oversized request into `/speak` and turn a 400 into silence

**WebUI** — `tests/test_voice.mjs`, node, the `test_md.mjs` extraction pattern:

- a fenced code block contributes nothing
- `**bold**`, `` `code` ``, `# heading`, table pipes contribute their text only
- `[text](url)` contributes `text`
- emoji contribute nothing
- an answer of 3× `max_chars` returns **3 chunks whose concatenation is the whole text** — nothing lost, no chunk over the limit
- chunks break at sentence boundaries; a sentence too long for one chunk breaks at a clause boundary; a single oversized token is cut rather than dropped
- zero chunks for an answer that is only a code fence → no request at all
- `max_chunks` is respected: the 9th chunk of a 23-chunk answer is not returned
- **the `auto` gate**: a 90-char prose answer → no `/narrate`; the same answer with a `|`-led table row → `/narrate`; a 400-char prose answer → `/narrate`. Asserted on the predicate, since it is the rule that decides whether a turn pays 5 s
- **the gate reads raw, not cleaned**: a code-fence-only answer cleans to `[]` yet still triggers `/narrate`. This is the regression test for the mistake of gating on cleaned text — the cleaner deletes the fence the gate looks for, so a code-only answer would otherwise be silently never spoken
- **both sources empty**: pass fails *and* cleaner returns `[]` → no `/speak` request, and the `nothing to read` notice — the one case where silence is correct, and it must be explained rather than assumed
- a script from `/narrate` is chunked **after** the rewrite, so a 527-char script yields 2 chunks and the raw answer's shape never reaches `/speak`

**WebUI, live** — `test_phase4_webui.py`, which already drives real Chrome:

- the button toggles, persists to `localStorage`, and survives a reload
- a second tab on the same session does **not** speak a turn the first tab asked for (the `S.turn.asked` rule; assert via a stubbed `POST /speak` that counts calls)
- `/voice on` in the composer reaches the same state as the button
- with `voice.enabled: false`, the button renders unavailable and clicking it prints the reason rather than toggling
- **stop**: with a stubbed `/speak` that returns a long-lived audio object, start speech, click the button in its `⏹` state, and assert three things — the element is paused with no `src`, the queue is empty, and the turn's `AbortController.signal.aborted` is `true`. Then assert the **setting is still on**, so stop did not become off
- `Escape` stops speech with no overlay open, and does **not** when one is (the existing overlay handler keeps its meaning)
- a `/narrate` that 504s still produces speech (the fallback), and the notice appears once rather than on every turn

**Manual smoke** (not a test): type a question with voice on, hear the answer presented rather than recited — with a table in it, confirm no cells are read; ask again mid-speech and hear the first cut off; press `Escape` mid-sentence and confirm silence within one frame, including a chunk that had already downloaded; open a second tab and confirm it stays silent.

## Build order

1. **Core routes**: `POST /speak` and `POST /narrate`, `build_spoken_script` in `session.py` beside `build_turn_digest` reusing `_summarise`, lazy piper import with a recorded reason, `voice:` in `SECTION_KEYS` + `config.example.yaml` + `requirements.txt`, the `voice` payload in `/info`, and both core suites. Independently shippable — two routes that nothing calls yet are inert, and their tests run without a browser.
2. **WebUI**: `spokenChunks()` + the `auto` predicate + `test_voice.mjs` first (pure, no DOM), then `stopSpeech()` and the queue, then the button's four states, the `localStorage` key, `/voice` in the local command branch, the `Escape` binding, and the `S.turn.asked` capture. Then the live Chrome checks.
3. **Docs pass + version bump**, full tier green first. README's WebUI section, `docs/clients.md`, and the piper line under optional dependencies with the voice-model download next to it.

Step 2 is the one worth splitting at `stopSpeech()`: it is a function with three effects and four callers, and it is testable before the button exists.

## Risks / open edges

- **The rewrite pass can be wrong.** It is a model restating another model's answer, and it will occasionally soften a caveat or round a figure it should not. Mitigated, not solved: the on-screen answer is complete and untouched, the prompt forbids inventing or altering anything it states, `temperature` is 0.2, and the script is short enough to fact-check against the text below it. `voice.script: never` remains for anyone who wants a verbatim reading. This is the real cost of presenting instead of reading, and it should not be described as free.
- **Speech arrives after the answer is fully rendered**, since it is triggered on `turn_end`, and the rewrite pass adds 2–5 s in front of it on the `auto` path. Chunking *during* streaming would start speech seconds earlier. Deliberately not the first version: `turn_end` is one clean text with one failure mode, while a mid-stream split has to guess where a sentence ends before the tokens after it have arrived, a fence that hasn't closed yet reads as prose, and **a rewrite needs the whole answer** — "the table above" has no referent in a fragment. The `auto` gate is the cheap half of the fix: the short answers, which are most answers, skip the pass entirely and speak 0.9 s after the turn ends.
- **A long answer is 20+ seconds of speech.** Chunking makes sure all of it is spoken; presenting makes it shorter but still long. `voice.max_chunks` is the control on how much a single turn may ask for, the page prints "spoke 8 of 23 parts" when it stops, and `stopSpeech()` is one click away — the difference between this and the truncation that section replaced.
- **`stopSpeech()` has three jobs and a fourth that is not its own.** Pausing the element, clearing the queue and aborting the controller cover everything the page controls. They do not cover a chunk that is *mid-`play()`* when the abort lands, nor iOS Safari's tendency to keep an `<audio>` alive after `src=""`. The live test asserts all three effects and the audible gap between click and silence is a manual-smoke measurement; if it proves laggy the fix is Web Audio (`AudioBufferSourceNode.stop(0)`), which is a playback rewrite and deliberately not pre-built.
- **The button is per tab, not per session.** Two tabs, one with voice on: the on-tab speaks what it asked, the other stays silent. Correct, and it matches the follow-cascade rule — but it means the `localStorage` key is a *default for new tabs*, not a live shared state. Worth one line in the docs.
- **`/narrate` is an unbounded model request from the network**, which is a worse thing to expose than `/speak`'s bounded CPU: it costs inference time on whatever the session's model is, and on a remote engine it costs money per call. Bounded by `max_answer_chars`, `script_max_tokens`, `script_timeout` and `_guard`, and by `script: auto` skipping most turns. Still, on a non-loopback bind this is a route that spends tokens for anyone holding the token — see "Two-machine setups", whose warning now applies to inference as well as to `exec`.
- **`/speak` is an unbounded CPU request from the network.** Bounded by `max_chars` and by `_guard`, and synthesis is off the event loop. A hostile client with the token can still make the core work; it already has `exec`, so this adds no class of harm — but it is why the route takes `_guard` and why `max_chars` is enforced server-side rather than trusted to the page.
- **The `auto` gate is a heuristic with a blind spot.** It tests for a fence, a `|`-led row and a math delimiter. An answer that explains a table in prose and contains no table is under 280 characters and gets read aloud — which is correct. An answer that is 300 characters of prose with one inline `$x$` gets the pass — also correct. What it cannot see is an answer that *needs* presenting without any of those markers: a 300-character wall of nested clauses. That one gets a rewrite it may not need, costing 5 s. The failure mode is latency on a rare shape, not a wrong script, which is the right way to be wrong.
- **Autoplay inside an iframe or a sandboxed context** may refuse even after a keypress. The WebUI is not served in an iframe today; if it ever is, voice is the feature that notices first.
- **Firefox and Safari** are untested — every measurement here is Chromium. The autoplay rules are similar and the `blob:` + `<audio>` path is standard, but "it works in Firefox" is not a claim this plan has evidence for.
- **`voices/` is gitignored** — a fresh core answers `available: false` with a reason naming the download, rather than failing at first speech.
