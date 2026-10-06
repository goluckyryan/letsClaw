#!/usr/bin/env python3
"""End-to-end check of /steering — a correction injected into a running turn.

Drives a scratch core and a real browser over CDP, the same way
browser_test.py does. The model must be reachable:

    .venv/bin/python web/steering_test.py

What it proves:
  1. /steering with no turn running is refused with a notice, nothing queued.
  2. /steering mid-turn interrupts the streaming round: the partial answer stays
     on screen, the interjection renders as a (pinned) user message, the
     model re-plans, and the turn ends cleanly with no error.
  3. The interjection is a real user message: a fresh attach replays it from
     the history, in order, exactly once.
  4. The raw protocol: the steering event carries turn_id and text, and lands
     before turn_end.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
import yaml

ROOT = Path(__file__).resolve().parent.parent
PORT = 8899
os.environ["CORE"] = f"http://127.0.0.1:{PORT}"   # before the import: browser_test reads it

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_test import check, open_tab, results, scratch_config  # noqa: E402

CORE = os.environ["CORE"]
PY = str(ROOT / ".venv" / "bin" / "python")

QUESTION = ("Write the numbers from 1 to 120, one per line, directly in your "
            "reply. Do not use any tools.")
STEERING = "STOP listing numbers. From now on the answer is just the word fixed."


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


async def type_and_send(tab, text):
    await tab.js(
        "(() => { const i = document.querySelector('#input');"
        " i.value = " + json.dumps(text) + ";"
        " i.dispatchEvent(new Event('input'));"
        " i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));"
        " return true; })()")


async def run(http):
    sess = f"steering{int(time.time()) % 100000}"

    # A raw observer on the same session, attached before the turn, so the
    # protocol-level checks do not depend on what the DOM happened to draw.
    ws = await http.ws_connect(f"{CORE}/ws?session={sess}")
    events = []
    async def observe():
        async for m in ws:
            if m.type is not aiohttp.WSMsgType.TEXT:
                continue
            e = json.loads(m.data)
            events.append(e)
            if e.get("t") == "turn_end":
                break
    obs = asyncio.create_task(observe())

    tab = await open_tab(http, f"{CORE}/?session={sess}")
    try:
        check("page connects to the core",
              await tab.until("document.querySelector('#status').textContent === 'connected'", 20),
              await tab.js("document.querySelector('#status').textContent"))

        # --- 1. idle: refused with a notice, nothing queued -----------------
        await type_and_send(tab, "/steering hello there")
        check("idle /steering is refused with a notice",
              await tab.until(
                  "document.querySelector('.slab.notice') && "
                  "document.querySelector('.slab.notice').textContent.includes('no turn is running')",
                  10),
              await tab.js("document.querySelector('.slab.notice') && "
                           "document.querySelector('.slab.notice').textContent"))
        check("idle /steering queued nothing",
              await tab.js("!!document.querySelector('.msg.user.steering')") is False)

        # --- 2. mid-turn: interrupt, keep the partial, re-plan --------------
        await type_and_send(tab, QUESTION)
        check("turn started (send disabled)",
              await tab.until("document.querySelector('#send').disabled", 15))
        # Wait for real streamed text — not thinking, not the question.
        check("answer text is streaming",
              await tab.until(
                  "(() => { const els = document.querySelectorAll('.msg.llm .body');"
                  " let n = 0; els.forEach(e => n += e.textContent.length);"
                  " return n > 300; })()", 60))
        await type_and_send(tab, f"/steering {STEERING}")

        check("steering interjection rendered as a user message",
              await tab.until(
                  "document.querySelector('.msg.user.steering') && "
                  "document.querySelector('.msg.user.steering').textContent.includes('STOP listing')",
                  30),
              await tab.js("document.querySelector('.msg.user.steering') && "
                           "document.querySelector('.msg.user.steering').textContent"))
        check("exactly one steering message (no double render)",
              await tab.until("document.querySelectorAll('.msg.user.steering').length === 1", 10))
        check("steering message is pinned (newest user message)",
              await tab.until(
                  "document.querySelector('.msg.user.steering') && "
                  "document.querySelector('.msg.user.steering').classList.contains('pin')", 10))
        check("partial answer kept on screen (text block before the steering)",
              await tab.until(
                  "(() => { const steering = document.querySelector('.msg.user.steering');"
                  " if (!steering) return false;"
                  " let seen = false;"
                  " for (const el of document.querySelectorAll('#log > .msg')) {"
                  "   if (el.classList.contains('llm') && el.textContent.length > 100) seen = true;"
                  "   if (el === steering) break;"
                  " }"
                  " return seen; })()", 10))
        check("turn ends after the re-plan",
              await tab.until("document.querySelector('#send').disabled === false", 120))
        check("no error surfaced",
              await tab.js(
                  "![...document.querySelectorAll('.slab.notice.err')]"
                  ".some(e => e.textContent.includes('RoundInterrupted'))") is True)
        check("final answer rendered",
              await tab.until(
                  "(() => { const els = [...document.querySelectorAll('.msg.llm .body')];"
                  " return els.length && els[els.length - 1].textContent.trim().length > 0; })()",
                  10))

        # --- 3. fresh attach: replayed from the history, in order, once -----
        tab2 = await open_tab(http, f"{CORE}/?session={sess}")
        try:
            check("fresh attach connects",
                  await tab2.until("document.querySelector('#status').textContent === 'connected'", 20))
            check("fresh attach replays the steering from history",
                  await tab2.until(
                      "(() => [...document.querySelectorAll('.msg.user .body')]"
                      ".some(b => b.textContent.includes('STOP listing')))()", 20))
            check("fresh attach: steering appears exactly once",
                  await tab2.until(
                      "[...document.querySelectorAll('.msg.user .body')]"
                      ".filter(b => b.textContent.includes('STOP listing')).length === 1", 10))
            check("fresh attach: order is question, (partial), steering, final answer",
                  await tab2.until(
                      "(() => {"
                      "  const log = [...document.querySelectorAll('#log > .msg')];"
                      "  const iQ = log.findIndex(e => e.classList.contains('user') && e.textContent.includes('Write the numbers'));"
                      "  const iB = log.findIndex(e => e.classList.contains('user') && e.textContent.includes('STOP listing'));"
                      "  const iA = log.findIndex(e => e.classList.contains('llm') && e.textContent.includes('fixed'));"
                      "  return iQ >= 0 && iB > iQ && iA > iB;"
                      " })()", 10),
                  await tab2.js(
                      "(() => { const log = [...document.querySelectorAll('#log > .msg')];"
                      " return log.map(e => e.className + ':' + e.textContent.slice(0, 24)).join(' | '); })()"))
        finally:
            await tab2.close()

        # --- 4. raw protocol -------------------------------------------------
        obs.cancel()
        try:
            await obs
        except (asyncio.CancelledError, Exception):
            pass
        steering_ev = [e for e in events if e.get("t") == "steering"]
        check("raw: exactly one steering event", len(steering_ev) == 1,
              f"{len(steering_ev)} steering events in {len(events)}")
        if steering_ev:
            e = steering_ev[0]
            check("raw: steering event carries turn_id and the text",
                  isinstance(e.get("turn_id"), int) and e.get("text", "").startswith("STOP listing"),
                  str(e))
            check("raw: steering event lands before turn_end",
                  [i for i, x in enumerate(events) if x.get("t") == "steering"][0]
                  < [i for i, x in enumerate(events) if x.get("t") == "turn_end"][0])
        check("raw: no error event in the turn",
              not [e for e in events if e.get("t") == "error"],
              str([e for e in events if e.get("t") == "error"]))
    finally:
        await tab.close()
        async with http.delete(f"{CORE}/sessions/{sess}"):
            pass


async def main():
    if not (Path("/usr/bin") / "google-chrome").exists() \
            and not (Path("/usr/bin") / "chromium").exists():
        print("no chrome found"); return 1

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    scratch_config(cfg)
    cfg.setdefault("core", {})
    cfg["core"]["port"] = PORT
    tmp = Path(tempfile.mkdtemp(prefix="letsclaw-steering-"))
    tmpcfg = tmp / "config.yaml"
    tmpcfg.write_text(yaml.safe_dump(cfg))

    # A headless Chrome of our own when none is serving :9222 — browser_test.py
    # launches its own, and a stale one from an earlier run is no better.
    chrome = None
    async with aiohttp.ClientSession() as probe:
        try:
            async with probe.get("http://127.0.0.1:9222/json/version") as r:
                if r.status != 200:
                    raise aiohttp.ClientError
        except aiohttp.ClientError:
            chrome = subprocess.Popen(
                ["/opt/google/chrome/chrome", "--headless=new",
                 "--remote-debugging-port=9222",
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

    proc = subprocess.Popen([PY, "source/server.py", "--config", str(tmpcfg)],
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

    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed"
          + (f" — FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
