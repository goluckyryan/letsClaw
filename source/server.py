#!/usr/bin/env python3
"""server.py — the letsClaw core service.

Start this first; the terminal, a WebUI and a Discord bot are all just clients:

    ./serve.sh            (or: python source/server.py)
    ./terminalUI.sh       (terminal client, in another shell)

HTTP:  GET /health  GET /models  GET /sessions  DELETE /sessions/<name>
WS:    /ws?session=<name>&last_seq=<n>

The reader never runs a turn inline — turns are tasks, so the socket stays
readable for `stop` and for the rollover answer while a turn is in flight.
"""

import argparse
import asyncio
import logging
import sys
import weakref
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

import core
import paths

logger = logging.getLogger("letclaw.server")

# The browser assets stay at the repo root, beside the code rather than inside it.
WEB_DIR = paths.REPO_ROOT / "web"


def _configure_logging(config):
    """config.yaml's logging: section was never wired up; a daemon needs it."""
    cfg = config.get("logging", {})
    level = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stderr)]
    path = cfg.get("file")
    if path:
        try:
            p = paths.resolve(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(p))
        except OSError as e:
            print(f"⚠️  cannot log to {path}: {e}", file=sys.stderr)
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # Third-party chatter at the configured level would drown the app's own
    # lines: the WebUI polls /sessions every 5 s (aiohttp.access), and every
    # model call is logged by httpx. Both go to WARNING.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


def _authorised(request):
    token = request.app["config"].get("core", {}).get("token") or ""
    if not token:
        return True
    sent = (request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or request.query.get("token", ""))
    return sent == token


async def health(request):
    mgr = request.app["mgr"]
    return web.json_response({"ok": True, "proto": core.PROTOCOL_VERSION,
                              "sessions": len(mgr.sessions)})


async def models(request):
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    cfg = request.app["config"]
    return web.json_response({
        "default": cfg.get("models", {}).get("default_model"),
        "configured": core.known_models(cfg),
    })


async def sessions(request):
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    return web.json_response({"sessions": request.app["mgr"].describe()})


async def delete_session(request):
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    name = request.match_info["name"]
    result = await request.app["mgr"].delete(name)
    if not result["ok"]:
        # 404 for "no such session", 409 for "it is busy" — the client shows the
        # second to the user and can retry it, which is not true of the first.
        status = 404 if result["error"].startswith("no session") else 409
        return web.json_response(result, status=status)
    return web.json_response(result)


async def reload_config(request):
    """Re-read config.yaml in place. 400 and no change if the file will not load."""
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    result = await request.app["mgr"].reload()
    return web.json_response(result, status=200 if result["ok"] else 400)


async def rename_session(request):
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"ok": False, "error": "malformed JSON"}, status=400)
    result = await request.app["mgr"].rename(request.match_info["name"],
                                             body.get("to"))
    if not result["ok"]:
        # 404 nothing to rename · 409 the new name is taken or a turn is running ·
        # 400 the name itself is unusable. Only the middle one is worth retrying.
        if result["error"].startswith("no session"):
            status = 404
        elif result["error"].startswith("a name must"):
            status = 400
        else:
            status = 409
        return web.json_response(result, status=status)
    return web.json_response(result)


async def index(request):
    """The WebUI. Unauthenticated even when core.token is set — the page holds
    no secrets, and it is the thing that asks for the token."""
    return web.FileResponse(WEB_DIR / "index.html",
                            headers={"Cache-Control": "no-cache"})


async def _no_cache_static(request, response):
    """Make the browser revalidate app.js and style.css on every load.

    index.html already says no-cache, but it references the other two by a fixed
    path. Without this, a browser is free to serve them from its own cache for
    as long as it likes — and a page that keeps running last week's app.js
    against this week's core is a bug report nobody can reproduce.
    """
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")


async def _writer(ws, sub):
    """Owns the socket. The turn never touches it, so a stalled peer can't
    stall generation — it just overflows its queue and gets dropped."""
    while not ws.closed:
        if not sub.queue:
            sub.wake.clear()
            try:
                await asyncio.wait_for(sub.wake.wait(), timeout=25)
            except asyncio.TimeoutError:
                pass
            continue
        if sub.overflowed:
            logger.warning("dropping a client that could not keep up")
            await ws.close(code=WSCloseCode.TRY_AGAIN_LATER,
                           message=b"too slow; reconnect and replay")
            return
        event = sub.queue.popleft()
        try:
            await ws.send_json(event)
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            return


async def ws_handler(request):
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    mgr = request.app["mgr"]
    ws = web.WebSocketResponse(heartbeat=20)  # detect half-open peers
    await ws.prepare(request)
    # Tracked so shutdown can close it. Weak, so a socket that goes away on its
    # own is not kept alive here by the bookkeeping meant to tidy it up.
    request.app["websockets"].add(ws)

    name = request.query.get("session", "terminal")
    last_seq = int(request.query.get("last_seq", 0) or 0)
    want_reasoning = request.query.get("reasoning", "1") != "0"

    try:
        # ?model= applies only where mgr.get() uses it — when this attach is what
        # creates the session. Re-attaching never switches an existing session's
        # model: that is /model's job, and doing it silently on reconnect would
        # move a conversation out from under whoever else is attached to it.
        session = await mgr.get(name, request.query.get("model"))
    except ValueError as e:
        await ws.send_json(core.ev("error", msg=str(e), fatal=True))
        await ws.close()
        return ws

    sub = core.Subscriber(wants_reasoning=want_reasoning)
    # Attach, then greet, then start the writer — in that order. Attaching first
    # means no event emitted during the greeting is lost; starting the writer
    # last means none can overtake the greeting and reach the client before it.
    session.attach(sub)
    await ws.send_json(session.snapshot(last_seq))
    writer = asyncio.create_task(_writer(ws, sub))
    logger.info("client attached to %s (%d total)", name, len(session.subscribers))

    turn = None
    try:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                data = msg.json()
            except ValueError:
                await ws.send_json(core.ev("error", msg="malformed JSON", fatal=False))
                continue
            kind = data.get("t")

            # Someone deleted this session out from under us. The socket is fine;
            # re-home it onto a fresh one of the same name rather than let this
            # client go on driving an object the registry no longer lists.
            if session.dead and kind in ("submit", "command"):
                # session.name, not the name captured at attach: a rename moves this
                # client with the session, so re-homing must follow it there rather
                # than resurrect whatever the session used to be called.
                gone = session.name
                session.detach(sub)
                session = await mgr.get(gone)
                session.attach(sub)
                await ws.send_json(session.snapshot())
                logger.info("re-homed a client onto a new %s after delete", gone)

            if kind == "submit":
                text = (data.get("text") or "").strip()
                if not text:
                    continue
                if turn and not turn.done():
                    await ws.send_json(core.ev("busy", reason="a turn is already running"))
                    continue
                # Who is speaking, when the client knows something the session
                # name does not — the Discord bot sends "discord:<username>" so
                # one channel's session still shows who typed. Clamped because
                # it is client-supplied and rides in every turn_start; falls
                # back to the session name, which is what it always used to be.
                origin = str(data.get("origin") or name)[:64] or name
                # A task, not an await: the reader must stay live to receive
                # `stop` and `rollover_reply` while the turn is running.
                turn = asyncio.create_task(session.run_turn(text, origin=origin))
            elif kind == "command":
                reply = await session.command(data.get("name", ""), data.get("args", ""))
                reply.update({"t": "response", "request_id": data.get("request_id")})
                await ws.send_json(reply)          # to this client only
            elif kind == "rollover_reply":
                ok = session.answer_rollover(data.get("request_id"), data.get("yes"))
                if not ok:
                    logger.debug("stale or duplicate rollover reply ignored")
            elif kind == "stop":
                if turn and not turn.done():
                    turn.cancel()
            elif kind == "ping":
                await ws.send_json(core.ev("pong"))
    finally:
        session.detach(sub)
        sub.wake.set()
        writer.cancel()
        logger.info("client left %s (%d remain)", name, len(session.subscribers))
    return ws


async def _on_startup(app):
    """Bring back the sessions the last run left on disk.

    Before the first request is served, so GET /sessions — and therefore the
    WebUI sidebar — is right on its very first paint.
    """
    try:
        n = await app["mgr"].restore()
    except Exception:
        logger.exception("could not restore saved sessions — starting empty")
        return
    if n:
        logger.info("restored %d session(s) from disk", n)
        print(f"   restored {n} session(s)")
        sys.stdout.flush()


async def _on_shutdown(app):
    # Close the sockets first, and actually close them. Detaching only stops
    # events reaching a client; the handler stays parked in `async for msg in
    # ws` waiting for a message an idle client will never send, and aiohttp
    # waits for that handler before it will let the process die. Without this
    # a Ctrl-C with one client attached took ~31 s — the time for the heartbeat
    # ping and its pong timeout to fail and tear the connection down — and
    # longer still with the heartbeat off. With it, shutdown is immediate, and
    # clients get a close frame instead of a socket that stops answering.
    for ws in set(app["websockets"]):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"core shutting down")
    for s in app["mgr"].sessions.values():
        for sub in list(s.subscribers):
            s.detach(sub)
    await app["mgr"].close()


def build_app(config, config_path=None):
    app = web.Application()
    app["config"] = config
    app["config_path"] = config_path
    app["mgr"] = core.SessionManager(config, config_path)
    app["websockets"] = weakref.WeakSet()
    app.add_routes([
        web.get("/health", health),
        web.get("/models", models),
        web.get("/sessions", sessions),
        web.delete("/sessions/{name}", delete_session),
        web.post("/sessions/{name}/rename", rename_session),
        web.post("/reload", reload_config),
        web.get("/ws", ws_handler),
    ])
    if (WEB_DIR / "index.html").exists():
        app.add_routes([web.get("/", index), web.static("/static", WEB_DIR)])
        app.on_response_prepare.append(_no_cache_static)
    else:
        logger.warning("no %s — the WebUI will not be served", WEB_DIR)
    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    return app


def main():
    ap = argparse.ArgumentParser(description="letsClaw core service")
    ap.add_argument("--bind"); ap.add_argument("--port", type=int)
    ap.add_argument("--config")
    args = ap.parse_args()
    try:
        config = core.load_config(args.config)
    except (FileNotFoundError, Exception) as e:
        print(f"❌ {e}")
        sys.exit(1)
    _configure_logging(config)
    core_cfg = config.get("core", {})
    bind = args.bind or core_cfg.get("bind", "127.0.0.1")
    port = args.port or int(core_cfg.get("port", 8770))

    app = build_app(config, args.config)
    print(f"🧠 letsClaw core on ws://{bind}:{port}/ws  (proto {core.PROTOCOL_VERSION})")
    if (WEB_DIR / "index.html").exists():
        print(f"   WebUI:  http://{bind}:{port}/")
    print(f"   models: {', '.join(core.known_models(config)) or '(none)'}")
    if bind not in ("127.0.0.1", "localhost", "::1"):
        print("   ⚠️  not bound to loopback — tools are unconfined, so anyone who can "
              "reach this port can run shell commands here")
    # This is a daemon; its stdout is a log file far more often than a tty, and block
    # buffering would hold the banner back until something else filled the buffer.
    sys.stdout.flush()
    web.run_app(app, host=bind, port=port, print=None)


if __name__ == "__main__":
    main()
