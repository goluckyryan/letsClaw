#!/usr/bin/env python3
"""End-to-end check of the WebUI in a real browser.

Drives headless Chrome over the DevTools Protocol — no Selenium, no Playwright,
nothing to install. The core and a reachable model server must be up:

    ./serve.sh &
    .venv/bin/python web/browser_test.py

Every assertion is made against the live DOM, so a JS error that silently stops
the render shows up as a failed check rather than a passing test.
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

import re

import aiohttp
import yaml

CORE = os.environ.get("CORE", "http://127.0.0.1:8770")
CHROME = shutil.which("google-chrome") or shutil.which("chromium")
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'ok   ' if ok else 'FAIL '} {name}" + (f"\n        {detail}" if detail and not ok else ""))
    return ok


class Tab:
    """One Chrome tab, driven over CDP."""

    def __init__(self, http, ws):
        self.http, self.ws = http, ws
        self._id = 0
        self.console = []
        self.pending = {}
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        async for m in self.ws:
            if m.type is not aiohttp.WSMsgType.TEXT:
                continue
            d = json.loads(m.data)
            if "id" in d and d["id"] in self.pending:
                self.pending.pop(d["id"]).set_result(d)
            elif d.get("method") == "Runtime.consoleAPICalled":
                args = " ".join(str(a.get("value", a.get("description", "")))
                                for a in d["params"]["args"])
                self.console.append((d["params"]["type"], args))
            elif d.get("method") == "Runtime.exceptionThrown":
                det = d["params"]["exceptionDetails"]
                self.console.append(("exception", det.get("text", "") + " " +
                                     str(det.get("exception", {}).get("description", ""))))

    async def cmd(self, method, **params):
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self._id] = fut
        await self.ws.send_json({"id": self._id, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout=30)

    async def js(self, expr):
        r = await self.cmd("Runtime.evaluate", expression=expr,
                           returnByValue=True, awaitPromise=True)
        res = r.get("result", {})
        if "exceptionDetails" in res or "exceptionDetails" in r.get("result", {}):
            raise RuntimeError(res)
        if res.get("result", {}).get("subtype") == "error":
            raise RuntimeError(res["result"].get("description"))
        return res.get("result", {}).get("value")

    async def until(self, expr, timeout=120, poll=0.25):
        """Poll a JS predicate until true. Returns False on timeout."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if await self.js(expr):
                    return True
            except Exception:
                pass
            await asyncio.sleep(poll)
        return False

    async def close(self):
        self.reader.cancel()
        await self.ws.close()


async def open_tab(http, url):
    async with http.put(f"http://127.0.0.1:9222/json/new?{url}") as r:
        info = await r.json()
    ws = await http.ws_connect(info["webSocketDebuggerUrl"], max_msg_size=0)
    tab = Tab(http, ws)
    await tab.cmd("Runtime.enable")
    await tab.cmd("Page.enable")
    return tab


async def main():
    if not CHROME:
        print("no chrome found"); return 1

    profile = tempfile.mkdtemp(prefix="letsclaw-cdp-")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--remote-debugging-port=9222",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
         "--disable-gpu", "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async with aiohttp.ClientSession() as http:
        for _ in range(60):                       # wait for the debug port
            try:
                async with http.get("http://127.0.0.1:9222/json/version") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.25)
        else:
            print("chrome never opened the debug port"); return 1

        try:
            await run(http)
        finally:
            chrome.terminate()
            shutil.rmtree(profile, ignore_errors=True)

    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f" — FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


async def run(http):
    sess = f"webtest{int(time.time()) % 100000}"
    tab = await open_tab(http, f"{CORE}/?session={sess}")

    # --- connect -------------------------------------------------------------
    check("page connects to the core",
          await tab.until("document.querySelector('#status').textContent === 'connected'", 20),
          await tab.js("document.querySelector('#status').textContent"))
    check("session listed and selected in the sidebar", await tab.js(
        "(() => { const r = document.querySelector('#side-list .srow.on');"
        "  return r && r.querySelector('.nm').textContent; })()") == sess)
    check("model list populated", (await tab.js("document.querySelector('#model').options.length")) > 0)
    check("hint line rendered", sess in (await tab.js("document.querySelector('#hint').textContent")))
    # The gate is `hidden`, but an author `display:` beats that — assert what the page
    # actually paints, not the attribute. Driving the DOM over CDP happily clicks
    # straight through an overlay, so only computed style catches this.
    check("no token gate on a tokenless core",
          await tab.js("getComputedStyle(document.querySelector('#gate')).display") == "none")
    check("nothing else is covering the composer", await tab.js(
        "(() => { const r = document.querySelector('#input').getBoundingClientRect();"
        "  const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);"
        "  return el && el.id; })()") == "input")

    # --- a real turn ---------------------------------------------------------
    await tab.js("""(() => {
      const i = document.querySelector('#input');
      i.value = 'Reply with exactly this markdown and nothing else: '
              + 'a level-2 heading saying Report, then a bash code block containing ls -la, '
              + 'then a bullet list of two items.';
      document.querySelector('#send').click();
    })()""")

    check("user message echoed from turn_start",
          await tab.until("document.querySelectorAll('.msg.user').length === 1", 20))
    check("spinner shows while waiting",
          await tab.until("!!document.querySelector('.waiting')", 10))
    check("turn completes",
          await tab.until("document.querySelectorAll('.slab.stats').length === 1", 180))
    check("spinner cleared after the turn", not await tab.js("!!document.querySelector('.waiting')"))
    check("assistant answer rendered",
          (await tab.js("document.querySelectorAll('.msg.assistant').length")) >= 1)

    html = await tab.js("document.querySelector('.msg.assistant .body').innerHTML")
    check("markdown became real elements",
          "<pre" in html and re.search(r"<h[2-6]", html) and "<ul>" in html, html[:200])
    check("code block got a copy button", 'class="copy"' in html)
    check("code block content survived", "ls -la" in html)
    stats_txt = await tab.js("document.querySelector('.slab.stats').textContent")
    check("stats line rendered", "context" in stats_txt, stats_txt)
    # The output counter: what the model generated, not what the prompt cost. It is
    # a separate figure from `assistant N tok` and carries its own ~ when the server
    # withheld usage, so assert on the label rather than on any particular number.
    check("stats line reports output tokens", " out " in stats_txt, stats_txt)
    check("output count is a real number", re.search(r"out ~?(\d+) tok", stats_txt)
          and int(re.search(r"out ~?(\d+) tok", stats_txt).group(1)) > 0, stats_txt)
    check("gauge updated from stats",
          "/" in (await tab.js("document.querySelector('#gauge-label').textContent")))
    # Spelled out in full, "262,144" ran under the 90% trip marker. Assert on where the
    # text actually lands, not on how it is formatted — the next thing to widen the
    # label will be something else.
    check("gauge label clears the trip marker", await tab.js(
        "(() => { const t = document.querySelector('#gauge-trip');"
        "  if (t.hidden) return true;"
        "  const r = document.createRange();"
        "  r.selectNodeContents(document.querySelector('#gauge-label'));"
        "  return r.getBoundingClientRect().right <= t.getBoundingClientRect().left; })()"))
    check("send re-enabled", not await tab.js("document.querySelector('#send').disabled"))
    check("stop button hidden again", await tab.js("document.querySelector('#stop').hidden"))

    # --- commands ------------------------------------------------------------
    await tab.js("""(() => {
      document.querySelector('#input').value = '/info';
      document.querySelector('#send').click();
    })()""")
    check("/info renders a panel",
          await tab.until("[...document.querySelectorAll('.slab.notice')]"
                          ".some(e => e.textContent.includes('rollover'))", 20))

    await tab.js("document.querySelector('#btn-reasoning').click()")
    check("reasoning toggle sticks", await tab.js("document.querySelector('#btn-reasoning').classList.contains('on')"))
    await tab.js("document.querySelector('#btn-reasoning').click()")

    # --- slash autocomplete ---------------------------------------------------
    await tab.js("""(() => {
      const i = document.querySelector('#input');
      i.value = '/mo';
      i.dispatchEvent(new Event('input'));
    })()""")
    check("autocomplete opens on /",
          not await tab.js("document.querySelector('#ac').hidden"))
    check("autocomplete offers /model",
          "/model" in (await tab.js("document.querySelector('#ac').textContent")))
    await tab.js("""(() => {
      const i = document.querySelector('#input');
      i.value = ''; i.dispatchEvent(new Event('input'));
    })()""")

    # --- a real tool call -----------------------------------------------------
    await tab.js("""(() => {
      document.querySelector('#input').value =
        'Use the exec tool to run: echo letsclaw-tool-ok    then tell me what it printed.';
      document.querySelector('#send').click();
    })()""")
    check("tool call slab appears",
          await tab.until("!!document.querySelector('.slab.tool')", 180))
    check("tool call named before it ran",
          "exec" in (await tab.js("document.querySelector('.slab.tool').textContent")))
    check("tool result size reported",
          await tab.until("!!document.querySelector('.slab.tool .done')", 180))
    check("tool turn finishes",
          await tab.until("document.querySelectorAll('.slab.stats').length === 2", 180))
    check("tool arguments are inspectable",
          "echo" in (await tab.js("document.querySelector('.slab.tool pre').textContent") or ""))
    # A tool slab must hang under the message bodies, not under the avatar gutter.
    check("tool slab lines up with the message bodies", await tab.js(
        "(() => { const a = document.querySelector('.msg.user .body').getBoundingClientRect().left;"
        "  const b = document.querySelector('.slab.tool .slab-box').getBoundingClientRect().left;"
        "  return Math.abs(a - b) < 2; })()"))
    check("no empty assistant bubble before the tool call", await tab.js(
        "[...document.querySelectorAll('.msg.assistant .body')]"
        "  .every(b => b.textContent.trim().length > 0)"))

    # --- events that are hard to provoke on demand, injected verbatim ---------
    # Rendering is what is under test here, so feeding handle() the same dicts
    # core.py emits is the honest way to reach these paths.
    await tab.js("""(() => {
      window.__sent = [];
      window.__realSend = S.ws.send.bind(S.ws);
      S.ws.send = (m) => window.__sent.push(JSON.parse(m));
      handle({t: 'rollover_ask', request_id: 'test-1', used: 900, budget: 1000,
              percent: 90, timeout_s: 60});
    })()""")
    check("rollover ask renders a dialog",
          "Roll over to a new session?" in (await tab.js("document.querySelector('.slab.roll').textContent")))
    check("ask offers both answers",
          (await tab.js("document.querySelectorAll('.slab.roll button').length")) == 2)

    await tab.js("[...document.querySelectorAll('.slab.roll button')].pop().click()")   # 'Keep it'
    reply = await tab.js("window.__sent[0]")
    check("declining sends rollover_reply",
          reply and reply.get("t") == "rollover_reply" and reply.get("yes") is False
          and reply.get("request_id") == "test-1", str(reply))
    check("buttons disappear once answered", await tab.js(
        "[...document.querySelectorAll('.slab.roll button')]"
        "  .every(b => getComputedStyle(b).display === 'none')"))

    await tab.js("""(() => {
      handle({t: 'busy', reason: 'a turn is already running in this session'});
      handle({t: 'error', msg: 'ConnectionError: server went away'});
      handle({t: 'notice', level: 'warn', text: 'past 90% of the window'});
      handle({t: 'rollover_start', reason: 'context at 91%'});
      handle({t: 'rollover_done', transcript: 'state/sessions/x.json',
              handoff: 'state/memory_store/long_term.md', used: 300, budget: 1000});
    })()""")
    check("busy rendered", "already running" in (await tab.js(
        "[...document.querySelectorAll('.slab.notice')].map(e=>e.textContent).join('|')")))
    check("error rendered in red", await tab.js("!!document.querySelector('.slab.notice.err')"))
    check("rollover_done shows both paths",
          "long_term.md" in (await tab.js(
              "[...document.querySelectorAll('.slab.roll')].map(e=>e.textContent).join('|')")))
    check("rollover_done draws a separator", await tab.js("!!document.querySelector('.sep')"))
    check("spinner not left spinning after rollover_done",
          not await tab.js("!!document.querySelector('.waiting')"))
    await tab.js("S.ws.send = window.__realSend")

    # --- a second tab on the same session ------------------------------------
    tab2 = await open_tab(http, f"{CORE}/?session={sess}")
    check("second tab connects",
          await tab2.until("document.querySelector('#status').textContent === 'connected'", 20))
    check("second tab replays the history",
          (await tab2.js("document.querySelectorAll('.msg.user').length")) == 2)
    check("replayed answer is rendered markdown",
          "<pre" in (await tab2.js("document.querySelector('.msg.assistant .body').innerHTML") or ""))
    check("replayed tool call is paired with its result",
          "chars" in (await tab2.js("document.querySelector('.slab.tool').textContent") or ""))
    # Replay builds the log from history, not from events, so it needs its own check.
    check("no empty bubbles in replayed history", await tab2.js(
        "[...document.querySelectorAll('.msg .body')]"
        "  .every(b => b.textContent.trim().length > 0)"))

    # a turn typed in tab 2 must appear in tab 1
    await tab2.js("""(() => {
      document.querySelector('#input').value = 'Say only: pong';
      document.querySelector('#send').click();
    })()""")
    check("tab 1 sees tab 2's message",
          await tab.until("document.querySelectorAll('.msg.user').length === 3", 30))
    check("tab 1 sees tab 2's answer",
          await tab.until("document.querySelectorAll('.slab.stats').length === 3", 180))

    t1 = await tab.js("[...document.querySelectorAll('.msg.user')].pop().textContent")
    t2 = await tab2.js("[...document.querySelectorAll('.msg.user')].pop().textContent")
    check("both tabs show identical text", t1 == t2, f"{t1!r} vs {t2!r}")

    # --- an independent session ----------------------------------------------
    tab3 = await open_tab(http, f"{CORE}/?session={sess}-other")
    check("third tab connects",
          await tab3.until("document.querySelector('#status').textContent === 'connected'", 20))
    check("separate session starts empty",
          (await tab3.js("document.querySelectorAll('.msg.user').length")) == 0)

    # --- switching session in place, by clicking the sidebar ------------------
    check("sidebar lists the other session too", await tab3.until(
        f"[...document.querySelectorAll('#side-list .nm')]"
        f"  .some(n => n.textContent === '{sess}')", 20))
    await tab3.js(f"""(() => {{
      [...document.querySelectorAll('#side-list .srow')]
        .find(r => r.dataset.name === '{sess}').click();
    }})()""")
    check("switching session loads its history",
          await tab3.until("document.querySelectorAll('.msg.user').length === 3", 20))
    await asyncio.sleep(1.5)
    check("only one socket after switching",
          (await http_json(http, f"{CORE}/sessions"))["by"][sess] == 3,
          str(await http_json(http, f"{CORE}/sessions")))

    # --- reconnect ------------------------------------------------------------
    await tab3.js("S.ws.close()")
    check("reconnects by itself",
          await tab3.until("document.querySelector('#status').textContent === 'connected'", 20))

    # --- the sidebar: create, switch, delete ----------------------------------
    check("sidebar is visible", await tab3.js(
        "getComputedStyle(document.querySelector('#side')).display") != "none")
    # The sidebar takes 210px off the header's width; at the default window size
    # that was enough to wrap the status pill onto a second row. Compare the two
    # ends of the header rather than pick a magic height.
    check("header still fits on one row", await tab3.js(
        "(() => { const a = document.querySelector('.brand').getBoundingClientRect();"
        "  const b = document.querySelector('#status').getBoundingClientRect();"
        "  return Math.abs(a.top - b.top) < 6; })()"))
    check("busy dot tracks a live turn", await tab3.js(
        "[...document.querySelectorAll('#side-list .dot')].every(d => d.hidden)"))
    shown = await tab3.js(
        f"(() => {{ const r = [...document.querySelectorAll('#side-list .srow')]"
        f"  .find(r => r.dataset.name === '{sess}');"
        f"  return r && r.querySelector('.ct').textContent; }})()")
    core_count = (await http_json(http, f"{CORE}/sessions"))["msgs"][sess]
    check("message count matches the core", shown == str(core_count),
          f"sidebar {shown!r} vs core {core_count}")

    # "+" then a name creates the session by switching to it — nothing is created
    # core-side until a client attaches.
    born = f"{sess}-born"
    await tab3.js("document.querySelector('#side-new').click()")
    # The field must open beside the + that summons it. It used to sit after
    # #side-list, which is flex:1, so it was pushed to the far bottom of the sidebar.
    check("the new-session field opens next to the +", await tab3.js(
        "(() => { const b = document.querySelector('#side-new').getBoundingClientRect();"
        "  const i = document.querySelector('#side-name').getBoundingClientRect();"
        "  const l = document.querySelector('#side-list').getBoundingClientRect();"
        "  return (i.top - b.bottom) < 24 && i.bottom <= l.top + 1; })()"))
    await tab3.js(f"""(() => {{
      document.querySelector('#side-name').value = '{born}';
      document.querySelector('#side-add')
              .dispatchEvent(new Event('submit', {{cancelable: true}}));
    }})()""")
    check("the + form creates and switches to a new session",
          await tab3.until(f"document.querySelector('#side-list .srow.on')"
                           f"  .dataset.name === '{born}'", 20))
    check("the new session exists core-side",
          born in [s["name"] for s in (await http_json(http, f"{CORE}/sessions"))["sessions"]])
    check("new session starts empty",
          (await tab3.js("document.querySelectorAll('.msg').length")) == 0)

    # Delete a session this tab is NOT looking at: confirm() is stubbed, since a
    # native dialog would block the CDP evaluate that opened it.
    await tab3.js("window.confirm = () => true")
    await tab3.js(f"""(() => {{
      [...document.querySelectorAll('#side-list .srow')]
        .find(r => r.dataset.name === '{sess}-other')
        .querySelector('.del').click();
    }})()""")
    check("deleting removes it from the core",
          await gone(http, f"{sess}-other"))
    check("deleting removes it from the sidebar", await tab3.until(
        f"![...document.querySelectorAll('#side-list .srow')]"
        f"  .some(r => r.dataset.name === '{sess}-other')", 20))
    check("the session being viewed is untouched",
          (await tab3.js("document.querySelector('#side-list .srow.on').dataset.name")) == born)

    # Now delete the one tab1 is attached to and watch it get told.
    await tab3.js(f"""(() => {{
      [...document.querySelectorAll('#side-list .srow')]
        .find(r => r.dataset.name === '{sess}')
        .querySelector('.del').click();
    }})()""")
    # Match the text, not just `.notice.warn`: this tab already collected warn
    # notices from the injected-events section, and a bare class check passes on
    # one of those before the delete has even arrived.
    check("an attached client is told its session was deleted", await tab.until(
        "[...document.querySelectorAll('.slab.notice.warn')]"
        "  .some(n => n.textContent.includes('was deleted'))", 20),
        await tab.js("document.body.textContent.slice(-200)"))
    check("the deleted session's log is cleared",
          (await tab.js("document.querySelectorAll('.msg').length")) == 0)
    check("its socket stays up",
          (await tab.js("document.querySelector('#status').textContent")) == "connected")

    # ...and that client can carry straight on, on a fresh session of that name.
    await tab.js("""(() => {
      document.querySelector('#input').value = 'Say only: back';
      document.querySelector('#send').click();
    })()""")
    check("a re-homed client can still send",
          await tab.until("document.querySelectorAll('.msg.assistant').length >= 1", 180),
          await tab.js("document.querySelector('#status').textContent"))
    check("the re-homed session came back empty",
          (await tab.js("document.querySelectorAll('.msg.user').length")) == 1)

    # A turn holds the session lock; deleting mid-turn would orphan it still
    # running, so the core must refuse rather than leave a task emitting into
    # something the registry has already forgotten.
    await tab3.js("""(() => {
      document.querySelector('#input').value = 'Count slowly from 1 to 40.';
      document.querySelector('#send').click();
    })()""")
    await tab3.until("document.querySelector('#stop').hidden === false", 20)
    async with http.delete(f"{CORE}/sessions/{born}") as r:
        body = await r.json()
    check("delete is refused while a turn is running",
          r.status == 409 and not body["ok"], f"{r.status} {body}")
    check("the busy session is still there",
          born in [s["name"] for s in (await http_json(http, f"{CORE}/sessions"))["sessions"]])
    await tab3.js("document.querySelector('#stop').click()")
    await asyncio.sleep(1)

    # --- rename ---------------------------------------------------------------
    # Same guard as delete, for a different reason: emit() stamps every event with
    # the session's name, so renaming mid-turn would split one turn across two.
    await tab3.js("""(() => {
      document.querySelector('#input').value = 'Count slowly from 1 to 40.';
      document.querySelector('#send').click();
    })()""")
    await tab3.until("document.querySelector('#stop').hidden === false", 20)
    async with http.post(f"{CORE}/sessions/{born}/rename", json={"to": f"{born}-x"}) as r:
        body = await r.json()
    check("rename is refused while a turn is running",
          r.status == 409 and not body["ok"], f"{r.status} {body}")
    await tab3.js("document.querySelector('#stop').click()")
    await asyncio.sleep(1)

    async with http.post(f"{CORE}/sessions/nope-{born}/rename", json={"to": "x"}) as r:
        check("renaming a session that does not exist is a 404", r.status == 404)
    async with http.post(f"{CORE}/sessions/{born}/rename", json={"to": "  "}) as r:
        check("a blank name is refused", r.status == 400, str(r.status))
    async with http.post(f"{CORE}/sessions/{born}/rename", json={"to": "a/b"}) as r:
        check("a name with a slash is refused", r.status == 400, str(r.status))
    async with http.post(f"{CORE}/sessions/{born}/rename", json={"to": sess}) as r:
        check("renaming onto an existing name is refused", r.status == 409, str(r.status))
    check("a refused rename changed nothing",
          born in [s["name"] for s in (await http_json(http, f"{CORE}/sessions"))["sessions"]])

    # The real thing, from the UI, on the session this tab is looking at. prompt()
    # is stubbed for the same reason confirm() was: a native dialog would block the
    # very evaluate that opened it.
    before = await tab3.js("document.querySelectorAll('.msg').length")
    renamed = f"{born}-renamed"
    await tab3.js(f"window.prompt = () => '{renamed}'")
    # Dispatch on the <path> inside the button, not the button: that is what a real
    # pointer lands on, and it only reaches the handler if closest('.ren') walks up
    # out of the SVG. Clicking the button itself would pass either way.
    await tab3.js(f"""(() => {{
      [...document.querySelectorAll('#side-list .srow')]
        .find(r => r.dataset.name === '{born}')
        .querySelector('.ren svg path')
        .dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
    }})()""")
    check("the old name is gone core-side", await gone(http, born))
    check("the new name is in the core's list",
          renamed in [s["name"] for s in (await http_json(http, f"{CORE}/sessions"))["sessions"]])
    check("the sidebar shows the new name", await tab3.until(
        f"[...document.querySelectorAll('#side-list .srow')]"
        f"  .some(r => r.dataset.name === '{renamed}')", 20))
    check("the renamed session is still the selected one", await tab3.until(
        f"document.querySelector('#side-list .srow.on').dataset.name === '{renamed}'", 20))
    # The point of renaming rather than recreating: the conversation comes along.
    check("the conversation survived the rename",
          (await tab3.js("document.querySelectorAll('.msg').length")) == before,
          f"{before} before")
    check("the attached tab was told", await tab3.until(
        "[...document.querySelectorAll('.slab.notice')]"
        "  .some(n => n.textContent.includes('Renamed'))", 20))
    check("the tab followed the rename into its URL",
          renamed in (await tab3.js("location.search")))
    check("the footer hint followed too",
          renamed in (await tab3.js("document.querySelector('#hint').textContent")))
    check("the title followed too", renamed in (await tab3.js("document.title")))
    # A reload must land back on the same conversation, which is the whole reason
    # the URL had to be rewritten.
    await tab3.js("location.reload()")
    check("a reload lands on the renamed session", await tab3.until(
        f"document.querySelector('#side-list .srow.on')"
        f"  && document.querySelector('#side-list .srow.on').dataset.name === '{renamed}'", 30))

    # --- the token gate -------------------------------------------------------
    # Needs a core started with core.token set, so this section brings up its own
    # on a second port. Without it the whole auth branch is dead code in testing.
    tab4 = await run_token_gate(http)
    await run_persistence(http)
    await run_reload(http)

    # --- console must be clean -----------------------------------------------
    tabs = (("tab1", tab), ("tab2", tab2), ("tab3", tab3), ("gate tab", tab4))
    for name, t in tabs:
        if not t:
            continue
        bad = [c for c in t.console if c[0] in ("error", "exception")]
        check(f"no JS errors in {name}", not bad, str(bad[:3]))

    for _, t in tabs:
        if t:
            await t.close()


async def run_token_gate(http):
    """Start a second, token-protected core and check the page asks for the token."""
    cfg = re.sub(r"^(\s*)token:.*$", r'\1token: "s3cr3t"', CONFIG.read_text(),
                 count=1, flags=re.M)
    cfg = re.sub(r"^(\s*)port:.*$", r"\1port: 8771", cfg, count=1, flags=re.M)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(cfg)
    tmp.close()
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--config", tmp.name],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                async with http.get("http://127.0.0.1:8771/health") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.25)
        else:
            check("token core starts", False, "never became healthy")
            return None

        async with http.get("http://127.0.0.1:8771/sessions") as r:
            check("core rejects an unauthenticated request", r.status == 401, str(r.status))
        async with http.get("http://127.0.0.1:8771/sessions",
                            headers={"Authorization": "Bearer s3cr3t"}) as r:
            check("core accepts the token", r.status == 200, str(r.status))

        t = await open_tab(http, "http://127.0.0.1:8771/?session=gate")
        check("gate is shown when the core wants a token",
              await t.until("getComputedStyle(document.querySelector('#gate')).display"
                            " === 'flex'", 20),
              await t.js("getComputedStyle(document.querySelector('#gate')).display"))
        check("gate blocks the composer", await t.js(
            "(() => { const r = document.querySelector('#input').getBoundingClientRect();"
            "  const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);"
            "  return el && el.id; })()") == "gate")

        await t.js("""(() => {
          document.querySelector('#gate-token').value = 's3cr3t';
          document.querySelector('#gate-form')
                  .dispatchEvent(new Event('submit', {cancelable: true}));
        })()""")
        check("the right token connects",
              await t.until("document.querySelector('#status').textContent === 'connected'", 20),
              await t.js("document.querySelector('#status').textContent"))
        check("gate goes away once connected",
              await t.js("getComputedStyle(document.querySelector('#gate')).display") == "none")
        return t
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.unlink(tmp.name)


async def run_persistence(http):
    """Restart a core and check the sessions are still there.

    Its own core on its own port, with live_dir pointed at a temp directory, so
    the machine's real state/live is never touched. The turn is a real one — the
    thing being tested is that a conversation survives, and only a conversation
    the model actually answered proves it.
    """
    port, base = 8773, "http://127.0.0.1:8773"
    live = tempfile.mkdtemp(prefix="letsclaw-live-")
    cfg = re.sub(r"^(\s*)port:.*$", rf"\1port: {port}", CONFIG.read_text(),
                 count=1, flags=re.M)
    cfg = re.sub(r"^(\s*)live_dir:.*$", rf'\1live_dir: "{live}"', cfg, count=1, flags=re.M)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(cfg)
    tmp.close()

    proc = None

    async def start():
        nonlocal proc
        proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"),
                                 "--config", tmp.name],
                                cwd=ROOT, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                async with http.get(f"{base}/health") as r:
                    if r.status == 200:
                        return True
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.25)
        return False

    def stop(hard=False):
        nonlocal proc
        if not proc:
            return
        proc.kill() if hard else proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc = None

    async def listed():
        async with http.get(f"{base}/sessions") as r:
            return {s["name"]: s for s in (await r.json())["sessions"]}

    async def turn(name, text):
        """One real turn over the wire, so the history on disk is a real history."""
        async with http.ws_connect(f"{base}/ws?session={name}") as ws:
            await ws.receive_json()
            await ws.send_json({"t": "submit", "text": text})
            end = time.time() + 180
            while time.time() < end:
                e = await ws.receive_json()
                if e["t"] == "turn_end":
                    return True
        return False

    t = None
    try:
        if not await start():
            check("persistence core starts", False, "never became healthy")
            return None

        await turn("persist-probe", "Reply with exactly the word BANANA and nothing else.")
        check("a turn is written to disk immediately",
              len(list(Path(live).glob("*.json"))) == 1,
              str(os.listdir(live)))

        # SIGKILL, not SIGTERM: the shutdown hook cannot run, so only the
        # after-every-turn save can make this pass.
        stop(hard=True)
        if not await start():
            check("core restarts after a kill -9", False, "never became healthy")
            return None
        s = (await listed()).get("persist-probe")
        check("a session survives kill -9, before anything attaches",
              bool(s) and s["messages"] == 2 and s["clients"] == 0,
              str(s))
        check("it comes back on a usable model",
              bool(s) and isinstance(s["model"], str) and s["model"], str(s))
        # The output odometer rides in the same file. Read it off disk rather than
        # over the wire: /info would need a socket, and the point of the check is
        # that the number was written, not that it can be rendered.
        saved = json.loads(next(iter(Path(live).glob("*.json"))).read_text())
        check("the output token total is saved and restored",
              isinstance(saved.get("total_output"), int) and saved["total_output"] > 0,
              str(saved.get("total_output")))

        t = await open_tab(http, f"{base}/?session=persist-probe")
        check("the restored session is in the sidebar",
              await t.until("[...document.querySelectorAll('#side-list .srow')]"
                            ".some(r => r.dataset.name === 'persist-probe')", 20))
        check("the restored conversation is in the scrollback",
              await t.until("document.querySelectorAll('#log .msg').length >= 2", 20),
              await t.js("document.querySelectorAll('#log .msg').length"))

        # A name nobody speaks in must leave nothing behind — sessions are created
        # by attaching, so otherwise every typo would live in the sidebar forever.
        async with http.ws_connect(f"{base}/ws?session=never-spoken") as ws:
            await ws.receive_json()
        check("an untouched session is not written",
              len(list(Path(live).glob("*.json"))) == 1, str(os.listdir(live)))

        async with http.post(f"{base}/sessions/persist-probe/rename",
                             json={"to": "probe two"}) as r:
            check("rename accepted", r.status == 200, str(r.status))
        await t.close()
        t = None

        stop()
        if not await start():
            check("core restarts after a rename", False, "never became healthy")
            return None
        names = await listed()
        check("a rename follows the session across a restart",
              "probe two" in names and "persist-probe" not in names, str(list(names)))
        check("and no session came back that nobody spoke in",
              "never-spoken" not in names, str(list(names)))

        async with http.delete(f"{base}/sessions/probe%20two") as r:
            check("delete accepted", r.status == 200, str(r.status))
        stop()
        if not await start():
            check("core restarts after a delete", False, "never became healthy")
            return None
        check("a deleted session does not come back",
              not await listed() and not list(Path(live).glob("*.json")),
              str(os.listdir(live)))
        return None
    finally:
        if t:
            await t.close()
        stop()
        os.unlink(tmp.name)
        shutil.rmtree(live, ignore_errors=True)



async def run_reload(http):
    """Change config.yaml under a running core and check it takes effect.

    Its own core on its own port with its own copy of the config, because this
    section's whole job is to rewrite that file repeatedly — including into
    deliberately broken YAML, which must not be anywhere near the real one.
    """
    port, base = 8774, "http://127.0.0.1:8774"
    live = tempfile.mkdtemp(prefix="letsclaw-reload-")
    cfg = yaml.safe_load(CONFIG.read_text())
    cfg.setdefault("core", {})["port"] = port
    cfg.setdefault("conversation", {})["live_dir"] = live
    cfg["conversation"]["rollover_at_percent"] = 90
    model = cfg["models"].get("default_model") or next(
        k for k in cfg["models"] if k != "default_model")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.close()

    def write(c):
        Path(tmp.name).write_text(yaml.safe_dump(c))

    write(cfg)
    proc = None

    async def reload():
        async with http.post(f"{base}/reload") as r:
            return r.status, await r.json()

    async def budget():
        """The budget the core hands a client on attach — the number the gauge shows."""
        async with http.ws_connect(f"{base}/ws?session=reload-probe") as ws:
            return (await ws.receive_json())["budget"]

    try:
        proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"),
                                 "--config", tmp.name],
                                cwd=ROOT, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                async with http.get(f"{base}/health") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.25)
        else:
            check("reload core starts", False, "never became healthy")
            return

        # --- the feature, in one assertion: a live socket sees the new budget ---
        async with http.ws_connect(f"{base}/ws?session=reload-probe") as ws:
            before = (await ws.receive_json())["budget"]
            cfg["models"][model]["context_length"] = before + 4242
            write(cfg)
            status, body = await reload()
            check("reload accepted", status == 200 and body["ok"], str(body)[:200])
            check("the reply names what changed",
                  any("context_length" in c for c in body.get("changed", [])),
                  str(body.get("changed")))
            # Broadcast to a socket that was already open — the point of the whole
            # feature is that this arrives without the client reconnecting.
            e = await asyncio.wait_for(ws.receive_json(), 20)
            check("an attached session is told, without reconnecting",
                  e["t"] == "session_state" and e.get("what") == "reloaded"
                  and e["budget"] == before + 4242, str(e)[:200])
        check("the new budget outlives the socket that saw it",
              await budget() == before + 4242)

        # --- a broken file changes nothing --------------------------------------
        good = before + 4242
        Path(tmp.name).write_text("models:\n  broken: [unclosed\n")
        status, body = await reload()
        check("broken YAML is refused with 400", status == 400 and not body["ok"], str(body)[:200])
        async with http.get(f"{base}/health") as r:
            check("the core is still healthy after a refused reload", r.status == 200)
        check("and still serving the old config", await budget() == good)

        # A config that parses but cannot build an engine is refused the same way,
        # which is why validation builds engines rather than inspecting keys.
        bad = yaml.safe_load(yaml.safe_dump(cfg))
        bad["models"][model].pop("base_url", None)
        write(bad)
        status, body = await reload()
        check("a model with no base_url is refused too",
              status == 400 and "base_url" in body.get("error", ""), str(body)[:200])
        check("still serving the old config after that", await budget() == good)

        # --- a live setting that is not the model ------------------------------
        write(cfg)
        await reload()
        cfg["conversation"]["rollover_at_percent"] = 71
        write(cfg)
        await reload()
        async with http.ws_connect(f"{base}/ws?session=reload-probe") as ws:
            await ws.receive_json()
            await ws.send_json({"t": "command", "name": "info", "request_id": "r1"})
            info = None
            for _ in range(10):
                e = await asyncio.wait_for(ws.receive_json(), 20)
                if e["t"] == "response" and e.get("info"):
                    info = e["info"]
                    break
        check("a session that never touched the setting takes the new value",
              bool(info) and info["rollover"]["percent"] == 71,
              str(info and info["rollover"]))

        # --- the model a live session is using, deleted from the config ---------
        # The session must keep working: moving a conversation onto a different
        # model behind the user's back would be worse than keeping a stale engine.
        spare = [k for k in cfg["models"] if k not in ("default_model", model)]
        if spare:
            shrunk = yaml.safe_load(yaml.safe_dump(cfg))
            shrunk["models"].pop(model)
            shrunk["models"]["default_model"] = spare[0]
            write(shrunk)
            status, body = await reload()
            check("a model in use can be removed from a running core",
                  status == 200 and body["ok"], str(body)[:200])
            async with http.get(f"{base}/models") as r:
                names = (await r.json())["configured"]
            check("and it leaves the model list", model not in names, str(names))
            async with http.ws_connect(f"{base}/ws?session=reload-probe") as ws:
                h = await ws.receive_json()
            check("the session using it keeps the model it had",
                  h["model"] == model and h["budget"] > 0, str(h)[:200])
            write(cfg)
            await reload()

        # --- mid-turn: deferred, then applied at turn_end ------------------------
        async with http.ws_connect(f"{base}/ws?session=reload-busy") as ws:
            await ws.receive_json()
            await ws.send_json({"t": "submit", "text": "Count from 1 to 30, one per line."})
            await asyncio.wait_for(ws.receive_json(), 30)      # turn_start
            await asyncio.sleep(1.5)                            # let it get going
            cfg["models"][model]["context_length"] = good + 100
            write(cfg)
            status, body = await reload()
            check("a reload during a turn is deferred, not forced",
                  status == 200 and body["deferred"] >= 1, str(body)[:200])
            # And it must actually land: ordering matters, so the reloaded event has
            # to come after turn_end rather than interleaved with the stream.
            ended = applied = False
            end = time.time() + 240
            while time.time() < end and not applied:
                e = await asyncio.wait_for(ws.receive_json(), 240)
                if e["t"] == "turn_end":
                    ended = True
                elif e["t"] == "session_state" and e.get("what") == "reloaded":
                    applied = True
            check("the deferred reload lands once the turn ends",
                  applied and ended, f"ended={ended} applied={applied}")

        # --- the divergence rule -------------------------------------------------
        # Reached in-process: a session only auto-disables its own rollover deep
        # inside a turn, and there is no client-side way to make it happen. Building
        # a manager here opens no sockets and touches no disk, so it costs nothing.
        sys.path.insert(0, str(ROOT))
        import core as corelib
        cfg["conversation"]["rollover_at_percent"] = 90
        write(cfg)
        mgr = corelib.SessionManager(yaml.safe_load(Path(tmp.name).read_text()), tmp.name)
        untouched = await mgr.get("untouched")
        diverged = await mgr.get("diverged")
        diverged.rollover_pct = 0          # what core.py does when a fresh window trips
        cfg["conversation"]["rollover_at_percent"] = 55
        write(cfg)
        await mgr.reload()
        check("a reload moves a session that never changed the setting",
              untouched.rollover_pct == 55, str(untouched.rollover_pct))
        check("and leaves alone one that changed it for itself",
              diverged.rollover_pct == 0, str(diverged.rollover_pct))

        # The rollover handoff lives in the system message, which a reload rebuilds.
        diverged.history = diverged.fresh_history("objective: survive the reload")
        diverged.history.append({"role": "user", "content": "hi"})
        cfg["conversation"]["rollover_at_percent"] = 60
        write(cfg)
        await mgr.reload()
        check("a reload keeps the carryover a rollover parked in the prompt",
              diverged.carryover() == "objective: survive the reload",
              repr(diverged.carryover()))
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        os.unlink(tmp.name)
        shutil.rmtree(live, ignore_errors=True)


async def http_json(http, url):
    async with http.get(url) as r:
        d = await r.json()
    d["by"] = {s["name"]: s["clients"] for s in d["sessions"]}
    d["msgs"] = {s["name"]: s["messages"] for s in d["sessions"]}
    return d


async def gone(http, name, timeout=20):
    """True once the core no longer lists this session."""
    end = time.time() + timeout
    while time.time() < end:
        d = await http_json(http, f"{CORE}/sessions")
        if name not in d["by"]:
            return True
        await asyncio.sleep(0.25)
    return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
