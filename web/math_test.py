#!/usr/bin/env python3
"""Maths rendering in a real browser.

    .venv/bin/python web/math_test.py

node web/test_md.mjs and node web/test_math.mjs cover the renderer as a pure
function. They cannot see the things that only exist in a page: whether the
vendored KaTeX actually loads over HTTP, whether a single font 404s, whether
the glyphs get real metrics instead of collapsing to zero width, and whether
the formula is legible against the theme's background.

This drives headless Chrome against a running core (CORE, default the real one
on 8770). It only ever loads the page and calls renderMd in the tab -- it never
sends a turn, so it costs no tokens and touches no session's history.
"""
import asyncio, json, os, shutil, subprocess, tempfile, time
import aiohttp

CORE = os.environ.get("CORE", "http://127.0.0.1:8770")
CHROME = shutil.which("google-chrome") or shutil.which("chromium")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("ok    " if ok else "FAIL  ") + name + (f"\n        {detail}" if not ok and detail else ""))


async def js(ws, state, expr):
    state["id"] += 1
    mid = state["id"]
    await ws.send_json({"id": mid, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True,
                                   "awaitPromise": True}})
    while True:
        m = await asyncio.wait_for(ws.receive(), 30)
        d = json.loads(m.data)
        if d.get("id") == mid:
            r = d.get("result", {})
            if "exceptionDetails" in r:
                raise RuntimeError(str(r["exceptionDetails"])[:300])
            return r.get("result", {}).get("value")
        if d.get("method") == "Network.responseReceived":
            p = d["params"]["response"]
            state["responses"].append((p["url"], p["status"]))


async def run(http):
    # A fresh session name that we attach to but never speak in; the core makes
    # it on attach and it stays empty.
    url = f"{CORE}/?session=mathtest{int(time.time()) % 100000}"
    async with http.put(f"http://127.0.0.1:9222/json/new?{url}") as r:
        info = await r.json()
    ws = await http.ws_connect(info["webSocketDebuggerUrl"], max_msg_size=0)
    state = {"id": 0, "responses": []}
    for m in ("Runtime.enable", "Page.enable", "Network.enable"):
        state["id"] += 1
        await ws.send_json({"id": state["id"], "method": m, "params": {}})

    # Let the page settle: token gate (if any), scripts, fonts.
    tok = ""
    cfg = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    if os.path.exists(cfg):
        import re
        m = re.search(r"^\s*token:\s*\"([^\"]*)\"", open(cfg).read(), re.M)
        tok = m.group(1) if m else ""
    await asyncio.sleep(1.5)
    if tok:
        await js(ws, state, f"localStorage.setItem('letsclaw.token', {json.dumps(tok)})")
        state["id"] += 1
        await ws.send_json({"id": state["id"], "method": "Page.reload", "params": {}})
        await asyncio.sleep(2.5)
    else:
        await asyncio.sleep(1.0)

    # Everything before this point was the un-tokened first load, whose 401 on
    # /models is the gate working as designed. The asset check below is about
    # what this change added, so start its log from the real, authorised page.
    state["responses"].clear()

    # --- the assets actually loaded -----------------------------------------
    check("katex.min.js loaded", await js(ws, state, "typeof katex !== 'undefined'"),
          "the vendored bundle did not define the global")
    check("katex version reported", bool(await js(ws, state, "window.katex && katex.version")),
          str(await js(ws, state, "window.katex ? katex.version : null")))
    check("stylesheet applied", await js(ws, state,
          "[...document.styleSheets].some(s => (s.href||'').includes('katex'))"))

    # --- render into a detached node: no session history is touched ----------
    setup = """
    (() => {
      const d = document.createElement('div');
      d.className = 'body'; d.id = 'mathprobe';
      d.style.cssText = 'position:fixed;left:-9999px;top:0;width:700px';
      document.body.appendChild(d);
      return true;
    })()"""
    await js(ws, state, setup)

    async def render(md):
        return await js(ws, state,
                        "document.getElementById('mathprobe').innerHTML = renderMd(" +
                        json.dumps(md) + "), true")

    await render("inline $E = mc^2$ and display:\n\n$$\\int_0^\\infty e^{-x}\\,dx = 1$$\n")
    check("katex element in the DOM",
          await js(ws, state, "!!document.querySelector('#mathprobe .katex')"))
    check("display maths is a block",
          await js(ws, state, "!!document.querySelector('#mathprobe .katex-display')"))
    check("no fallback span (katex did the work)",
          await js(ws, state, "!document.querySelector('#mathprobe .math-raw')"))

    # Zero width = the font never arrived, or the CSS did not apply.
    w = await js(ws, state,
                 "document.querySelector('#mathprobe .katex').getBoundingClientRect().width")
    check("rendered maths has real width", (w or 0) > 20, f"width={w}")
    h = await js(ws, state,
                 "document.querySelector('#mathprobe .katex-display').getBoundingClientRect().height")
    check("display maths has real height", (h or 0) > 10, f"height={h}")

    # The KaTeX fonts must actually be in use, not silently substituted.
    fam = await js(ws, state,
                   "getComputedStyle(document.querySelector('#mathprobe .katex .mord')).fontFamily")
    check("KaTeX font family in use", "KaTeX" in (fam or ""), f"font-family={fam}")
    # document.fonts.check() wants a size+family it has already resolved; the
    # reliable question is which faces the page loaded, so ask the registry.
    # Fonts are fetched lazily when a glyph first needs them: wait for the
    # fetches this render kicked off before asking what loaded.
    await js(ws, state, "document.fonts.ready.then(() => true)")
    fams = await js(ws, state,
                    "[...document.fonts].filter(f => f.family.startsWith('KaTeX'))"
                    ".map(f => f.family + ':' + f.status).join(',')")
    check("KaTeX faces registered and loaded",
          "KaTeX_Main:loaded" in (fams or "") and "KaTeX_Math:loaded" in (fams or ""),
          f"faces={fams}")

    # Inherits the bubble colour rather than KaTeX's own black.
    col = await js(ws, state,
                   "getComputedStyle(document.querySelector('#mathprobe .katex')).color")
    body = await js(ws, state, "getComputedStyle(document.body).color")
    check("maths takes the theme's text colour", col == body, f"katex={col} body={body}")

    # --- a wide equation must scroll, not stretch the pane -------------------
    await render("$$" + " + ".join(f"x_{{{i}}}^{{2}}" for i in range(40)) + "$$")
    over = await js(ws, state, """(() => {
      const d = document.querySelector('#mathprobe .katex-display');
      return {scroll: d.scrollWidth, client: d.clientWidth,
              ox: getComputedStyle(d).overflowX};
    })()""")
    check("wide equation scrolls instead of stretching",
          over and over["ox"] in ("auto", "scroll") and over["scroll"] > over["client"],
          str(over))

    # --- error path ----------------------------------------------------------
    await render("$\\notarealmacro{x}$")
    check("bad macro renders something rather than throwing",
          bool(await js(ws, state, "document.getElementById('mathprobe').innerHTML.length > 10")))
    check("bad macro is error-coloured",
          await js(ws, state,
                   "document.getElementById('mathprobe').innerHTML.includes('color:var(--err)')"))

    # --- the regression that motivated the change ----------------------------
    await render("$a^*b^*$ and $x_1$")
    check("no <em> inside maths",
          not await js(ws, state, "!!document.querySelector('#mathprobe em')"))

    # --- nothing 404'd -------------------------------------------------------
    await js(ws, state, "true")   # drain
    bad = [(u, s) for u, s in state["responses"] if s >= 400]
    check("no failed asset requests", not bad, str(bad[:5]))
    fonts = [(u, s) for u, s in state["responses"] if "fonts/KaTeX" in u]
    check("at least one KaTeX font fetched OK",
          any(s == 200 for _, s in fonts), str(fonts[:5]))

    await ws.close()
    async with http.get(f"http://127.0.0.1:9222/json/close/{info['id']}"):
        pass


async def main():
    if not CHROME:
        print("no chrome found")
        return 1
    profile = tempfile.mkdtemp(prefix="letsclaw-math-")
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
    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (f" — FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
