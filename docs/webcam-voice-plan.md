# WebCam + Voice module — plan

Status: **planned** (2026-10-08). Hardware and software paths verified on this
machine; nothing built yet.

Goal: letsClaw sees and talks.

1. **Voice control** — "Hey Dudu, …" (or a PTT key) → transcribed → submitted
   to the core as a normal turn; the answer is spoken back.
2. **See** — the camera's motion trigger hands a frame to the model, which
   looks at it and decides what to do.
3. **Presence greeting** — Ryan walks into the office, the camera trips,
   letsClaw looks, recognises him, and says hi.

## What already exists (verified 2026-10-08)

### Hardware (this box, 192.168.203.75)

| | |
|---|---|
| Camera + mic | Logitech Brio 100 — `/dev/video0`, MJPG 1920×1080, USB mic (mono) |
| Mic source (Pulse) | `alsa_input.usb-046d_Brio_100_2551APP031R8-02.mono-fallback` |
| GPU | RTX 5090 32 GB — **llama.cpp `qwen38-local` holds ~27.4 GB of it** (97% util while
  serving). Only ~3 GB free. **The module never loads anything on the GPU** — see
  the STT note below. The vision model is remote (SGLang on .132), so that side
  never touches this GPU either. |
| Audio out | HDMI (NVidia + Generic HDMI sinks) and ALC897 analog |

### CamControl (`~/CamControl`) — the camera side, already done

Bash + ffmpeg + v4l2-ctl rig, tested on the Brio 100. **The module does not
re-implement any of this** — it drives it as a subprocess and reads its
output. Key facts:

- `run.sh -motion-only` (hot mode, the default): a full-res stream stays open
  and ffmpeg holds the newest frame's JPEG in `/dev/shm`; a trigger saves it
  by file move — **sub-second, and the frame is the scene at the trigger
  moment**. The stream runs in bounded 600 s windows; each reopen is blind
  for ~3 s (warmup).
- Motion decision: `tools/motion_watch.py` — a frame counts when ≥
  `MOTION_SENS` % (default 2) of pixels changed against **both** previous
  frames (noise floor 12, 2 consecutive frames), so a lamp switching on or a
  sensor glitch does not trip it. `MOTION_COOLDOWN_SEC=5` spaces frames.
- Frames land in `sessions/<ts>/frames/<wallclock>.jpg`;
  `sessions/current` → the active session. `bin/status`, `bin/stop`,
  `bin/cameras`, `run.sh -ss` for a still.
- **The camera device is exclusively owned** — nothing else can open it while
  the watcher runs (no concurrent users, no "LED trick" — see risks).
- Verified live on this box: a 40 s motion-only session produced two trigger
  frames.

### The "dudu" stack (`~/sherpa-tts-env`, `~/sherpa-tts-models`, `~/wakeword_data`)

Built for a *different machine* (solarisdaq): it posts to Discord through
`openclaw` (not installed here) and needs `sherpa_onnx` (installed nowhere
here). It cannot run as-is. **Salvageable and used by this plan:**

- `~/wakeword_data/hey_dudu_model/hey_dudu.onnx` — a trained "Hey Dudu"
  wake-word model: 39-dim MFCC input → sigmoid, threshold 0.7. The training
  mic was also a Brio 100, so it should transfer.
- The MFCC extraction (scipy, 25 ms frames / 10 ms hop / 40 mel / 13 coeff +
  mean/std/delta-mean) in `dudu-wake-loop.py` — copied, not sourced.
- The loop shapes from `dudu-wake-loop.py` / `dudu-ptt.py`: 1 s audio chunks,
  skip chunks below `SILENCE_RMS=0.005`, on wake speak "Yes?" then listen
  6 s; PTT = hold Pause/Break (evdev) → record → release → transcribe.
- Presence idea: periodic face check writing a flag (was OpenCV Haar,
  disabled there) — **superseded**: the vision model is the face detector.

### Audio that works here (tested end-to-end 2026-10-08)

- **TTS**: `piper-tts` 1.8.0 in `letsClaw/.venv`, voice
  `en_US-lessac-medium` downloaded to `voices/`. `echo … | piper -m … -f out.wav`
  → `aplay` → heard. (sherpa-onnx, which dudu-speak used, is not installed;
  piper is the TTS.)
- **STT**: `faster-whisper` in `letsClaw/.venv`; `large-v3` **already in the
  HF cache** (dudu's scripts warmed it). Round trip verified: TTS
  "Hey, I am in the office now." → whisper → exact text.
  **Runs on CPU, not the GPU**: `device='cpu', compute_type='int8'`
  transcribes the 3 s clip in ~4.5 s wall (including a 3.4 s model load), and a
  6 s PTT utterance lands well under the time a human takes to finish talking.
  The GPU is the LLM's (llama.cpp holds ~27.4 GB of 32); large-v3 fp16 would fit
  in the remaining ~3 GB only by a hair and would fight the model for VRAM
  bandwidth while it serves. CPU costs nothing that is not already spent.
  **Gotcha**: `av` 19.0.1 breaks faster-whisper's file-path decode
  (`metadata_errors` kwarg); `av<15` won't build on Python 3.14. Workaround:
  read the WAV with `wave` + numpy and pass the **array** to `transcribe()`.
- Mic verified: 5 s `arecord` from the Brio source, sane levels.

### Vision (tested 2026-10-08)

- **SGLang** `qwen3.8-27b-sglang-dual` @ `192.168.203.132:8888` accepts OpenAI
  multimodal content parts — a 1×1 test image came back with
  `image_tokens: 64`. That is the vision model.
- Local llama.cpp `qwen38-local` is **text-only**: server started with
  `--no-mmproj` and no mmproj file exists. (A Qwen-VL GGUF + mmproj would fix
  this later; not needed now.)
- The core's main turn path is `engine.chat_with_tools()` — the OpenAI chat
  endpoint — so multimodal content parts flow through the engine **unchanged**.
  Only the core's bookkeeping assumes string content (see Phase 1).

### Core protocol (the seam the module plugs into)

- WS `ws://127.0.0.1:8770/ws?session=<name>&token=<t>&reasoning=0`;
  client sends `{"t":"submit","text":…,"origin":…}`; events
  `turn_start / text / turn_end / notice / busy / error`.
- Attaching with `?model=<name>` creates the session on that model — how the
  office session gets the vision model without touching anyone else's.
- `source/discord_client.py` is the template: a thin client, one socket, no
  conversation state, reattaches by name.
- Token is `core.token` in `config.yaml` (gitignored).

## Design

### Shape

A new thin client, `source/cam_client.py`, started by `run_cam.sh` alongside
the core — exactly the Discord client's shape. It owns three loops and one
socket:

```
                 ┌─────────────── core (existing) ───────────────┐
 wake word ──┐   │  session "office"  (model: qwen38-dgsSpark)   │
 PTT key ────┼─whisper─submit──────────────►  turn  ──► turn_end ─┼──► piper TTS
             │   │                                                 │
 motion frame┴─vision turn (image + text)─────────────────────────┘
    ▲
 CamControl run.sh -motion-only   (subprocess, started/stopped by the client)
```

- **One office session** for both voice and camera. It is a normal session —
  visible in the WebUI, steerable, stoppable. Motion turns arrive with
  `origin: "cam"`, voice turns with `origin: "voice"`. If the session is busy
  (Ryan is mid-conversation), a motion submit is refused `busy` and dropped —
  not queued, matching the core's one-turn-at-a-time rule.
- The client holds no conversation state; a restart reattaches by name and
  replays by seq.

### Voice loop

1. 1 s chunks from the Pulse mic source (16 kHz mono, `arecord` subprocess or
   `sounddevice` — decision: `arecord` subprocess + `wave`/numpy, same
   pattern as dudu, no new C dep).
2. RMS gate (`0.005`) → MFCC (copied from dudu-wake-loop) → `hey_dudu.onnx`
   → score ≥ 0.7 = wake.
3. Wake: speak "Yes?" (piper), wait for it to finish, record 6 s,
   transcribe (faster-whisper `large-v3`, **CPU int8**, VAD on), submit
   non-empty text with `origin: "voice"`.
4. PTT (optional, config-gated): hold Pause/Break records, release transcribes
   and submits — same tail, no wake word.
5. On `turn_end` of a turn the client started (voice or cam), speak the
   answer with piper: strip markdown/emoji/code, cap length, sentence-chunk
   the WAV playback. A turn whose text is the NO-RESPONSE marker (below) is
   not spoken.

### Camera loop

1. At client start (config `cam.enabled: true`), spawn
   `~/CamControl/run.sh -motion-only -b` and remember the session dir.
   `bin/stop` on client exit.
2. Poll `sessions/current/frames/` for new files (1 s cadence; filenames are
   wall-clock, so "new" is monotonic).
3. New frame → **vision turn**: submit to the office session
   `{"t":"submit","text":"[camera] motion at <time>","content":[{text…},{image_url…}]}`.
   The text part carries the standing instruction: *"The office camera just
   detected movement; here is the frame. If it is Ryan, greet him in one or
   two short sentences. If it is not a person (lighting, pet, empty room),
   reply exactly NO-RESPONSE."*
4. **Greeting cooldown**: the client tracks the last spoken greeting and
   suppresses new vision turns for `cam.greet_cooldown_min` (default 10)
   minutes — motion frames keep arriving, but the model is only consulted
   when a greeting is actually allowed. (The frame itself is never lost; it
   stays in the session dir.)
5. NO-RESPONSE answers are not spoken and not shown as a chat line in the
   WebUI? — **decision: shown** (the WebUI should show what the camera did;
   only speech is suppressed).

### Core changes (Phase 1 — the only core touch)

Everything else is a new client plus config. The core gets exactly one
capability: **a submit may carry multimodal content**.

- `server.py` `submit` handler: accept optional `content` (a list of content
  parts). `text` remains required as the **display** string (it feeds
  `turn_start`, the journal, the WebUI); `content` is what goes into history.
  `content` absent → `{"role":"user","content":text}` as today, so every
  existing client is untouched.
- `core.py`: `start_turn/run_turn/_turn` take `content=None` (default `text`);
  history gets `content`, `jot()` and `turn_start` keep `text`.
- `token_counter.py`: `count_content` must handle list content — sum
  `count_text` over the text parts, and charge a flat ~350 tokens per image
  part (Qwen-VL-class image token counts run 64–1200 depending on size; the
  estimate only feeds the ⚡ line and rollover headroom).
- Tests: token counter with list content; a scratch-core test that submits
  `content` and asserts the history shape + `turn_start.text` stays the
  display string.

### Config

New `cam:` section in `config.yaml` / `config.example.yaml` (the line-level
config editor already handles unknown sections):

```yaml
cam:
  enabled: false               # run_cam.sh refuses to start without this
  session: "office"            # core session name
  model: "qwen38-dgsSpark"     # vision model the office session is created on
  camcontrol: "~/CamControl"
  motion:
    enabled: true
    greet_cooldown_min: 10
  voice:
    wake: true                 # "Hey Dudu" loop
    wake_threshold: 0.7
    wake_model: "~/wakeword_data/hey_dudu_model/hey_dudu.onnx"
    ptt: false                 # Pause/Break push-to-talk
    listen_sec: 6
    whisper: "large-v3"
    mic_source: "alsa_input.usb-046d_Brio_100_2551APP031R8-02.mono-fallback"
  tts:
    enabled: true
    voice: "voices/en_US-lessac-medium.onnx"
    speed: 1.0
    gain: 1.0
    device: "default"          # aplay device; e.g. "plughw:0,3" for HDMI
```

Restart-only like the Discord section: read at client start, no hot reload.

### Dependencies

Add to `requirements.txt`: `piper-tts`, `faster-whisper`, `onnxruntime`,
`evdev`, `scipy`. (`numpy` already present; `av` stays as-is with the
array-input workaround — pinning `av<15` fails to build on Python 3.14.)

## Build order

| phase | deliverable | test |
|---|---|---|
| 1 | core multimodal submit + token counter | unit + scratch-core test (no camera, no network) |
| 2 | `cam_client.py` skeleton: attach, TTS on turn_end | speak a canned turn_end |
| 3 | voice: wake loop + PTT → whisper → submit | TTS→mic→whisper round trip; wake on a recorded "Hey Dudu" |
| 4 | camera: CamControl lifecycle + frame poller → vision turn | synthetic frame dropped in `frames/` triggers a turn (vision model = live SGLang; mark `LIVE` like `test_discord_live.py`) |
| 5 | config, `run_cam.sh`, README section | full pass: steering/browser/settings baselines unchanged |

Phase 1 is independently shippable (the WebUI could grow an image-attach
button on the same seam) — but nothing else does until it lands.

## Risks and open edges

- **Greeting latency.** The office session's model is a *thinking* 27 B on
  SGLang; a "who is that?" round may spend 10–30 s of reasoning before the
  greeting is out. Mitigations, in order: keep the vision prompt tight (one
  or two sentences, NO-RESPONSE escape); if it still lags, point `cam.model`
  at a faster entry or add one with `reasoning: false`. Not solved on paper —
  measure in Phase 4.
- **GPU is the LLM's.** llama.cpp runs `qwen38-local` on the same RTX 5090
  and holds ~27.4 GB of 32 GB, 97% util while serving. Nothing in this module
  may load on it: whisper is CPU int8 (measured: a 3 s clip transcribes in
  ~4.5 s including load — plenty for a 6 s utterance), the wake word is
  onnxruntime CPU, and piper TTS is CPU. The only GPU consumer in the whole
  pipeline is the *remote* vision model, on another machine. If the GPU is
  ever less loaded, `cam.voice.whisper_device: cuda` is a config knob, not a
  redesign — but the default stays CPU.
- **Wake word transfer.** The ONNX was trained on solarisdaq's Brio 100 mic;
  this is the same hardware class, but gain/room differ. The 0.7 threshold is
  a starting value, not a guarantee — Phase 3 records a few local "Hey Dudu"
  samples and tunes the threshold against them.
- **LED / privacy.** Hot mode keeps the camera streaming (LED on) for the
  whole watch window. That is the price of sub-second trigger frames; it is an
  office camera pointed at a desk. Noted, accepted.
- **Disk.** Motion-only frames are never deleted (~220 kB each at 1080p). A
  busy day is small (the differ is conservative), but a `cam:` note in the
  README should point at `du -sh ~/CamControl/sessions` for long runs.
- **Camera contention.** If the user starts their own CamControl session
  (time-lapse etc.), the client's watcher cannot open the device and its
  windows fail. The client logs that clearly and keeps the voice loops alive.
- **Brio mic is mono 16 kHz** — fine for whisper, nothing to do.
- **`av` 19.0.1 / faster-whisper file decode is broken** on Python 3.14 —
  always pass numpy arrays to `transcribe()`; a comment in the code says why.

## Explicitly not doing

- Re-implementing motion detection in Python (CamControl's differ is better
  and already tuned; the camera is exclusively owned anyway).
- OpenCV face detection as a pre-filter (the vision model does it; one less
  heavy dep and one less thing to tune).
- Continuous video streaming to the model (frame-per-trigger is the feature;
  a video feed would burn the SGLang server and the network for nothing).
- Porting dudu's Discord channel-switching (that belongs to the Discord bot,
  which already exists).
- A WebUI camera view (the WebUI shows the *conversation* the camera
  produces; a live stream is a different project).
