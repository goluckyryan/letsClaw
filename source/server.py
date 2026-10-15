#!/usr/bin/env python3
"""server.py — the letsClaw core service.

Start this first; the terminal, a WebUI and a Discord bot are all just clients:

    ./serve.sh            (or: python source/server.py)
    ./terminalUI.sh       (terminal client, in another shell)

HTTP:  GET /health  GET /models  GET /sessions  DELETE /sessions/<name>
       GET /sessions/<name>/history  POST /sessions/<name>/rename
WS:    /ws?session=<name>&last_seq=<n>

The reader never runs a turn inline — turns are tasks, so the socket stays
readable for `stop` and for the rollover answer while a turn is in flight.
"""

import argparse
import asyncio
import copy
import logging
import secrets
import shutil
import socket
import sys
import weakref
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSCloseCode, WSMsgType, web

import config as configlib
import core
import logging_setup
import paths

logger = logging.getLogger("letclaw.server")

# The browser assets stay at the repo root, beside the code rather than inside it.
WEB_DIR = paths.REPO_ROOT / "web"

# Where the agent puts pictures it wants shown. Not under web/: that directory is
# the app's own source, served unauthenticated to anyone who loads the page, and
# generated output does not belong in it. state/ is already gitignored.
DEFAULT_FILES_DIR = "state/files"
# Served by extension, so a mistyped path cannot turn this into a general
# arbitrary-file-read route for anyone holding the token.
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".ico": "image/x-icon", ".avif": "image/avif",
}


def _configure_logging(config):
    """config.yaml's logging: section was never wired up; a daemon needs it."""
    logging_setup.configure(config, "core")
    # Third-party chatter at the configured level would drown the app's own
    # lines: the WebUI polls /sessions every 5 s (aiohttp.access), and every
    # model call is logged by httpx. Both go to WARNING.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# Addresses that are not reachable from another machine. Used both by the
# startup check and by the banner, so the two cannot disagree about what
# "loopback" means.
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _split_host_port(host):
    """The hostname out of a Host header, with any port removed.

    Three shapes to get right, and the bracket one is why this is not a
    rsplit: `example:8770`, `[::1]:8770`, and a bare `[::1]` — splitting that
    last one on the final colon returns `:1]`, which matches nothing and would
    refuse a legitimate IPv6 loopback.
    """
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host[1:]
    # A bare IPv6 literal has several colons and no brackets. It is invalid in
    # a Host header, but refusing to parse it is friendlier than lopping a
    # group off the end and comparing the wreckage.
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _machine_hostnames(bind):
    """Every name this machine legitimately answers to, for the Host check.

    Deliberately generous. A name missing from here is a 403 on page load for
    someone who did nothing wrong, so this collects loopback, the bind, the
    machine's own hostname and FQDN, and every address those resolve to. What
    it can never collect is a name on someone else's nameserver, which is the
    whole point — see _host_ok.
    """
    names = {"localhost", "127.0.0.1", "::1"}
    if bind and bind not in ("0.0.0.0", "::"):
        names.add(bind.strip("[]").lower())
    try:
        hostname = socket.gethostname()
        names.add(hostname.lower())
        names.add(socket.getfqdn(hostname).lower())
        for info in socket.getaddrinfo(hostname, None):
            names.add(info[4][0].split("%")[0].lower())
    except (OSError, UnicodeError):
        # No DNS, no /etc/hosts entry for ourselves, an unresolvable hostname:
        # all survivable. Loopback is already in the set and that is the case
        # this check exists to keep working.
        pass
    lan = _lan_ip()
    if lan:
        names.add(lan)
    return names


def _host_ok(request):
    """Whether the Host header names this server, rather than merely agreeing
    with itself.

    This is what makes the Origin check below mean anything. `_same_origin`
    compares Origin to Host, and Host is chosen by the client — so a page on
    a domain whose A record has been re-pointed at 127.0.0.1 (DNS rebinding)
    sends a matching, entirely honest Origin and Host pair, and sails through.
    The browser is not lying and nothing is forged; the name itself moved.

    So the pair is only evidence once one half is pinned to something an
    attacker cannot choose: a name this machine actually answers to. Their
    nameserver can point any name at this address, but it cannot put that name
    in our allowlist. `core.allowed_hosts` extends it for a reverse proxy,
    which legitimately forwards a Host we have never heard of.
    """
    host = request.headers.get("Host", "")
    if not host:
        # HTTP/1.1 requires it and every browser and proxy sends it. Its
        # absence is not a case to be lenient about.
        return False
    extra = request.app["config"].get("core", {}).get("allowed_hosts") or []
    if isinstance(extra, str):
        extra = [extra]
    allowed = request.app.get("allowed_hosts", set()) | {
        str(h).strip().lower() for h in extra}
    return _split_host_port(host) in allowed


@web.middleware
async def _host_guard(request, handler):
    """Refuse anything addressed to a name that is not ours, before it routes.

    Here rather than in `_guard` because the rebinding payoff is not only the
    write routes: once the browser believes the attacker's page is same-origin
    with us, it can read `/sessions`, `/config` and whole conversations out of
    `/sessions/<name>/history` too. The page itself and `/theme.css` are in
    scope for the same reason — an unauthenticated stylesheet whose computed
    values can be read back is a reliable "letsClaw is running here" oracle.

    `/health` stays open: it is documented as needing nothing, it says only
    that the process is up, and it is what a monitor or a container probe
    calls — often through whatever name the orchestrator made up.
    """
    if request.path == "/health":
        return await handler(request)
    if not _host_ok(request):
        logger.warning("refused %s %s addressed to Host %r — not a name this "
                       "core answers to (add it to core.allowed_hosts if it "
                       "should be)", request.method, request.path,
                       request.headers.get("Host", ""))
        raise web.HTTPForbidden(reason="unrecognised Host header")
    return await handler(request)

# What /config shows in place of a secret. Non-empty, so the Settings panel can
# still tell that a token is set; nothing like a real token, so it cannot be
# mistaken for one or accidentally saved back as one.
REDACTED = "********"


def _authorised(request):
    token = request.app["config"].get("core", {}).get("token") or ""
    if not token:
        return True
    sent = (request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or request.query.get("token", ""))
    # compare_digest, not ==: string equality returns as soon as two bytes
    # differ, so its running time leaks how long a correct prefix was and a
    # patient client can recover the token one character at a time.
    #
    # Encoded first, because compare_digest raises TypeError on a str holding
    # anything outside ASCII. Unencoded, a token with an accent in it made
    # every authenticated request 500 for the life of the process — including
    # the owner's own WebUI — and let an unauthenticated client fill the log
    # with tracebacks by sending ?token=%C3%A9.
    return secrets.compare_digest(sent.encode("utf-8"), token.encode("utf-8"))


def _same_origin(request):
    """False only when a browser is driving this request from another site.

    Loopback is not a wall against the user's own browser: any page they visit
    can open ws://127.0.0.1:8770/ws and drive an unconfined `exec`. The defence
    is that browsers attach `Origin` to WebSocket handshakes and to every
    cross-origin request, and script cannot forge it — while non-browser
    clients (the bot, the terminal, curl) send none at all.

    So a missing Origin is allowed through, which keeps every existing client
    working and costs nothing: the attack this closes is specifically a browser,
    and a browser always tells us. An Origin that is present must name this very
    server, port included — another port on localhost is a different origin and
    is exactly where a hostile page would be served from.

    Note what this does NOT establish on its own. Host is a client-supplied
    header, so this equality says only that the browser is consistent with
    itself; a rebound DNS name satisfies it honestly. `_host_guard` is what
    pins Host to a name that is really ours, and the two together are the
    check this docstring used to claim to be by itself.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    # "null" (a sandboxed iframe, a file:// page) parses to an empty netloc and
    # is refused along with anything else that does not name a host.
    netloc = urlsplit(origin).netloc
    return bool(netloc) and netloc == request.headers.get("Host", "")


def _guard(request):
    """Both gates a state-changing route needs: who is asking, and from where.

    Read-only routes need only the first — we send no CORS headers, so a hostile
    page can fire a cross-origin GET but cannot read what comes back. A write
    lands whether or not the reply is readable, and a WebSocket is exempt from
    the same-origin policy altogether, which is why those two take both checks.
    """
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    if not _same_origin(request):
        logger.warning("refused a cross-origin %s %s from Origin %r",
                       request.method, request.path, request.headers.get("Origin"))
        raise web.HTTPForbidden(reason="cross-origin request refused")


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
        "configured": configlib.known_models(cfg),
        "forwards": request.app["mgr"].forwards.status(),
    })


async def sessions(request):
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    return web.json_response({"sessions": request.app["mgr"].describe()})


async def session_history(request):
    """The session's messages, newest last — the tail a client wants to show.

    Read-only and capped: a switch digest needs the last few lines, not the
    whole conversation, and an unbounded copy of a long session would be a
    slow JSON for no one."""
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    name = request.match_info["name"]
    try:
        limit = int(request.query.get("limit", 10))
    except ValueError:
        limit = 10
    session = request.app["mgr"].sessions.get(name)
    if session is None:
        return web.json_response({"ok": False, "error": f"no session {name!r}"},
                                 status=404)
    msgs = [m for m in session.history if m.get("role") != "system"]
    return web.json_response({
        "ok": True, "session": session.name,
        "messages": [{"role": m["role"], "content": m.get("content")}
                     for m in msgs[-max(limit, 1):]],
    })


async def delete_session(request):
    _guard(request)
    name = request.match_info["name"]
    result = await request.app["mgr"].delete(name)
    if not result["ok"]:
        # 404 for "no such session", 409 for "it is busy" — the client shows the
        # second to the user and can retry it, which is not true of the first.
        status = 404 if result["error"].startswith("no session") else 409
        return web.json_response(result, status=status)
    return web.json_response(result)


def _redacted(config):
    """The config with every secret replaced by a placeholder.

    /config is authenticated, but authentication is not the point here. The
    point is that a secret which reaches the browser is then in devtools, in a
    HAR export, and on screen during a demo or a screen-share. Three kinds live
    in this file and none of them has any business in a response body:

      * core.token — the credential for this very service;
      * discord.token — a credential for someone else's, and holding the core
        token should not hand anyone the bot;
      * models.*.api_key — third-party keys that cost real money.

    Redacted rather than dropped, so the Settings panel can still distinguish
    "set" from "unset"; the panel does not offer to reveal them, because there
    is nothing here to reveal.
    """
    safe = copy.deepcopy(config)
    for section, key in (("core", "token"), ("discord", "token")):
        block = safe.get(section)
        if isinstance(block, dict) and block.get(key):
            block[key] = REDACTED
    # models: is a mapping of name -> settings, each of which may carry a key.
    models = safe.get("models")
    if isinstance(models, dict):
        for entry in models.values():
            if isinstance(entry, dict) and entry.get("api_key"):
                entry["api_key"] = REDACTED
    return safe


async def config_get(request):
    """The live config dict, for the Settings panel. Authenticated like the
    other state reads, and with the two tokens redacted on the way out."""
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    return web.json_response({
        "path": str(request.app["config_path"]),
        "config": _redacted(request.app["config"]),
    })


async def config_save(request):
    """Save the webui: section and apply it live. Only webui is accepted:
    it is the one section whose change is safe to apply without a restart
    and that a browser should not be editing byte-for-byte."""
    _guard(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"ok": False, "error": "malformed JSON"}, status=400)
    webui = body.get("webui") if isinstance(body, dict) else None
    if not isinstance(webui, dict):
        return web.json_response(
            {"ok": False, "error": "body must be {'webui': {...}}"}, status=400)
    result = await request.app["mgr"].save_webui(webui)
    return web.json_response(result, status=200 if result["ok"] else 400)


async def reload_config(request):
    """Re-read config.yaml in place. 400 and no change if the file will not load."""
    _guard(request)
    result = await request.app["mgr"].reload()
    return web.json_response(result, status=200 if result["ok"] else 400)


async def rename_session(request):
    _guard(request)
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


def _theme_css(config):
    """The pin's style as CSS custom properties, from config's webui.pin.

    Generated rather than served from a file: config.yaml is the one place a
    setting lives, and /reload picks an edit up without a restart. A tab that
    is already open gets the same values over the hello/reloaded events
    (applyPin in app.js), because a browser will not re-fetch this stylesheet
    on its own.
    """
    pin = configlib.config_pin(config)
    lines = ["/* generated from config.yaml — webui.pin; /reload re-themes */", ":root {"]
    for key in configlib.PIN_KEYS:
        if key not in pin:
            continue
        if not configlib.valid_pin_value(pin[key]):
            # Hand-edited into config.yaml, or saved before save_webui checked.
            # Dropping the one property is the entire cost of refusing it;
            # serving it would put attacker-chosen CSS on an unauthenticated
            # route in every open tab.
            logger.warning("webui.pin.%s is not a usable CSS value; ignoring it", key)
            continue
        # CSS custom properties are case-sensitive and do NOT treat _ as
        # - — --pin-font_size is a different name from --pin-font-size.
        lines.append(f"  --pin-{key.replace('_', '-')}: {pin[key]};")
    lines.append("}")
    return web.Response(text="\n".join(lines) + "\n", content_type="text/css",
                        headers={"Cache-Control": "no-cache"})


async def theme(request):
    """The WebUI's configurable style. Unauthenticated like / — it holds no
    secrets, and the page that loads it is the thing that holds the token."""
    return _theme_css(request.app["config"])


def files_root(config):
    """The directory /files serves, as an absolute path."""
    configured = (config.get("webui", {}) or {}).get("files_dir") or DEFAULT_FILES_DIR
    return paths.resolve(configured).resolve()


async def files(request):
    """An image the agent produced, for the WebUI to show inline.

    Authorised like everything else, but by query token rather than header: this
    is fetched by the browser's own <img> loader, which cannot be given an
    Authorization header. app.js appends core.token the same way the websocket
    does.

    Two independent guards on the path, because this is the one route that turns
    model output into a filesystem read: the name is resolved and then required
    to sit under the root, which defeats both ../ and a symlink pointing out of
    it, and the extension must be an image. A model that writes a path into its
    own answer cannot make either of those pass for /etc/shadow.
    """
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    root = files_root(request.app["config"])
    try:
        target = (root / request.match_info["name"]).resolve()
    except (OSError, ValueError):
        raise web.HTTPNotFound()
    # is_relative_to: the resolved path must still be inside the root.
    if not target.is_relative_to(root) or not target.is_file():
        raise web.HTTPNotFound()
    ctype = IMAGE_TYPES.get(target.suffix.lower())
    if not ctype:
        raise web.HTTPNotFound()
    return web.FileResponse(target, headers={
        "Content-Type": ctype,
        # An SVG opened in its own tab is a document and could carry script; in
        # an <img> it cannot. Both are covered: deny it every resource and stop
        # the browser second-guessing the type we just declared.
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
        # The agent overwrites a plot under the same name all the time.
        "Cache-Control": "no-cache",
    })


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
        except asyncio.CancelledError:
            raise                       # the handler's finally, on the way out
        except (ConnectionResetError, RuntimeError):
            return                      # the peer went away; nothing to report
        except Exception:
            # Anything else — a payload json cannot serialise, an aiohttp
            # connection error that is neither of the above — used to end this
            # task silently while the reader stayed parked in `async for`. The
            # socket stayed open, the client went on sending, and nothing ever
            # came back: the UI just stopped updating with no error anywhere.
            # Close it instead, so the client sees the drop and reconnects.
            logger.exception("writer failed on a %s event; closing the socket",
                             (event or {}).get("t"))
            try:
                await ws.close(code=WSCloseCode.INTERNAL_ERROR,
                               message=b"writer failed; reconnect and replay")
            except Exception:
                logger.debug("close after writer failure", exc_info=True)
            return


async def ws_handler(request):
    _guard(request)
    mgr = request.app["mgr"]

    # Read the query string BEFORE the handshake. After prepare() the response
    # is a websocket, so a ValueError out of int() can no longer be turned into
    # a status: aiohttp logs a 500 it cannot send and the socket just drops,
    # with no close frame and nothing for the client to report. A bad last_seq
    # should be a 400 the client can read.
    name = request.query.get("session", "terminal")
    want_reasoning = request.query.get("reasoning", "1") != "0"
    try:
        last_seq = int(request.query.get("last_seq", 0) or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(reason="last_seq must be a whole number")
    # A negative would make `if last_seq` true and then match every event, so
    # a client replaying from "-1" would be sent the whole turn buffer twice.
    last_seq = max(last_seq, 0)

    ws = web.WebSocketResponse(heartbeat=20)  # detect half-open peers
    await ws.prepare(request)
    # Tracked so shutdown can close it. Weak, so a socket that goes away on its
    # own is not kept alive here by the bookkeeping meant to tidy it up.
    request.app["websockets"].add(ws)

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
    writer = None
    # Attach, then greet, then start the writer — in that order. Attaching first
    # means no event emitted during the greeting is lost; starting the writer
    # last means none can overtake the greeting and reach the client before it.
    #
    # attach() sits immediately before the try, and the greeting inside it: a
    # client that vanishes during the handshake makes send_json raise, and with
    # that line outside the try the finally never ran — the subscriber stayed
    # attached to a dead socket for the life of the process, taking a copy of
    # every event the session emitted from then on.
    session.attach(sub)
    try:
        await ws.send_json(session.snapshot(last_seq))
        writer = asyncio.create_task(_writer(ws, sub))
        logger.info("client attached to %s (%d total)", name, len(session.subscribers))
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
                if session.busy:
                    await ws.send_json(core.ev("busy", reason="a turn is already running"))
                    continue
                # Who is speaking, when the client knows something the session
                # name does not — the Discord bot sends "discord:<username>" so
                # one channel's session still shows who typed. Clamped because
                # it is client-supplied and rides in every turn_start; falls
                # back to the session name, which is what it always used to be.
                origin = str(data.get("origin") or name)[:64] or name
                # A task, not an await: the reader must stay live to receive
                # `stop` and `rollover_reply` while the turn is running. The
                # session holds the handle, not this handler, so the turn
                # outlives the socket that started it and any client attached
                # to the session can stop it.
                session.start_turn(text, origin=origin)
            elif kind == "command":
                reply = await session.command(data.get("name", ""), data.get("args", ""))
                reply.update({"t": "response", "request_id": data.get("request_id")})
                await ws.send_json(reply)          # to this client only
            elif kind == "rollover_reply":
                ok = session.answer_rollover(data.get("request_id"), data.get("yes"))
                if not ok:
                    logger.debug("stale or duplicate rollover reply ignored")
            elif kind == "stop":
                # Say so when there was nothing to stop. Silence here reads
                # exactly like a stop that was ignored, which is the complaint
                # this whole path existed to cause.
                if not session.stop_turn():
                    await ws.send_json(core.ev(
                        "notice", level="warn",
                        text="nothing to stop — no turn is running in this session"))
            elif kind == "steering":
                # A correction for the turn in flight. When nothing is running,
                # add_steering's own notice reaches every attached client, so this
                # socket needs no reply of its own.
                text = (data.get("text") or "").strip()
                if text:
                    session.add_steering(text)
            elif kind == "ping":
                await ws.send_json(core.ev("pong"))
    finally:
        session.detach(sub)
        sub.wake.set()
        if writer is not None:          # None when the greeting never got out
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
        n = 0
    if n:
        logger.info("restored %d session(s) from disk", n)
        print(f"   restored {n} session(s)")
    # The core's ssh port-forwards: the keepalive that notices a dropped path
    # and brings the forward back. A running loop exists here, which is why
    # this is on_startup rather than SessionManager.__init__.
    app["mgr"].forwards.start(
        app["mgr"]._engine_ports,
        app["config"].get("core", {}).get("ssh_health_sec"))
    for name, st in app["mgr"].forwards.status().items():
        via = st["target"] + (f" via {st['jump']}" if st["jump"] else "")
        print(f"   forward: {name} {st['local']} -> {st['remote']} "
              f"(via {via})")
    if app["mgr"].forwards.status() and shutil.which("ssh") is None:
        print("   WARNING: no 'ssh' binary found — the ssh_forward models "
              "will not work on this machine")
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
    #
    # Closed concurrently rather than one at a time, because the await was the
    # window: `set(...)` copies, so the classic mutation-during-iteration was
    # already avoided, but suspending inside the loop let `ws_handler` add to
    # the same WeakSet between steps — a WebUI reconnecting on its timer, or
    # the bot reconnecting the instant its link drops, both entirely normal
    # during a shutdown. The next step then raised RuntimeError out of
    # on_shutdown and skipped mgr.close() below, so the core exited with every
    # session unsaved: precisely the loss this handler exists to prevent.
    # gather() builds its task list from one synchronous snapshot and never
    # touches the set again.
    closes = [ws.close(code=WSCloseCode.GOING_AWAY, message=b"core shutting down")
              for ws in list(app["websockets"])]
    try:
        # return_exceptions, so one socket that cannot be closed does not take
        # the other closes — or the persist below — down with it.
        await asyncio.gather(*closes, return_exceptions=True)
        for s in app["mgr"].sessions.values():
            for sub in list(s.subscribers):
                s.detach(sub)
    finally:
        # In a finally for the same reason: no amount of socket bookkeeping
        # going wrong is worth losing the conversations.
        await app["mgr"].close()


def build_app(config, config_path=None, bind=None):
    app = web.Application(middlewares=[_host_guard])
    app["config"] = config
    app["config_path"] = config_path
    app["mgr"] = core.SessionManager(config, config_path)
    app["websockets"] = weakref.WeakSet()
    # Resolved once: gethostname/getaddrinfo can block on a slow resolver, and
    # this must not happen per request. `bind` defaults from the config so a
    # caller that builds an app without going through main() — every test that
    # does — gets the same allowlist the real core would have.
    app["allowed_hosts"] = _machine_hostnames(
        bind or config.get("core", {}).get("bind", "127.0.0.1"))
    app.add_routes([
        web.get("/health", health),
        web.get("/models", models),
        web.get("/sessions", sessions),
        web.delete("/sessions/{name}", delete_session),
        web.post("/sessions/{name}/rename", rename_session),
        web.get("/sessions/{name}/history", session_history),
        web.post("/reload", reload_config),
        web.get("/config", config_get),
        web.post("/config", config_save),
        web.get("/theme.css", theme),
        web.get("/files/{name:.*}", files),
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


def _lan_ip():
    """The LAN address of this machine, for telling a neighbour where the WebUI is.

    The UDP trick: "connect" a UDP socket to a public address. No packet is
    sent — the kernel just reports the source address it WOULD use, which is
    the one on the default route. A machine with no route out (a lab core
    with no internet) gets None and the banner falls back to the bind.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _webui_host(bind):
    """The host part of the WebUI banner line.

    A bind on every interface (0.0.0.0 / ::) is not an address you can type
    in, so it is printed as the LAN address instead. A bind on a specific IP
    is already the address, and loopback stays as it is.
    """
    if bind in ("0.0.0.0", "::"):
        return _lan_ip() or bind
    return bind


def main():
    ap = argparse.ArgumentParser(description="letsClaw core service")
    ap.add_argument("--bind"); ap.add_argument("--port", type=int)
    ap.add_argument("--config")
    args = ap.parse_args()
    try:
        # ensure_config, not load_config: a fresh clone has no config.yaml at all
        # (it is gitignored), and the old behaviour was a traceback-free "❌ No
        # config.yaml" and an exit — which left a person with nothing to edit.
        # This writes a starter file with one model whose base_url is empty, so
        # the core boots, the WebUI opens, and the first turn says exactly which
        # line to fill in. A file that exists but is someone's work is never
        # overwritten — see ensure_config for the three cases.
        config, created = configlib.ensure_config(args.config)
    except (FileNotFoundError, Exception) as e:
        print(f"❌ {e}")
        sys.exit(1)
    _configure_logging(config)
    if created:
        print(f"⚠️  wrote a starter config to {args.config or configlib.CONFIG_PATH}"
              " — no model is configured yet, so fill in models.local.base_url")
        print("   (config.example.yaml is the fully commented template; "
              "docs/config.html is the reference)")
    core_cfg = config.get("core", {})
    bind = args.bind or core_cfg.get("bind", "127.0.0.1")
    # Not int_or: silently falling back to 8770 would start a core on a port
    # the person did not ask for and did not know about, and every client
    # reading the same config would then fail to reach it. A bad port is worth
    # stopping for — but with a message naming the key, not a traceback.
    try:
        port = args.port or int(core_cfg.get("port", 8770))
    except (TypeError, ValueError):
        print(f"❌ core.port is {core_cfg.get('port')!r}, which is not a port "
              "number — fix it in config.yaml (the default is 8770)")
        sys.exit(1)

    # An unconfined `exec` on an address other machines can reach, with nothing
    # asked of whoever connects, is not a warning — it is a remote shell for the
    # network. Loopback with no token still works untouched: that is the
    # zero-config local case, and the Origin check is what defends it.
    if bind not in LOOPBACK and not (core_cfg.get("token") or ""):
        print(f"❌ refusing to bind {bind}: core.token is empty, and tools are "
              "unconfined — anyone who can reach this port could run shell "
              "commands here.\n   Set core.token in config.yaml, or bind "
              "127.0.0.1.")
        sys.exit(1)

    app = build_app(config, args.config, bind)
    print(f"🧠 letsClaw core on ws://{bind}:{port}/ws  (proto {core.PROTOCOL_VERSION})")
    if (WEB_DIR / "index.html").exists():
        print(f"   WebUI:  http://{_webui_host(bind)}:{port}/")
    print(f"   models: {', '.join(configlib.known_models(config)) or '(none)'}")
    if bind not in LOOPBACK:
        print("   ⚠️  not bound to loopback — tools are unconfined, so anyone who can "
              "reach this port can run shell commands here")
    # This is a daemon; its stdout is a log file far more often than a tty, and block
    # buffering would hold the banner back until something else filled the buffer.
    sys.stdout.flush()
    web.run_app(app, host=bind, port=port, print=None)


if __name__ == "__main__":
    main()
