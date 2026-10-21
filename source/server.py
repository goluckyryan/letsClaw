#!/usr/bin/env python3
"""server.py — the letsClaw core service.

Start this first; the terminal, a WebUI and a Discord bot are all just clients:

    ./serve.sh            (or: python source/server.py)
    ./terminalUI.sh       (terminal client, in another shell)

HTTP:  GET /health  GET /models  GET /sessions  DELETE /sessions/<name>
       GET /sessions/<name>/history  POST /sessions/<name>/rename
       GET /files  GET /files/<name>  DELETE /files/<name>
WS:    /ws?session=<name>&last_seq=<n>

The reader never runs a turn inline — turns are tasks, so the socket stays
readable for `stop` and for the rollover answer while a turn is in flight.
"""

import argparse
import asyncio
import copy
import logging
import os
import secrets
import shutil
import signal
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

# Re-exported rather than defined here: the show_image tool needs the same
# extension map and the same root, and tools.py cannot import this module
# (server -> core -> tools). See paths.py for the reasoning.
from paths import DEFAULT_FILES_DIR, IMAGE_TYPES, files_root  # noqa: E402,F401

# Newest first and capped, for the same reason /sessions/<name>/history is
# capped: the Media panel wants the plot the agent just wrote, not a slow JSON
# of a directory somebody pointed at their photo library. The browser builds one
# full-size <img> per entry, so this is a real backstop and not a formality.
MEDIA_LIST_MAX = 500


def _configure_logging(config):
    """config.yaml's logging: section was never wired up; a daemon needs it."""
    logging_setup.configure(config, "core")
    # Third-party chatter at the configured level would drown the app's own
    # lines: the WebUI polls /sessions every 5 s (aiohttp.access), and every
    # model call is logged by httpx. Both go to WARNING.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# Addresses that are not reachable from another machine. Used by the startup
# check, by the banner, and by configlib.boot_blockers — which is what /restart
# asks whether the core that comes back would survive its own startup check. It
# lives in config.py so those three cannot disagree about what "loopback" means.
LOOPBACK = configlib.LOOPBACK

class _RestartState:
    """Where a restart stands, for the four places that have to agree.

    `armed`     /restart was accepted and the timer is set.
    `exec`      the timer actually fired — the one thing main() consults.
    `stopping`  a signal, not the timer, started the shutdown.
    `timer`     the pending call_later, so a shutdown can disarm it.

    `armed` and `exec` are deliberately not one flag: see _arm_restart.
    """
    __slots__ = ("armed", "exec", "stopping", "timer")

    def __init__(self):
        self.armed = False
        self.exec = False
        self.stopping = False
        self.timer = None


# Between arming a restart and leaving. Short, because the only thing waiting on
# it is the broadcast notice working its way out through the per-socket writers,
# and _on_shutdown drains those properly before it closes anything.
RESTART_GRACE = 0.1
# A ceiling on that drain. A client that cannot take its last few events must
# not hold the restart open.
DRAIN_GRACE = 2.0


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
    # `restarting` is true only inside the grace window, which is a tenth of a
    # second — but a poller that catches it there would otherwise see a healthy
    # core and then a refused connection, with nothing to connect the two.
    return web.json_response({"ok": True, "proto": core.PROTOCOL_VERSION,
                              "sessions": len(mgr.sessions),
                              "restarting": bool(mgr.restarting)})


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


def _files_target(request, name):
    """The one file `name` may mean under files_root, or 404.

    Both guards in one place, because three routes now ask the same question and
    they must not be able to disagree: a listing that shows a file the read route
    would refuse is a tile that is broken forever, and a delete that resolves a
    path the read route would not is a hole. Returns (path, content type).
    """
    root = files_root(request.app["config"])
    try:
        target = (root / name).resolve()
    except (OSError, ValueError):
        raise web.HTTPNotFound()
    # is_relative_to: the resolved path must still be inside the root. This
    # defeats both ../ and a symlink pointing out of it.
    if not target.is_relative_to(root) or not target.is_file():
        raise web.HTTPNotFound()
    ctype = IMAGE_TYPES.get(target.suffix.lower())
    if not ctype:
        raise web.HTTPNotFound()
    return target, ctype


def files_delete_allowed(config):
    """Whether the WebUI may unlink from files_dir. Default: yes.

    Off by default would protect nothing — the same core.token already reaches
    an unconfined `exec` and `write_file`, so anyone who can press the button
    can already remove the directory. The key exists for the other case: a
    files_dir pointed at a photo library or a shared mount, where the pictures
    are not disposable and a stray click is not recoverable.
    """
    return bool((config.get("webui", {}) or {}).get("files_delete", True))


def _scan_files(root):
    """Blocking: one scandir plus a stat per entry. Called via to_thread."""
    out = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        # No directory yet is an empty gallery, not an error. The agent creates
        # it the first time show_image runs.
        return out
    for e in entries:
        ctype = IMAGE_TYPES.get(Path(e.name).suffix.lower())
        if not ctype:
            continue
        try:
            # resolve(), not e.is_file(): scandir follows a symlink happily, and
            # a link in the root pointing at /etc/ssl/cert.png has to vanish
            # from the listing exactly as /files/<name> refuses to serve it.
            if not Path(e.path).resolve().is_relative_to(root):
                continue
            st = e.stat()
        except OSError:
            continue
        if not os.path.isfile(e.path):
            continue
        out.append({"name": e.name, "size": st.st_size,
                    "mtime": int(st.st_mtime), "type": ctype})
    out.sort(key=lambda f: (-f["mtime"], f["name"]))
    return out


async def files_list(request):
    """What is in webui.files_dir, for the WebUI's Media panel.

    Read-only, so _authorised alone — the same gate /sessions and /config take,
    and for the same reason: no CORS headers go out, so a hostile page can fire
    this GET and cannot read what comes back (see _guard).

    Flat, deliberately. /files/<name> will serve state/files/sub/deep.png,
    because confinement is all that route needs, but the renderer's IMG_OK
    accepts no slashes — so no answer can ever reference a nested file. Listing
    them would fill the panel with tiles the conversation cannot produce, and
    would let a directory the agent created decide the size of this response.

    Off the event loop: a cold directory of a few hundred entries is a stat per
    file, and the core is shared.
    """
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    root = files_root(request.app["config"])
    found = await asyncio.to_thread(_scan_files, root)
    return web.json_response({
        "ok": True,
        "dir": str(root),
        "total": len(found),
        "truncated": len(found) > MEDIA_LIST_MAX,
        "can_delete": files_delete_allowed(request.app["config"]),
        "files": found[:MEDIA_LIST_MAX],
    }, headers={"Cache-Control": "no-cache"})


async def files_delete(request):
    """Remove one image from webui.files_dir.

    _guard, not _authorised: this is a write, so it takes both checks the way
    delete_session and config_save do. The token may still arrive by query —
    _authorised accepts either — but the gate that matters for a browser is
    _same_origin, and a fetch() can send the header, which is why the WebUI
    does. The query form exists for <img>, which cannot; nothing else needs it.
    """
    _guard(request)
    if not files_delete_allowed(request.app["config"]):
        # 403, not 404: the file is there and the operator turned the feature
        # off. A 404 would send the WebUI looking for a bug that is a setting.
        raise web.HTTPForbidden(reason="webui.files_delete is off")
    target, _ = _files_target(request, request.match_info["name"])
    try:
        await asyncio.to_thread(target.unlink)
    except OSError as e:
        # A read-only mount or a permission problem has to say so: silence here
        # is a tile that refuses to disappear and no reason why.
        logger.warning("could not delete %s: %s", target, e)
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    logger.info("deleted %s at the WebUI's request", target)
    return web.json_response({"ok": True, "name": target.name})


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
    own answer cannot make either of those pass for /etc/shadow. Both live in
    _files_target now, so the listing and the delete cannot drift from this.
    """
    if not _authorised(request):
        raise web.HTTPUnauthorized()
    target, ctype = _files_target(request, request.match_info["name"])
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
                if mgr.restarting:
                    # The busy check inside request_restart is a snapshot. Without
                    # this gate a turn can start inside the grace window and be
                    # cancelled a tenth of a second later by the very restart that
                    # just reported nothing running. Reported as `busy` because
                    # that is the one "not now" every client already renders.
                    await ws.send_json(core.ev(
                        "busy", reason="the core is restarting — try again in a moment"))
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
    _install_stop_guard(app)
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


def _arm_restart(app):
    """Start the core on its way out, so that main() can exec it back in.

    Installed as SessionManager.restart_hook. Called from inside the /restart
    command handler, which still has a reply to send, so nothing may happen
    synchronously here: a timer fires the actual exit a moment later and this
    returns at once.

    Deliberately NOT a signal. aiohttp installs one handler for SIGINT and
    SIGTERM alike, and serve.sh's trap sends SIGINT too, so a self-signal makes
    /restart indistinguishable from `systemctl stop` and from Ctrl-C — and a
    stop arriving inside the grace window would re-exec a core that was meant
    to stay down. Raising GracefulExit straight out of a loop callback keeps
    the two paths separate: it is a SystemExit subclass, and Handle._run
    re-raises those instead of handing them to the exception handler.

    `st.exec` is set *inside* the callback rather than here, and that is the
    whole of the fix: an external signal during the grace raises its own
    GracefulExit first, the timer never runs, the flag is still False, and
    main() exits for good.
    """
    st = app["restart_state"]
    if st.armed:
        return
    st.armed = True

    def fire():
        # Someone asked us to stop for real while the grace was running. They
        # win: a stop has to mean stopped, and coming back up would be the one
        # outcome nobody can correct from the keyboard they just used. See
        # _install_stop_guard for how that is known.
        if st.stopping:
            logger.warning("restart abandoned: the core was asked to stop")
            return
        st.exec = True
        raise web.GracefulExit()

    # Kept so _on_shutdown can cancel it. A timer left armed through someone
    # else's shutdown raises GracefulExit out of aiohttp's own cleanup, which
    # is not expecting it.
    st.timer = asyncio.get_running_loop().call_later(RESTART_GRACE, fire)


def _install_stop_guard(app):
    """Replace aiohttp's SIGINT/SIGTERM handlers with ones that leave a note.

    aiohttp installs `_raise_graceful_exit` for both signals, and serve.sh's own
    trap sends SIGINT, so from inside the process `systemctl stop`, Ctrl-C and
    the restart timer all arrive as the same bare GracefulExit. /restart has to
    tell them apart, because the consequence of guessing wrong is a core that
    comes back from a stop — and nobody can correct that from the keyboard they
    just pressed Ctrl-C on.

    Cancelling the timer in _on_shutdown covers most of it, but not a
    cancellation that takes longer than the grace to reach _on_shutdown. This
    does, by recording the cause at the only place it is known.

    Installed from on_startup, which BaseRunner.setup() runs *after* it adds
    aiohttp's handlers — so these replace them rather than the other way round.
    If that order ever changes, the note is never left and the timer cancel is
    what is left; hence both.
    """
    st = app["restart_state"]
    loop = asyncio.get_running_loop()

    def stop():
        st.stopping = True
        raise web.GracefulExit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError):
            logger.debug("no signal handler for %s", sig, exc_info=True)


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
    #
    # A restart says so in the close frame. 1012 is the difference between a
    # client that tells someone the core is coming back and one that reports a
    # connection it cannot explain — the WebUI keys a notice off it and the
    # terminal keys its parting line off it, neither of which can be inferred
    # from 1001.
    # Before anything that awaits: this handler can run while the grace timer
    # is still pending — an external stop during the window is exactly that
    # case — and a timer that fires from in here raises GracefulExit out of
    # aiohttp's cleanup. Cancelling an already-fired handle is a no-op, so the
    # genuine restart path is unaffected.
    st = app["restart_state"]
    if st.timer is not None:
        st.timer.cancel()

    # st.exec, not st.armed: the first is armed, the second is decided. A stop
    # that landed inside the grace window leaves the core armed and not coming
    # back, and telling those clients "restarting" would be a promise nothing
    # is going to keep.
    restarting = st.exec
    if restarting:
        code, why = WSCloseCode.SERVICE_RESTART, b"core restarting"
    else:
        code, why = WSCloseCode.GOING_AWAY, b"core shutting down"

    if restarting:
        # The restart's own broadcast notice is sitting in the per-socket
        # queues, and _writer is the only thing that moves it onto the wire.
        # Closing now would throw away the one message explaining why the
        # socket is closing. The unicast reply to whoever typed /restart needs
        # none of this — ws_handler sends that inline — so this loop is purely
        # for the broadcast, and it is a postcondition (the queues are empty)
        # rather than a guess at how long a flush takes. Bounded, because a
        # client that cannot take its last few events must not hold the
        # restart open.
        deadline = asyncio.get_running_loop().time() + DRAIN_GRACE
        while asyncio.get_running_loop().time() < deadline:
            if not any(sub.queue for s in app["mgr"].sessions.values()
                       for sub in list(s.subscribers)):
                break
            await asyncio.sleep(0.01)
        else:
            logger.warning("restart: a client did not drain in %.1fs; "
                           "closing anyway", DRAIN_GRACE)

    closes = [ws.close(code=code, message=why)
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
    # Two flags, not one. "restarting" is armed the moment /restart is accepted
    # and is what the close code and the submit gate read; "restart" is set only
    # when the timer actually fires, and is the single thing main() consults
    # before exec'ing. See _arm_restart for why they cannot be the same flag.
    # A mutable holder rather than four app keys: aiohttp freezes the app at
    # startup and writing app[...] after that is deprecated, and three of these
    # four are only ever written while it is running.
    app["restart_state"] = _RestartState()
    app["mgr"].restart_hook = lambda: _arm_restart(app)
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
        # Adjacent on purpose: registered apart, aiohttp builds a second
        # resource for the same pattern rather than adding the method to the
        # first. It resolves either way, but one resource is what a reader of
        # this list assumes. /files is a PlainResource and cannot collide.
        web.get("/files", files_list),
        web.get("/files/{name:.*}", files),
        web.delete("/files/{name:.*}", files_delete),
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
        print("   (config.example.yaml is the fully commented reference — "
              "every key, its default, and what needs a restart)")
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
    try:
        web.run_app(app, host=bind, port=port, print=None)
    finally:
        # In a finally, not after: run_app suppresses only CancelledError out of
        # runner.cleanup(), so anything else raised on the way down — out of
        # mgr.close(), say — would otherwise skip the re-exec and leave a core
        # that was asked to restart simply gone.
        if app["restart_state"].exec:
            _exec_self()


def _exec_self():
    """Replace this process with a fresh core. Does not return.

    Same PID, which is the point: the core is a background child of serve.sh,
    which ends in `wait "$core_pid"`, under a systemd unit whose ExecStart is
    serve.sh itself. execv keeps the PID, so the wait keeps waiting and systemd
    never sees a service that stopped.
    """
    logger.warning("re-exec: the core is restarting now")
    # execv does not flush anything. The log handlers hold buffered writes and
    # file descriptors that are FD_CLOEXEC, so without this the last lines
    # before a restart are lost — and stdout is a pipe under serve.sh, which
    # means block buffering and the whole banner going with them.
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    # A dead ssh forward killed on the way out is still a child of this PID, and
    # the new image has no subprocess bookkeeping to reap it with — so it would
    # stay a zombie for as long as the core runs, one more per restart.
    # ssh_forward now waits for the ones it kills; this is the backstop for any
    # it could not, and it never blocks.
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass                                      # nothing left to reap
    except OSError:
        logger.debug("reap before re-exec", exc_info=True)
    # orig_argv, so that a core started with -X or -W comes back with it. It is
    # the whole command line — [executable, *interpreter options, script,
    # *script args] — and sys.argv is the tail of it from the script on, so the
    # options are what the two differ by. Slicing orig_argv[1:] instead would
    # have put the script path in twice.
    #
    # argv[0] is resolved because it is relative today ("source/server.py", from
    # serve.sh) and that only works as long as nothing ever chdir'd.
    opts = []
    if len(sys.orig_argv) > len(sys.argv):
        opts = sys.orig_argv[1:len(sys.orig_argv) - len(sys.argv)]
    argv = [sys.executable, *opts,
            str(Path(sys.argv[0]).resolve()), *sys.argv[1:]]
    try:
        os.execv(sys.executable, argv)
    except OSError as e:
        # Non-zero, always. Restart=on-failure brings the core back from a
        # failure; an exit 0 here reads as a deliberate stop and leaves it down
        # — the worst possible outcome for a command whose entire promise is
        # that the core comes back.
        print(f"❌ could not re-exec the core: {e}", file=sys.stderr)
        sys.stderr.flush()
        sys.exit(70)


if __name__ == "__main__":
    main()
