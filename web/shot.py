#!/usr/bin/env python3
"""Screenshot the WebUI. Usage: web/shot.py <session> [out.png] [dark|light]"""
import asyncio
import base64
import shutil
import subprocess
import sys
import tempfile

import aiohttp

sess = sys.argv[1] if len(sys.argv) > 1 else "web"
out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/letsclaw.png"
theme = sys.argv[3] if len(sys.argv) > 3 else "dark"


async def main():
    profile = tempfile.mkdtemp(prefix="letsclaw-shot-")
    chrome = subprocess.Popen(
        [shutil.which("google-chrome"), "--headless=new", "--remote-debugging-port=9223",
         f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu",
         "--force-device-scale-factor=2", "--window-size=1280,860", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    async with aiohttp.ClientSession() as http:
        for _ in range(60):
            try:
                async with http.get("http://127.0.0.1:9223/json/version") as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.25)
        async with http.put(
                f"http://127.0.0.1:9223/json/new?http://127.0.0.1:8770/?session={sess}") as r:
            info = await r.json()
        ws = await http.ws_connect(info["webSocketDebuggerUrl"], max_msg_size=0)
        n = 0

        async def cmd(method, **params):
            nonlocal n
            n += 1
            await ws.send_json({"id": n, "method": method, "params": params})
            while True:
                d = (await ws.receive_json())
                if d.get("id") == n:
                    return d

        await cmd("Emulation.setEmulatedMedia", features=[
            {"name": "prefers-color-scheme", "value": theme}])
        await asyncio.sleep(4)
        r = await cmd("Page.captureScreenshot", format="png")
        with open(out, "wb") as f:
            f.write(base64.b64decode(r["result"]["data"]))
        print(out)
        await ws.close()
    chrome.terminate()
    shutil.rmtree(profile, ignore_errors=True)


asyncio.run(main())
