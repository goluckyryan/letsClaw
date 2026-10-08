#!/usr/bin/env python3
"""Full-geometry trace of the tall-question auto-scroll drop.

Prints every event from turn_start with scrollTop / scrollHeight / clientHeight,
atBottom, and the rects (relative to the log's viewport) of the user question
and the newest reasoning block, so the mechanism is visible, not guessed.

    .venv/bin/python web/scroll_diag.py
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
PORT = 8917
os.environ["CORE"] = f"http://127.0.0.1:{PORT}"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_test import open_tab  # noqa: E402

CORE = os.environ["CORE"]
PY = str(ROOT / ".venv" / "bin" / "python")
TOKEN = None


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


PROBE = r"""
(async () => {
  const log = document.querySelector('#log');
  log.innerHTML = '';
  S.showReasoning = true;
  const lr = () => log.getBoundingClientRect();
  const rel = (el) => { const r = el.getBoundingClientRect(); const v = lr();
    return {top: Math.round(r.top - v.top), bot: Math.round(r.bot - v.top + (r.height - (r.bot-r.top)))}; };
  const gap = () => Math.max(0, log.scrollHeight - log.scrollTop - log.clientHeight);
  const atB = () => gap() < 80;
  const trace = [];
  const rec = (label, extra) => {
    const q = log.querySelector('.msg.user');
    const rb = [...log.querySelectorAll('.msg.reasoning .body')].pop();
    const row = {t: label, gap: gap(), top: log.scrollTop, h: log.scrollHeight, atB: atB(),
                 qTop: q ? Math.round(q.getBoundingClientRect().top - lr().top) : null,
                 qBot: q ? Math.round(q.getBoundingClientRect().bottom - lr().top) : null,
                 rBot: rb ? Math.round(rb.getBoundingClientRect().bottom - lr().top) : null};
    Object.assign(row, extra || {}); trace.push(row);
  };
  const orig = window.handle;
  window.handle = (e) => { const r = orig(e); rec(e.t); return r; };
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const step = async (e, w) => { window.handle(e); if (w) await sleep(w); };
  const R = 'The model reasons in a longish paragraph so the block grows and the log scrolls a little at a time. ';
  const question = 'Please do the following in detail:\n' + Array.from({length:8},(_,i)=>'  step '+(i+1)+': a long instruction that wraps').join('\n');

  for (let k = 0; k < 2; k++) {
    await step({t:'turn_start', text:'earlier ' + k}, 30);
    for (let i = 0; i < 5; i++) await step({t:'reasoning', delta: R}, 12);
    await step({t:'tool_call', id:'a'+k, name:'exec', arguments:'{"command":"ls -la"}'}, 30);
    await sleep(90);
    await step({t:'tool_result', id:'a'+k, size:700}, 30);
    for (let i = 0; i < 2; i++) await step({t:'text', delta: R}, 12);
    await step({t:'turn_end'}, 30);
  }
  log.scrollTop = log.scrollHeight; await sleep(50);
  rec('--- at bottom before test turn ---');

  await step({t:'turn_start', text: question}, 40);
  rec('  (after turn_start settle)');
  for (let i = 0; i < 6; i++) { await step({t:'reasoning', delta: R}, 15); rec('  reasoning'+i); }
  await step({t:'tool_call', id:'c1', name:'exec', arguments:'{"command":"cat /a/long/file"}'}, 50);
  await sleep(200);
  await step({t:'tool_result', id:'c1', size:1500}, 50);
  for (let i = 0; i < 2; i++) await step({t:'reasoning', delta: R}, 15);
  return JSON.stringify({clientH: log.clientHeight, trace});
})
"""


async def main():
    global TOKEN
    if not (shutil.which("google-chrome") or shutil.which("chromium")):
        print("no chrome found"); return 1
    tmp = Path(tempfile.mkdtemp(prefix="letsclaw-scroll-"))
    for d in ("sessions", "live"):
        (tmp / d).mkdir()
    text = (ROOT / "config.yaml").read_text()
    import re as _re
    text = _re.sub(r"(?m)^  port: \d+", f"  port: {PORT}", text)
    TOKEN = yaml.safe_load(text)["core"]["token"]
    text = _re.sub(r"(?m)^  sessions_dir: .*", f'  sessions_dir: "{tmp}/sessions"', text)
    text = _re.sub(r"(?m)^  live_dir: .*", f'  live_dir: "{tmp}/live"', text)
    text = _re.sub(r"(?m)^  file: .*", f'  file: "{tmp}/log"', text)
    (tmp / "config.yaml").write_text(text)
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
                 "--window-size=1280,520", "about:blank"],
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
                print("chrome never opened"); return 1
    proc = subprocess.Popen([PY, "source/server.py", "--config", str(tmp / "config.yaml")],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as http:
            if not await wait_health(http):
                print("core never came up"); return 1
            sess = f"sdgfull{int(time.time()) % 100000}"
            tab = await open_tab(http, f"{CORE}/?session={sess}")
            try:
                if await tab.until("!document.querySelector('#gate').hidden", 5):
                    await tab.js("(() => { const i = document.querySelector('#gate-token');"
                                 f" i.value = {json.dumps(TOKEN)};"
                                 " document.querySelector('#gate-form').requestSubmit(); })()")
                await tab.until("document.querySelector('#status').textContent === 'connected'", 20)
                d = json.loads(await tab.js(f"({PROBE})()"))
            finally:
                await tab.close()
                async with http.delete(f"{CORE}/sessions/{sess}"):
                    pass
            print(f"clientH={d['clientH']}\n")
            for row in d["trace"]:
                mark = "  " if row["atB"] else "!!"
                print(f"{mark} {row['t']:32} gap={row['gap']:>5} top={row['top']:>5} h={row['h']:>5} "
                      f"qTop={str(row['qTop']):>5} qBot={str(row['qBot']):>5} rBot={str(row['rBot']):>5}")
            return 0
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill()
        if chrome: chrome.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
