# AGENT.md — setup for an agent

A short, imperative path from a fresh clone to a running, verified letsClaw core.
The **README.md** is the deep reference (philosophy, the reasoning model, every
config key); this file is the *do this, then check this* path and the traps.
When you need the why, or a key not covered here, go to the README section named.

## What it is

A low-energy agent engine: a Python core (`source/`) that owns the conversations,
plus three thin clients that are just windows onto it — a terminal client
(`source/chat.py`), a WebUI served by the core (no build step), and an optional
Discord bot (`source/discord_client.py`). **Nothing works without the core running.**

## Bootstrap (fresh clone)

```bash
cd ~/letsClaw

# 1. Interpreter + dependencies. All Python deps live in .venv, never system-wide.
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
#    required: aiohttp, openai, PyYAML   optional: tiktoken (token counts),
#    discord.py (bot only). See requirements.txt for which is which.

# 2. Config. config.yaml is gitignored — it does not exist yet.
cp config.example.yaml config.yaml
#    then edit config.yaml (do NOT edit the example):
#      - models.<name>.base_url  REQUIRED per entry — the core boots without it
#                            (engines build lazily) but the first turn fails
#      - models.default_model    which model a new session starts on
#      - core.bind / core.port / core.token   see "Security" below
#      - discord.*               omit or set enabled:false if you have no bot

# 3. Start the core. Leave it running.
./serve.sh
```

`serve.sh` also starts the Discord bot when `discord.enabled` is not false **and**
`discord.token` is set; it prints a note and carries on if either is missing.
Ctrl-C stops both. The core and the bot are separate processes; the bot is
optional.

## Verify it is up

```bash
curl -s http://127.0.0.1:8770/health
# -> {"ok": true, "proto": <int>, "sessions": 0}
```

`/health` needs no token. The core **boots even if no model server is reachable** —
engines are constructed without I/O, so a wrong `base_url` only bites on the first
turn, not at boot. A green `/health` means the core is up, not that a model works.
To confirm a model, open a client and send a message.

## Security (read before exposing it)

- Default `core.bind` is `127.0.0.1` (loopback) — reachable from this machine only.
- The agent tools are **unconfined**: anyone who can reach the port gets a shell on
  this host. So if you change `bind` to anything non-loopback (e.g. `0.0.0.0` for a
  LAN), you **must** set a `core.token` too. The core prints a warning on non-loopback.
- Across the internet, do not just open the port: put it behind a VPN or a TLS+auth
  reverse proxy.
- `config.yaml` holds the bot token and is gitignored for exactly this reason.

## Tests

Run from the repo root with the venv interpreter. The unit tests need no core.
The two CDP browser tests (`steering_test.py`, `settings_test.py`) each start
their own scratch core on a temp config and a temp state dir, so they never
touch your real `config.yaml` or `state/` — run them any time. `browser_test.py`
is different: its main flow runs against the **real** core (the `CORE` env var,
default `http://127.0.0.1:8770`), so have `./serve.sh` up first; only its
token-gate sub-scenario spawns a scratch core. All three need a headless
Chrome on the box.

```bash
.venv/bin/python tests/test_chunk.py        # chunker (no core needed)
# ... the other tests/test_*.py are the unit suite (see tests/)

# End-to-end over CDP:
.venv/bin/python web/steering_test.py       # own scratch core — safe anytime
.venv/bin/python web/settings_test.py       # own scratch core — safe anytime
.venv/bin/python web/browser_test.py        # main flow hits the REAL core: ./serve.sh first
```

`web/*_test.py` are tracked in git; `tests/` is gitignored (a local suite).

## Repo layout (where things live)

```
source/core.py           the core: sessions, turns, rollover, the journal
source/llm_engine.py     the OpenAI-compatible chat client + the reasoning pad
source/server.py         the aiohttp WS/HTTP server, serves the WebUI
source/chat.py           the terminal client
source/discord_client.py the Discord bot (optional)
source/chunker.py        fence-aware answer chunking (shared by the clients)
web/                     the WebUI (vanilla JS, no build) + its CDP tests
models/base.md           the base behavior file (committed)
models/sessions/*.md     per-session behavior + archived transcripts (gitignored)
config.example.yaml      the documented config template (committed)
config.yaml              your config (gitignored)
state/                   live conversations, archives, memory (gitignored)
```

## Gotchas that will bite an agent

- **`config.yaml` is not in git.** A fresh clone has none. Copy the example first.
- **Every model entry needs `base_url`** or the core refuses to build that engine.
- **The venv does not survive a distro Python upgrade** — the packages stay in
  `.venv/lib/python3.<old>`. Symptom: a bare `ModuleNotFoundError`. Delete `.venv`
  and rebuild; nothing else is lost.
- **`ruamel.yaml` is not a dependency.** Only `PyYAML`. The core edits `config.yaml`
  line-by-line (not a YAML round-trip) specifically so it does not destroy the
  file's comments — do not "improve" a save into `yaml.safe_dump`.
- **Restart-only keys** (`core.bind`, `core.port`, `logging.file`, `discord.enabled`)
  need a `./serve.sh` restart; everything else is picked up by `/reload` live.
- **The core holds the conversations.** Closing a client never ends a session; the
  session lives in `state/` and is reloaded on the next core start.

## Pointers into the README

- Config keys in full: **README → Config** (and `config.example.yaml`, which is the
  most commented source of truth).
- How a turn/round/reasoning pad works: **README → Terminology** and
  **→ Reasoning: the Notepad and the Answer Sheet**.
- Rollover and the journal: **README → Context Rollover**.
- Per-client detail (terminal, WebUI, Discord): the matching **README** section.
