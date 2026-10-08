#!/usr/bin/env python3
"""End-to-end check of the WebUI Settings panel.

Drives a scratch core (with core.token set, so the auth paths are exercised)
and a real browser over CDP, the same way steering_test.py does. The model
does not have to be reachable — no turn is ever started:

    .venv/bin/python web/settings_test.py

The scratch core runs from a COPY of the real config.yaml pointed at a temp
state dir, so the panel's Save button writes the copy — never the real file.

What it proves:
  1. GET/POST /config need the token; GET returns the live config dict.
  2. The panel renders every top-level section; webui is open and editable,
     the rest is read-only; secrets are masked behind a reveal; the
     restart-only keys (core.bind, core.port, discord.enabled) carry a badge.
  3. Saving webui rewrites only that block — every comment and every other
     section of the file survives — and re-themes the live tab without a
     page reload.
  4. A /reload from elsewhere redraws an open panel.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
import yaml

ROOT = Path(__file__).resolve().parent.parent
PORT = 8901
TOKEN = "sekrit-settings"
os.environ["CORE"] = f"http://127.0.0.1:{PORT}"   # before the import: browser_test reads it

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_test import check, open_tab, results  # noqa: E402

CORE = os.environ["CORE"]
PY = str(ROOT / ".venv" / "bin" / "python")
TMPCFG = None   # set by main(), read by run(): the file the scratch core serves


async def wait_health(http, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            async with http.get(f"{CORE}/health") as r:
                if r.status == 200:
                    return True
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.25)
    return False


async def run(http):
    sess = f"settings{int(time.time()) % 100000}"
    tab = await open_tab(http, f"{CORE}/?session={sess}")
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        # --- the gate, since this core wants a token --------------------------
        check("the token gate appears",
              await tab.until("!document.querySelector('#gate').hidden", 10))
        await tab.js("(() => { const i = document.querySelector('#gate-token');"
                     f" i.value = {json.dumps(TOKEN)};"
                     " document.querySelector('#gate-form').requestSubmit(); })()")
        check("page connects after the token",
              await tab.until("document.querySelector('#status').textContent === 'connected'", 20),
              await tab.js("document.querySelector('#status').textContent"))

        # --- HTTP contract ----------------------------------------------------
        async with http.get(f"{CORE}/config") as r:
            check("GET /config without the token is 401", r.status == 401, str(r.status))
        async with http.get(f"{CORE}/config", headers=auth) as r:
            j = await r.json()
            check("GET /config returns the live config",
                  r.status == 200 and isinstance(j.get("config"), dict)
                  and set(j["config"]) >= {"core", "models", "webui"},
                  str(sorted(j.get("config") or {})))
            check("GET /config names the file", "config.yaml" in j.get("path", ""))
        async with http.post(f"{CORE}/config", json={"webui": {"theme": "dark"}}) as r:
            check("POST /config without the token is 401", r.status == 401, str(r.status))
        async with http.post(f"{CORE}/config", json={"nope": 1}, headers=auth) as r:
            check("POST /config refuses a body without webui", r.status == 400, str(r.status))

        # --- the panel ---------------------------------------------------------
        check("settings button sits at the bottom of the sidebar",
              await tab.js("!!document.querySelector('#side-foot #side-settings')"))
        await tab.js("document.querySelector('#side-settings').click()")
        check("panel opens", await tab.until("!document.querySelector('#settings').hidden", 10))
        check("panel shows the config path",
              await tab.until("document.querySelector('#set-path').textContent.includes('config.yaml')", 10),
              await tab.js("document.querySelector('#set-path').textContent"))

        for key in ("core", "models", "behavior", "tools", "conversation",
                    "memory", "logging", "webui", "discord"):
            check(f"panel renders section '{key}'", await tab.js(
                "[...document.querySelectorAll('#set-body details summary')]"
                + ".some(s => s.textContent.trim().startsWith(" + json.dumps(key) + "))"))

        check("webui section starts open, the rest closed",
              await tab.until("(() => { const open = [...document.querySelectorAll('#set-body details')]"
                              ".filter(d => d.open).map(d => d.querySelector('summary').textContent.trim());"
                              " return open.length === 1 && open[0].startsWith('webui');"
                              " })()", 10),
              await tab.js("[...document.querySelectorAll('#set-body details')].filter(d => d.open)"
                           ".map(d => d.querySelector('summary').textContent.trim()).join(',')"))

        check("webui offers a theme select",
              await tab.until("!!document.querySelector('#set-body select')", 10))
        r = await http.get(f"{CORE}/config", headers=auth)
        live_theme = (await r.json())["config"]["webui"]["theme"]
        check("the forced temp-copy theme is what the core serves",
              live_theme == "auto", live_theme)
        check("theme select holds the live value",
              await tab.js("document.querySelector('#set-body select').value") == live_theme,
              await tab.js("document.querySelector('#set-body select').value"))
        check("webui offers the four pin inputs",
              await tab.js("document.querySelectorAll('#set-body .cfg-edit input').length") == 4,
              str(await tab.js("document.querySelectorAll('#set-body .cfg-edit input').length")))
        check("only the webui section has a Save button",
              await tab.until("(() => { const btns = [...document.querySelectorAll('#set-body .set-save')];"
                              " return btns.length === 1 && btns[0].closest('details')"
                              " .querySelector('summary').textContent.trim().startsWith('webui');"
                              " })()", 10))

        # Secrets are masked; the eye reveals. The first one in the panel is
        # core.token — this core's own token, so the expected value is known.
        check("core.token is masked by default",
              await tab.until("(() => { const s = document.querySelector('#set-body .cfg-secret');"
                              " return s && s.querySelector('.dots') && s.querySelector('.cfg-val').hidden;"
                              " })()", 10))
        await tab.js("document.querySelector('#set-body .cfg-secret button').click()")
        check("reveal shows the real token",
              await tab.until("(() => { const s = document.querySelector('#set-body .cfg-secret');"
                              f" return s.querySelector('.cfg-val').textContent === {json.dumps(TOKEN)};"
                              " })()", 10))

        # Restart-only keys are badged; the live ones are not.
        await tab.js("(() => { for (const name of ['core', 'discord']) {"
                     " const d = [...document.querySelectorAll('#set-body details')]"
                     "  .find(d => d.querySelector('summary').textContent.trim().startsWith(name));"
                     "  d.open = true; } })()")
        check("core.bind, core.port and discord.enabled carry the restart badge, core.token does not",
              await tab.until("(() => { const badged = [...document.querySelectorAll('#set-body .cfg-row')]"
                              ".filter(r => r.querySelector('.badge'))"
                              " .map(r => r.querySelector('.cfg-key').firstChild.textContent.trim());"
                              " return badged.includes('bind') && badged.includes('port')"
                              " && badged.includes('enabled')"
                              " && !badged.includes('token');"
                              " })()", 10),
              await tab.js("[...document.querySelectorAll('#set-body .cfg-row')]"
                           ".filter(r => r.querySelector('.badge'))"
                           ".map(r => r.querySelector('.cfg-key').firstChild.textContent.trim()).join(',')"))

        # --- save: the file keeps its comments, the tab re-themes live ---------
        original = TMPCFG.read_text()
        await tab.js("(() => { const s = document.querySelector('#set-body select'); s.value = 'dark';"
                     " s.dispatchEvent(new Event('change')); })()")
        check("Save is enabled once the form differs from the file",
              await tab.until("!document.querySelector('#set-body .set-save').disabled", 10))
        await tab.js("document.querySelector('#set-body .set-save').click()")
        check("save reports what changed",
              await tab.until("(() => { const n = document.querySelector('#set-body .set-foot .set-notice');"
                              " return n.classList.contains('ok') && n.textContent.includes('webui.theme');"
                              " })()", 15),
              await tab.js("document.querySelector('#set-body .set-foot .set-notice').textContent"))

        fresh = yaml.safe_load(TMPCFG.read_text())
        check("config.yaml now says theme: dark",
              fresh.get("webui", {}).get("theme") == "dark",
              str(fresh.get("webui")))
        r = await http.get(f"{CORE}/config", headers=auth)
        live = (await r.json())["config"]["webui"]["theme"]
        check("the live config endpoint agrees", live == "dark", live)

        # The pin inputs round-trip too: transparency 0.5 must land in the file
        # and in the generated stylesheet the pinned prompt is styled from.
        await tab.js("(() => { const i = [...document.querySelectorAll('#set-body .cfg-edit input')]"
                     ".find(i => i.type === 'number'); i.value = '0.5'; i.dispatchEvent(new Event('input'));"
                     " document.querySelector('#set-body .set-save').click(); })()")
        check("the pin save reports what changed",
              await tab.until("(() => { const n = document.querySelector('#set-body .set-foot .set-notice');"
                              " return n.classList.contains('ok') && n.textContent.includes('transparency');"
                              " })()", 15),
              await tab.js("document.querySelector('#set-body .set-foot .set-notice').textContent"))
        fresh = yaml.safe_load(TMPCFG.read_text())
        check("pin.transparency saved to the file",
              fresh["webui"]["pin"]["transparency"] == 0.5,
              str(fresh.get("webui", {}).get("pin")))
        async with http.get(f"{CORE}/theme.css") as r:
            check("theme.css serves the new pin", "--pin-transparency: 0.5" in await r.text())

        # The pin's style must reach the ALREADY-OPEN tab, not just a fresh load:
        # a browser will not re-fetch the /theme.css <link> when the server's copy
        # changes, so the reloaded event is the live path (applyPin in app.js).
        # Without it, a save only took effect after an F5 — the pin "sometimes"
        # ignored the transparency while a turn was running.
        check("the live tab's pin follows the saved transparency without a reload",
              await tab.until("getComputedStyle(document.documentElement)"
                              ".getPropertyValue('--pin-transparency').trim() === '0.5'", 10),
              await tab.js("getComputedStyle(document.documentElement)"
                           ".getPropertyValue('--pin-transparency').trim() || '(unset)'"))

        # The page re-themed itself over the reloaded event — and no page
        # reload happened, because the gate's token survived it (a reload would
        # have shown the gate again until boot re-read localStorage).
        check("the live tab re-themed to dark",
              await tab.until("document.documentElement.dataset.theme === 'dark'", 10),
              str(await tab.js("document.documentElement.dataset.theme")))

        # An edit from *elsewhere* (a hand in an editor) plus a /reload: the
        # open panel must redraw against the new file, and the tab re-theme.
        # (A no-op reload deliberately emits nothing, so the file really does
        # have to change.)
        txt = TMPCFG.read_text()
        assert "theme: dark" in txt
        TMPCFG.write_text(txt.replace("theme: dark", "theme: light", 1))
        async with http.post(f"{CORE}/reload", headers=auth) as r:
            check("a plain /reload still works", r.status == 200, str(r.status))
        check("an open panel redraws after a reload",
              await tab.until("document.querySelector('#set-body select').value === 'light'", 10),
              await tab.js("document.querySelector('#set-body select').value"))
        check("the tab followed it",
              await tab.until("document.documentElement.dataset.theme === 'light'", 10),
              str(await tab.js("document.documentElement.dataset.theme")))

        # --- close, and prove the file kept its comments -----------------------
        await tab.js("document.querySelector('#set-close').click()")
        check("the close button hides the panel",
              await tab.until("document.querySelector('#settings').hidden", 10))
        after = TMPCFG.read_text()
        check("saving kept every comment and every other byte",
              after.count("#") == original.count("#")
              and original.split("webui:")[0] == after.split("webui:")[0])
    finally:
        await tab.close()
        async with http.delete(f"{CORE}/sessions/{sess}", headers=auth):
            pass


async def main():
    if not (shutil.which("google-chrome") or shutil.which("chromium")):
        print("no chrome found"); return 1

    # The real config.yaml, copied and patched: the panel must show a realistic
    # file with all nine sections and ~95 comment lines, and Save writes this
    # copy — the real config.yaml is never touched. Patching the text (not a
    # YAML dump) is what keeps those comments in place to be preserved.
    tmp = Path(tempfile.mkdtemp(prefix="letsclaw-settings-"))
    for d in ("sessions", "live"):
        (tmp / d).mkdir()
    global TMPCFG
    TMPCFG = tmp / "config.yaml"
    text = (ROOT / "config.yaml").read_text()
    import re as _re
    text = _re.sub(r"(?m)^  port: \d+", f"  port: {PORT}", text)
    # Whatever the real core.token is ("" on a fresh clone, a real secret on a
    # running box), the scratch core gets its own: the auth checks below expect
    # TOKEN, and the panel's reveal is tested against it. count=1 hits the core
    # section; the discord token further down is untouched.
    assert _re.search(r"(?m)^  token: ", text)
    text = _re.sub(r'(?m)^  token: .*$', f'  token: "{TOKEN}"', text, count=1)
    text = _re.sub(r"(?m)^  sessions_dir: .*", f'  sessions_dir: "{tmp}/sessions"', text)
    text = _re.sub(r"(?m)^  live_dir: .*", f'  live_dir: "{tmp}/live"', text)
    text = _re.sub(r"(?m)^  file: .*", f'  file: "{tmp}/log"', text)
    text = _re.sub(r"(?m)^  theme: \S+", "  theme: auto", text)
    cfg = yaml.safe_load(text)   # the patches must not have broken the file
    assert cfg["core"]["port"] == PORT and cfg["core"]["token"] == TOKEN
    assert len([l for l in text.splitlines() if "#" in l]) > 50
    TMPCFG.write_text(text)

    chrome = None
    async with aiohttp.ClientSession() as probe:
        try:
            async with probe.get("http://127.0.0.1:9222/json/version") as r:
                if r.status != 200:
                    raise aiohttp.ClientError
        except aiohttp.ClientError:
            chrome = subprocess.Popen(
                [shutil.which("google-chrome") or shutil.which("chromium"),
                 "--headless=new", "--remote-debugging-port=9222",
                 f"--user-data-dir={tmp / 'chrome'}",
                 "--no-first-run", "--no-default-browser-check", "--disable-gpu",
                 "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(60):
                try:
                    async with probe.get("http://127.0.0.1:9222/json/version") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.25)
            else:
                print("chrome never opened the debug port"); return 1

    proc = subprocess.Popen([PY, "source/server.py", "--config", str(TMPCFG)],
                            cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            if not await wait_health(http):
                print("scratch core never came up"); return 1
            await run(http)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if chrome:
            chrome.terminate()
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed"
          + (f" — FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
