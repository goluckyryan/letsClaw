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
#    required: aiohttp, openai, PyYAML, tiktoken (token counts — there is a
#    cruder fallback, but the rollover calibration assumes tiktoken)
#    optional: discord.py (bot only). See requirements.txt for which is which.

# 2. Config. config.yaml is gitignored — it does not exist yet.
cp config.example.yaml config.yaml
#    then edit config.yaml (do NOT edit the example):
#      - models.<name>.base_url  REQUIRED per entry — the core boots without it
#                            (engines build lazily) but the first turn fails
#      - models.default_model    which model a new session starts on
#      - core.bind / core.port / core.token   see "Security" below
#        (when a token is set: the WebUI asks for it in a gate, the terminal client
#         takes --token / CLAW_TOKEN / the local config's own core.token, and the
#         bot reads core.token from the config it loads)
#      - discord.*               omit or set enabled:false if you have no bot

# 3. Start the core. Leave it running.
./serve.sh
```

`serve.sh` also starts the Discord bot when `discord.enabled` is not false **and**
`discord.token` is set; it prints a note and carries on if either is missing.
Ctrl-C stops both. The core and the bot are separate processes; the bot is
optional.

Neither shell script works that out for itself. Both run
`source/discord_probe.py` and read the verdict off its exit code — `0` start it,
`2` disabled, `3` no token, `4` no config yet, and `1` meaning the probe itself
raised and the traceback is on stderr. `install.sh` asks the same question to
decide whether to install `discord.py`. If you change what "the bot is wanted"
means, change it there and nowhere else: the two scripts used to hold two copies
of this and the copies drifted, until `install.sh` was reporting every uncaught
exception as "no token" and skipping `discord.py` on a fresh install.

## Verify it is up

```bash
curl -s http://127.0.0.1:8770/health
# -> {"ok": true, "proto": <int>, "version": "1.0.0", "sessions": 0}
```

`/health` needs no token. The core **boots even if no model server is reachable** —
engines are constructed without I/O, so a wrong `base_url` only bites on the first
turn, not at boot. A green `/health` means the core is up, not that a model works.
To confirm a model, open a client and send a message.

The suites live in `tests/`, all of them, and `tests/run_all.py` runs them — but
**`tests/` is gitignored and stays that way**, so the suite exists on the machine
it was written on and nowhere else. If you are reading this in a clone and
`tests/` is missing, that is why, and it is deliberate. The two gates below are a
local discipline, not something a fresh checkout or a CI job can enforce.

```bash
.venv/bin/python tests/run_all.py          # fast tier — gate a refactor on this
.venv/bin/python tests/run_all.py --full   # everything — gate a commit on this
```

Two consequences worth knowing before you change anything. A test you write is
invisible to `git diff`, `git status` and review, so a source fix lands tracked
while the test guarding it does not — write both, but do not expect the pair to
travel together. And `run_all.py` with no flags skips the entire browser tier, so
a green fast run says nothing at all about `web/app.js`; anything touching the
WebUI has to be re-run with `--full` and `CORE=`, or by calling the suite
directly with `LIVE_TESTS=1`.

The fast tier is hermetic: no network, no ssh, no browser, no spawned core.
`--full` adds the ones that need something from outside the process, and three
of those (`test_browser.py`, `test_image.py`, `test_math_render.py`) stay skipped
until you set `CORE=` as well — they create and delete sessions on whatever core
`CORE` names, and they no longer default to the live one, which is your real
conversations. The runner globs `tests/test_*.py` rather than keeping a list, so
a new suite is picked up with no edit.

## Security (read before exposing it)

- Default `core.bind` is `127.0.0.1` (loopback) — reachable from no *other* machine.
  It is **not** a wall against your own browser: any page you visit can open
  `ws://127.0.0.1:8770/ws`. Two headers stop that, and it takes both.
  The `Origin` check on `/ws` and on every state-changing route is the first —
  browsers always send `Origin` and cannot forge it, while the terminal and the
  bot send none and are unaffected. On its own it proves only that the browser
  agrees with itself, because it compares `Origin` to `Host` and `Host` is also
  chosen by the client: a page served from a domain whose A record has been
  re-pointed at `127.0.0.1` (DNS rebinding) sends a matching `Origin` and `Host`
  perfectly honestly, and sails through. So the second check pins the other half.
  **Every request except `GET /health` must carry a `Host` this machine actually
  answers to** — loopback, the bind address, the hostname and FQDN and the
  addresses those resolve to. An attacker's nameserver can point any name at us
  but cannot put that name in the allowlist. This one is a middleware and runs
  before routing, not only on the writes, because once a page is believed to be
  same-origin it can *read* `/sessions`, `/config` and whole conversations out of
  `/sessions/<name>/history`. Either refusal is a 403 and a log line.
- **`core.allowed_hosts`** (a list) extends that allowlist. You need it only when
  something in front of the core forwards a `Host` the core has never heard of —
  a reverse proxy. It is read per request, so `/reload` is enough.
- The agent tools are **unconfined**: anyone who can reach the port gets a shell on
  this host. So if you change `bind` to anything non-loopback (e.g. `0.0.0.0` for a
  LAN), you **must** set a `core.token` too — the core now refuses to start in that
  combination rather than warning about it. Its banner line shows the LAN address to
  type into a browser (e.g. `http://192.168.203.75:8770/`) instead of the untypeable
  `0.0.0.0`.
- `GET /config` redacts `core.token`, `discord.token` and every
  `models.<name>.api_key`; the token comparison is constant-time. `webui.pin`
  values are validated as CSS at save and at render, because `/theme.css` is
  generated and served unauthenticated.
- Across the internet, do not just open the port: put it behind a VPN or a TLS+auth
  reverse proxy.
- `config.yaml` holds the bot token and is gitignored for exactly this reason.

## Showing the user a picture

Write the file into `state/files/` (or whatever `webui.files_dir` says), then put
`![a short caption](thatfile.png)` in the answer — the WebUI fetches it from
`/files/` and renders it inline. Only a **flat filename with an image extension**
works: no subdirectory, no URL, no `data:`. The terminal and Discord clients show
the Markdown as text, so say where the file is in words too if it matters.

## Repo layout (where things live)

```
source/core.py           the core: sessions, turns, rollover, the journal
source/session.py        the disk layer: live store, archives, journals, handoffs
source/llm_engine.py     the OpenAI-compatible chat client + the reasoning pad
source/server.py         the aiohttp WS/HTTP server, serves the WebUI
source/config.py         config.yaml → engines; building them is the validation
source/behavior.py       the behavior-file cache and the two base.md directives
source/config_edit.py    saving the webui: block back into config.yaml as text
source/logging_setup.py  one rotating log file per writer (core, bot)
source/ssh_forward.py    supervised `ssh -N -L` forwards for remote model servers
source/chat.py           the terminal client
source/discord_client.py the Discord bot (optional)
source/discord_probe.py  "start the bot?" — serve.sh and install.sh both ask it
source/chunker.py        fence-aware answer chunking (shared by the clients)
source/version.py        `__version__` — the one place the release number lives
                         (**every commit bumps it**; see the docstring for why)
web/                     the WebUI (vanilla JS, no build)
tests/                   every suite and helper, whatever it tests (gitignored)
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
- **Restart-only keys** need the core restarted — `/restart` from any client, or
  `./serve.sh` again; everything else is picked up by `/reload` live.
  `/restart` re-execs the core in place: same PID, conversations kept, every
  client back in about a second. It refuses while a turn is running anywhere
  (`/restart force` overrides, and those turns are lost) and refuses outright
  when the config it would come back on will not load — that one is not
  overridable, because `Restart=on-failure` with no `StartLimitBurst` turns a
  core that exits 1 into a restart loop rather than a message. It restarts the
  **core only**: the bot is a sibling process started by `serve.sh` and keeps
  running, so an edit to `discord_client.py` still needs `serve.sh` itself.
  The keys are `core.bind` and `core.port` (the socket is already
  bound, and rebinding it drops every client); `logging.file`, `logging.max_bytes`
  and `logging.backups`, which `logging_setup.configure()` reads once when it
  installs the handlers — `logging.level` is *not* restart-only; and on the bot's
  side `discord.enabled` (`serve.sh` reads it before deciding to launch at all)
  along with `discord.token`, `discord.core` and `discord.core_token`, which a
  live bot deliberately will not re-point at another core on the strength of a
  reload event, and the bot's posting limits `discord.idle_detach_min`,
  `discord.max_messages`, `discord.max_lines`, `discord.show_tools` and
  `discord.auto_names`, which `Settings.__init__` parses once. A restart there means restarting the *bot*,
  which is a separate process from the core.
- **The Discord allowlist does reload.** `discord.users`, `mention_only` and
  `mention_list` are re-read by `Settings.refresh_security()` when the bot sees
  the core's `reloaded` event, and it fails closed — a `config.yaml` that will
  not parse keeps the allowlist already in force. An allowlist that cannot be
  narrowed without a restart is not an allowlist: before this, removing a
  snowflake and reloading printed "config reloaded" and changed nothing, and
  that person kept an unconfined shell here until the process was restarted.
- **The core holds the conversations.** Closing a client never ends a session; the
  session lives in `state/` and is reloaded on the next core start.

## Pointers into the README

- Config keys in full: **README → Config** (and `config.example.yaml`, which is the
  most commented source of truth).
- How a turn/round/reasoning pad works: **README → Terminology** and
  **→ Reasoning: the Notepad and the Answer Sheet**.
- Rollover and the journal: **README → Context Rollover**.
- Per-client detail (terminal, WebUI, Discord): the matching **README** section.
