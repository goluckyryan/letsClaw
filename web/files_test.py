#!/usr/bin/env python3
"""The /files route: confinement, extension gate, auth.

    .venv/bin/python web/files_test.py

This is the one route that turns model output into a filesystem read, so most
of what matters here is refusals: a traversal, a symlink pointing out of the
root, a path that is not an image, a bad token. Runs against an aiohttp test
server -- no core, no browser, no network, nothing to clean up.
"""
import asyncio, sys, tempfile, os
sys.path.insert(0, '/home/ryan/letsClaw/source')
sys.path.insert(0, '/home/ryan/letsClaw')
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
import server as srv

results=[]
def check(n, ok, d=""):
    results.append((n,bool(ok))); print(("ok    " if ok else "FAIL  ")+n+(f"  {d}" if not ok else ""))

async def main():
    root = tempfile.mkdtemp(prefix="filesroot-")
    outside = tempfile.mkdtemp(prefix="outside-")
    # a real png
    png = bytes.fromhex('89504e470d0a1a0a') + b'\x00'*40
    open(os.path.join(root,'plot.png'),'wb').write(png)
    open(os.path.join(root,'notes.txt'),'w').write('secret-ish')
    open(os.path.join(outside,'secret.png'),'wb').write(b'OUTSIDE')
    os.symlink(os.path.join(outside,'secret.png'), os.path.join(root,'link.png'))
    os.makedirs(os.path.join(root,'sub'), exist_ok=True)
    open(os.path.join(root,'sub','deep.png'),'wb').write(png)

    cfg = {"webui":{"files_dir":root}, "core":{"token":"tok"},
           "models":{}, "conversation":{}, "tools":{"enabled":False}}
    app = web.Application()
    app["config"]=cfg
    app.add_routes([web.get("/files/{name:.*}", srv.files)])
    client = TestClient(TestServer(app)); await client.start_server()

    async def get(path, token="tok"):
        q = f"?token={token}" if token is not None else ""
        r = await client.get(f"/files/{path}{q}")
        return r.status, await r.read(), r.headers

    s,b,h = await get("plot.png")
    check("serves a png", s==200 and b.startswith(b'\x89PNG'), f"status={s}")
    check("content-type is image/png", h.get("Content-Type")=="image/png", h.get("Content-Type"))
    check("nosniff set", h.get("X-Content-Type-Options")=="nosniff")
    check("CSP set", "default-src 'none'" in (h.get("Content-Security-Policy") or ""))

    s,_,_ = await get("notes.txt")
    check("refuses a non-image extension", s==404, f"status={s}")

    s,b,_ = await get("link.png")
    check("refuses a symlink pointing outside the root", s==404 and b'OUTSIDE' not in b, f"status={s}")

    for p in ("../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "....//etc/passwd",
              "/etc/passwd", "%2e%2e%2fsecret.png"):
        s,b,_ = await get(p)
        check(f"refuses traversal {p!r}", s==404 and b'root:' not in b, f"status={s}")

    s,_,_ = await get("sub/deep.png")
    check("a subdirectory file is still inside the root (allowed)", s==200, f"status={s}")

    s,_,_ = await get("plot.png", token="wrong")
    check("wrong token refused", s==401, f"status={s}")
    s,_,_ = await get("plot.png", token=None)
    check("no token refused", s==401, f"status={s}")

    s,_,_ = await get("nope.png")
    check("missing file is 404", s==404, f"status={s}")

    # no token configured -> open
    cfg["core"]["token"]=""
    s,_,_ = await get("plot.png", token=None)
    check("open when no core.token is set", s==200, f"status={s}")

    await client.close()
    bad=[n for n,ok in results if not ok]
    print(f"\n{len(results)-len(bad)}/{len(results)} passed" + (f" FAILED {bad}" if bad else ""))
    return 1 if bad else 0
raise SystemExit(asyncio.run(main()))
