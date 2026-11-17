# Voice — the WebUI speaks its answers

The WebUI reads its answers aloud through the speakers of the machine the browser is on. The terminal and the Discord bot do not speak. No separate voice process exists: the core synthesizes, the browser plays.

Two rules set the shape of everything below:

- **Speech is a presentation, not a reading.** The answer on screen is complete and never changes. What is spoken can be a short script derived from it, which refers to "the table above" instead of reciting cells and skips an equation instead of saying "backslash frac".
- **Speech stops in the middle.** One stop kills the audio already playing, the parts still queued, and the requests still in flight.

## The pieces

| where | what |
|---|---|
| `source/voice.py` | the `voice:` config section, read and floored on every request (`settings`); whether the core can speak and why not (`status`, `info`); piper synthesis (`synthesize`); the cap on a script's length (`cap_script`) |
| `source/session.py` | `build_spoken_script` and its prompt `_SPOKEN_SYSTEM`, beside `build_turn_digest` |
| `source/server.py` | `POST /speak` (`speak`) and `POST /narrate` (`narrate`, engine choice in `_narrate_engine`) |
| `source/core.py` | the `voice` field of `/info` |
| `web/app.js`, section `voice: text` | `cleanForSpeech`, `spokenChunks`, `wantsNarration` — pure functions, no DOM |
| `web/app.js`, section `voice: playback` | the switch, the button, the queue, `stopSpeech` |
| `web/index.html` | `#btn-voice`, beside `#btn-digest` |
| `config.example.yaml` | every `voice.*` key, commented |
| `voices/` | the piper voice file (gitignored) |

## One answer, end to end

1. **`turn_end` in the tab that asked.** The answer is the raw markdown of the turn's last text element (`_raw`), never the rendered HTML: KaTeX spans and `<pre>` blocks have no sentence in them to read.
2. **Clean it.** `cleanForSpeech` drops code fences (closed or not), display and inline maths (`$$…$$`, `\[…\]`, `\(…\)`), pictures, URLs, emoji, table separator rows and markup; links keep their text; headings, bullets and table rows end in a full stop so the voice pauses there. A single `$` is left alone, because it is as often a price as a formula. Digits are left alone too: piper's phonemizer already reads `32 GB` correctly.
3. **Present or read?** `wantsNarration` decides from `voice.script`, on the **raw** answer:

   | `voice.script` | the page asks `/narrate` when |
   |---|---|
   | `always` | every answer |
   | `auto` (default) | the raw answer is at least `script_min_chars` (280) long, **or** contains a code fence, a `|`-led table row, or a math delimiter (`$`, `\(`, `\[`) |
   | `never` | never |

   The test reads the raw text because the cleaner deletes the very fences and pipes it looks for. An answer that is only code or only a table cleans to nothing, and that is exactly the answer a presenter can still say something about.
4. **`POST /narrate`** with the raw answer and the session name. A script comes back, or the page falls back to the cleaned text and says so once per session per page load. The answer is never left silent because the rewrite failed.
5. **Chunk it.** `spokenChunks` splits the text into parts of at most `max_chars` (400): at the last sentence end that fits, else the last clause boundary, else the last space; a single token longer than the cap is cut rather than dropped. At most `max_chunks` (8) parts are kept, and the page says "spoke 8 of 23 parts" when it stopped short. Chunking runs on the script, after the rewrite, so the presenter always sees the whole answer.
6. **`POST /speak`** once per part. The first is awaited; each next part is fetched while the current one plays, so playback never waits on the network at a seam.
7. **Play** through one `<audio>` element, one object URL at a time, each revoked before the next replaces it.

If both the rewrite and the cleaner produce nothing, the page shows `🔇 nothing to read` and sends no request.

## The core's two routes

Two routes rather than one, because they fail in opposite ways and the page has to survive each separately. `/narrate` is a model call: it can take seconds, time out, or come back wrong. `/speak` is pure CPU and has to stay fast. Folded together, a stalled model would hold a synthesis worker, and a route that needs only piper would also need a live inference server.

Both take `_guard` (token and same-origin): neither changes state, but each costs the core real work, so a cross-origin page must not be able to trigger it. Full status codes are in [protocol.md](protocol.md).

**`POST /speak {text}` → `audio/wav`.** Empty text is a 400. Text over `voice.max_chars` is a 400 and is **not** cut: the page is the only client and it chunks to that cap, so a longer request is a bug, and a silent cut would drop words without anyone noticing. Synthesis runs in `asyncio.to_thread`, so a turn never waits on someone's browser. A voice that is off, a missing piper or a missing voice file is a 503 whose reason names the fix.

**`POST /narrate {text, session}` → `{script}`.** The engine is `voice.script_model` if set, else the engine of the named session — the model that wrote the answer presents it, and its engine is already built — else `models.default_model`. The session is looked up with `mgr.sessions.get`, never `mgr.get()`, which would create one. The request takes no session lock and never enters history: `build_spoken_script` sends its own message list through `_summarise`, the same function the handoff and the digest use, so it inherits the no-thinking hint and its plain retry. Every failure is a non-200, so the page can tell "the rewrite failed" from "nothing to say". The rewrite is bounded by `max_answer_chars` (4000) in, `script_max_tokens` (300) out and `script_timeout` (12 s, then 504), and the script is cut to `max_chunks × max_chars` at a sentence end, so its tail is never dropped mid-sentence by the page's own cap.

The rewrite prompt is a module constant, not a behavior file: it is a mechanical instruction to re-render text, and a file in `models/` would invite editing the voice's identity when all anyone wants is different pacing. It carries three rules: never recite a table cell by cell; never read code, markup, LaTeX, paths or command lines as prose; keep every claim and number exactly as the answer states it. The last is the one about correctness rather than style, and it is why the temperature is 0.2.

## Can the core speak?

`/info` carries `"voice": {available, reason, max_chars, max_chunks, script, script_min_chars, max_answer_chars}`. `available` means `voice.enabled` is not false, piper can be found, and the voice file and its `.onnx.json` exist. All three are checked on every request because each is cheap — a dict read, `importlib.util.find_spec("piper")`, a stat — and a cached answer would outlive a `/reload` that names another voice, or the download that fixed a missing one. `find_spec` locates the package without importing it, so `/info` never loads onnxruntime. Piper is imported on the first `/speak` and the voice stays loaded for the life of the process; a core that never speaks never pays for either.

Every number the page obeys rides in that field, because the core's config owns them: a page that hardcoded 400 would send requests a core retuned to 200 refuses. The page re-reads it after every `/reload`.

`/health` does not carry it: it is unauthenticated.

## The switch, and who speaks

The on/off lives in the browser, in `localStorage` as `letsclaw.voice.<session>` — the shape of `letsclaw.sent.<name>`, for the same reason: what you want read aloud in one conversation has no business applying to another. The core holds no state for it, so `/voice` is handled in the page the way `/reasoning` is, and the terminal and the bot never see it.

**Only the tab that asked speaks.** `localStorage` is shared by every tab of an origin, so the switch alone would make two tabs on one session speak the same answer a beat apart. `turn_start` is a broadcast; the tab that sent the question has `S.weAsked` set, and `turn_start` copies it onto the turn as `S.turn.asked` before consuming it. `turn_end` speaks only when it is set. A turn started in another tab, in Discord or in the terminal is drawn everywhere and spoken nowhere. The stored switch is therefore a default for new tabs, not a live shared state.

## The button

| state | label | a click |
|---|---|---|
| off | `🔇 voice` | turns voice on for this tab's session |
| on | `🔊 voice` (lit) | turns it off |
| speaking | `⏹ voice` | stops this answer; the setting stays on |
| unavailable | `🔇 voice`, greyed | prints the reason from `/info` |

The label always ends in `voice`, so the header row never changes width, and it is labelled rather than icon-only because emoji fall back to tofu on a machine with no emoji font. `speaking` follows `S.speech`, not `audio.paused`: the gap between two parts is still speaking, and a lamp that flickered at every seam would lie. The unavailable state is a CSS class rather than `:disabled`, because a disabled button swallows the click that is supposed to explain it.

`/voice on`, `/voice off`, `/voice stop` and a bare `/voice` (toggle) go through the same functions as the button.

## Stopping

Stopping an answer and turning voice off are different acts: someone cut off by a long answer still wants the next one spoken. **Stop** lasts for this answer; **off** lasts until turned back on.

`stopSpeech` is the one stop. It reaches three places, and missing any one leaves sound playing after a click that looked like it worked:

1. **the element** — paused, its `src` removed and the element reloaded empty, so the buffer is dropped and nothing can resume; the object URL is revoked
2. **the queue** — parts not yet played, emptied
3. **the requests in flight** — the answer's `AbortController`, aborted, which cancels a `/narrate` still waiting and any `/speak` still downloading

The controller belongs to the spoken answer (`S.speech`), not to the turn: `turn_end` clears `S.turn` in the same handler that starts speech. A fresh controller per answer means an abort can never land on the next answer's requests.

Callers: the button in its `⏹` state, `/voice stop`, `/voice off`, a session switch, any new `turn_start` (barge-in), and Escape. **Escape** stops speech only when it closed nothing else: the overlay listener stops only if no panel was open and no `.lightbox` is in the DOM, and the composer's autocomplete and the sidebar's new-session box stop their own Escape from propagating, so closing either one leaves the voice alone.

## Measurements

Probed in headless Chrome over CDP with real input events, and against piper and the live models on this hardware.

| probe | result | what it decides |
|---|---|---|
| `<audio>.play()` with no input | rejected, `NotAllowedError` | a page cannot speak before any input |
| play after a trusted click; 1.5 s later from a timer | `playing`, `playing` | websocket-driven playback works once unlocked |
| play after a **keypress**; 8 s later | `playing`, `playing` | **typing the question is enough** — no click needed |
| `navigator.userActivation.hasBeenActive` after a keypress | `true`, and it stays true | the unlock is sticky |
| `speechSynthesis` voices in Chrome on Linux | 19, all `localService: false`, default `Google Deutsch` | browser TTS is Google's cloud — a private conversation would be posted to a third party, so piper on the core is the voice |
| piper in-process | load 0.7 s, ~136 MB resident; RTF ≈ 0.05 | load once, lazily, and keep it |
| piper CLI per call | ~1.0 s | why the core keeps the voice loaded rather than forking |
| piper, 743-char answer, one call vs 8 sentence calls | 1.77 s / 40.4 s audio vs 1.80 s / 40.3 s | chunking costs nothing in total |
| piper, first sentence of that answer alone | 0.17 s | speech starts in a fifth of a second when chunked |
| piper, 527-char script | 0.93 s synth → 31.6 s audio, 1394 KB WAV | what a presented answer costs on the wire |
| piper, `32 GB` as digits vs spelled out | 8.7 s vs 9.4 s audio, both correct | the prompt does not spend tokens spelling numbers |
| rewrite, local llama.cpp | 2.0–3.6 s, 76–112 tokens out | the latency `script: auto` avoids on short answers |
| rewrite, remote SGLang, no-thinking hint on / off | 5.4 s / 7.4 s with 302 reasoning tokens and no usable output | the hint is load-bearing — hence `_summarise` |
| rewrite of a table + formula + fence + path answer | "the table above compares…", "the formula provided", "the script shown" | the pass does what it is for |
| rewrite of a table-only answer | "compares two specific values" | where the cleaner returns nothing, the presenter still speaks |
| aiohttp `client_max_size` | 1 MB, on the request body | the request carries text; the WAV is the response, which has no such cap |

## Two machines

Nothing to configure. A browser on machine B pointed at `http://A:8770` plays on B's speakers, because that is where the browser is; the WAV crosses the network as the response to that one tab. The rewrite runs on A, where the engines are, so B needs no model and no piper. Its cost is paid on A's inference queue — the same one turns wait on — which is part of why `auto` skips short answers.

Reaching A from B means a non-loopback `core.bind`, and that makes `core.token` the only thing between the LAN and an unconfined `exec` tool, now also between the LAN and a route that spends inference. See Security in the README before setting it.

## Limits and open edges

- **The rewrite can be wrong.** It is a model restating another model's answer and can soften a caveat or round a figure. The answer on screen is complete and untouched, the prompt forbids altering what it states, and the script is short enough to check against the text. `voice.script: never` gives a verbatim reading.
- **Speech starts after the turn ends,** plus 2–5 s for the rewrite on the `auto` path. Speaking while streaming would start sooner, but a sentence cannot be split before the tokens after it have arrived, an unclosed fence reads as prose, and a presenter needs the whole answer for "the table above" to mean anything. Short plain answers skip the rewrite and start about a second after the turn ends.
- **A background tab that has not played sound yet waits.** Measured: with another tab in front, the `<audio>` element sits at `readyState 0` until its own tab is shown — Chrome defers media loading in background tabs. An answer asked in a tab you then leave can start when you come back; the button reads `⏹` meanwhile and stop still works. Whether a tab that has already played once is exempt is unmeasured.
- **Stop uses `<audio>`, not Web Audio.** pausing plus dropping the source is a stop. If it ever proves audibly laggy, `AudioBufferSourceNode.stop(0)` is the fix, at the cost of a decode step and a second playback path.
- **The `auto` test is a heuristic.** It sees fences, `|`-led rows and math delimiters. A 300-character wall of plain prose gets a rewrite it may not need, costing latency, never a wrong script.
- **Chromium only.** Every measurement here is Chrome. Firefox and Safari have similar autoplay rules and the `blob:` + `<audio>` path is standard, but neither has been tried.
- **No voice file ships.** `voices/` is gitignored; a fresh core reports `available: false` with a reason naming the download, rather than failing at the first answer.
