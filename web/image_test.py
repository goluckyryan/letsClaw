#!/usr/bin/env python3
"""Agent-produced images in a real browser.

    CORE=http://127.0.0.1:9131 .venv/bin/python web/image_test.py

test_md.mjs proves the renderer emits the right tag and refuses the wrong src.
Only a browser can say whether the image actually decodes and paints, whether
the token in the query is accepted by the live route, whether a wide plot is
bounded by the column instead of blowing it out, whether a missing file
degrades to a legible note, and whether the log still follows the bottom after
an image settles its height (it loads *after* the append that scrolled).

Renders into a detached node and sends no turn: costs nothing, touches no
session history.
"""
import asyncio, json, os, re, shutil, subprocess, tempfile, time
import aiohttp

try:
    CORE = os.environ["CORE"]
except KeyError:
    raise SystemExit(
        "web/image_test.py: set CORE first.\n"
        "This suite attaches a session on whatever core CORE names, and used to\n"
        "default to the live core at http://127.0.0.1:8770.\n"
        "    CORE=http://127.0.0.1:9131 .venv/bin/python web/image_test.py")
CHROME = shutil.which("google-chrome") or shutil.which("chromium")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("ok    " if ok else "FAIL  ") + name + (f"\n        {detail}" if not ok and detail else ""))


async def run(http):
    async with http.put(f"http://127.0.0.1:9222/json/new?{CORE}/?session=imgtest") as r:
        info = await r.json()
    ws = await http.ws_connect(info["webSocketDebuggerUrl"], max_msg_size=0)
    i = [0]

    async def js(expr):
        i[0] += 1
        mid = i[0]
        await ws.send_json({"id": mid, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True,
                                       "awaitPromise": True}})
        while True:
            m = await asyncio.wait_for(ws.receive(), 30)
            d = json.loads(m.data)
            if d.get("id") == mid:
                res = d.get("result", {})
                if "exceptionDetails" in res:
                    raise RuntimeError(str(res["exceptionDetails"])[:300])
                return res.get("result", {}).get("value")

    for m in ("Runtime.enable", "Page.enable"):
        i[0] += 1
        await ws.send_json({"id": i[0], "method": m, "params": {}})

    tok = ""
    cfg = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    if os.path.exists(cfg):
        mt = re.search(r'^\s*token:\s*"([^"]*)"', open(cfg).read(), re.M)
        tok = mt.group(1) if mt else ""
    await asyncio.sleep(1.5)
    if tok:
        await js(f"localStorage.setItem('letsclaw.token', {json.dumps(tok)})")
        i[0] += 1
        await ws.send_json({"id": i[0], "method": "Page.reload", "params": {}})
        await asyncio.sleep(3)

    check("page has a token to send", bool(await js("S && !!S.token")) or not tok)

    # Inside #log, because that is where the app's delegated handlers live: the
    # click that opens the lightbox and the error that swaps a missing image for
    # a note are both listeners on #log, and every real message renders there.
    # A probe hung off document.body would be outside all of them and would test
    # nothing. On-screen too -- loading="lazy" never fetches an image that is
    # never in the viewport, and whether it loads is the whole point here.
    await js("(() => { const d=document.createElement('div'); d.className='body'; d.id='P';"
             "d.style.cssText='width:700px';"
             "document.getElementById('log').appendChild(d); return true })()")

    async def render(md):
        return await js("document.getElementById('P').innerHTML = renderMd(" +
                        json.dumps(md) + "), true")

    # --- a real image paints -------------------------------------------------
    await render("Here is the run:\n\n![the plot](testplot.png)\n")
    check("img element created", await js("!!document.querySelector('#P img.md-img')"))
    ok = await js("""(async () => {
      const im = document.querySelector('#P img.md-img');
      if (!im) return 'no img';
      if (!im.complete) await new Promise(r => {
        im.onload = r; im.onerror = r; setTimeout(r, 8000);
      });
      return im.naturalWidth + 'x' + im.naturalHeight;
    })()""")
    check("image actually decoded", ok == "900x420", f"naturalSize={ok}")
    check("src points at /files with the token",
          await js("/^\\/files\\/testplot\\.png(\\?token=)?/.test("
                   "document.querySelector('#P img.md-img').getAttribute('src'))"))

    # --- bounded by the column ----------------------------------------------
    box = await js("""(() => {
      const im = document.querySelector('#P img.md-img');
      const p  = document.getElementById('P');
      return {img: Math.round(im.getBoundingClientRect().width),
              col: Math.round(p.getBoundingClientRect().width),
              maxh: getComputedStyle(im).maxHeight};
    })()""")
    check("wide image is bounded by the column",
          box and box["img"] <= box["col"] + 1, str(box))
    check("tall image is capped by max-height", box and box["maxh"].endswith("px"), str(box))

    # --- a missing file degrades --------------------------------------------
    await render("![gone](nosuchfile.png)")
    await asyncio.sleep(1.2)
    check("missing image becomes a note, not a broken icon",
          await js("!!document.querySelector('#P .md-img-missing')"))
    check("the note names the image",
          "gone" in (await js("(document.querySelector('#P .md-img-missing')||{}).textContent || ''")))

    # --- refusals reach the DOM as text -------------------------------------
    await render("![x](http://evil.example/a.png) and ![y](../../etc/passwd)")
    check("no img for a remote src", not await js("!!document.querySelector('#P img')"))
    check("no outbound link either", not await js("!!document.querySelector('#P a')"))

    # --- the lightbox --------------------------------------------------------
    await render("![p](testplot.png)")
    await asyncio.sleep(.8)
    await js("document.querySelector('#P img.md-img').click()")
    await asyncio.sleep(.4)
    check("clicking opens the lightbox", await js("!!document.querySelector('.lightbox img')"))
    await js("document.querySelector('.lightbox').click()")
    await asyncio.sleep(.3)
    check("clicking again closes it", not await js("!!document.querySelector('.lightbox')"))

    # --- auto-scroll follows a late-loading image ----------------------------
    # The real failure this guards: an image settles its height after the append
    # that scrolled the log, so the view lands mid-plot and stays there.
    stuck = await js("""(async () => {
      const log = document.getElementById('log');
      log.innerHTML = '';
      follow = true;
      for (let i = 0; i < 25; i++) {
        const d = document.createElement('div');
        d.className = 'msg llm';
        d.innerHTML = '<div class="body">filler line ' + i + '</div>';
        log.appendChild(d);
      }
      log.scrollTop = log.scrollHeight;
      const d = document.createElement('div');
      d.className = 'msg llm';
      d.innerHTML = '<div class="body">' + renderMd('![p](testplot.png?v=' + Date.now() + ')') + '</div>';
      log.appendChild(d);
      log.scrollTop = log.scrollHeight;
      const im = d.querySelector('img');
      if (im && !im.complete) await new Promise(r => {
        im.onload = r; im.onerror = r; setTimeout(r, 8000);
      });
      await new Promise(r => setTimeout(r, 400));
      return log.scrollHeight - log.scrollTop - log.clientHeight;
    })()""")
    check("log is still at the bottom after an image loads",
          stuck is not None and stuck < 80, f"gap={stuck}px")

    await ws.close()
    async with http.get(f"http://127.0.0.1:9222/json/close/{info['id']}"):
        pass


async def main():
    if not CHROME:
        print("no chrome found")
        return 1
    profile = tempfile.mkdtemp(prefix="letsclaw-img-")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--remote-debugging-port=9222",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
         "--disable-gpu", "--window-size=1280,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    async with aiohttp.ClientSession() as http:
        for _ in range(60):
            try:
                async with http.get("http://127.0.0.1:9222/json/version") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.25)
        else:
            print("chrome never opened the debug port")
            return 1
        try:
            await run(http)
        finally:
            chrome.terminate()
            shutil.rmtree(profile, ignore_errors=True)
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f" — FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
