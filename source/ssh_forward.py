"""ssh_forward.py — SSH local port-forwards owned by the core.

A model entry may name an `ssh_forward` beside its `base_url`:

    qwen38-flash-helios:
      base_url: "http://127.0.0.1:18888/v1"   # the port is IGNORED when
      ssh_forward: "heliosspark@192.168.1.101"  # ssh_forward is set — the core
      ssh_jump: "helios@192.168.203.52"         # assigns its own local port
      ssh_remote_port: 8888                     # the port on the destination

The entry is self-contained: `ssh_forward` is the destination as ssh takes it
(user@ip, bare ip, or — if the machine has an alias — an alias), and
`ssh_jump` is an optional jump host (user@ip or ip). `ssh_remote_port` is
the port the service listens on at the destination — always its own loopback,
so the port alone is all there is to name. Nothing is read from ~/.ssh/config,
so the model works on a fresh machine that has only the ssh keys. `ssh_jump` is spelled out as a ProxyCommand rather than -J because a
-J drops command-line options on the inner hop — and some jump hosts run old
sshd that only offers ssh-rsa, which the inner hop must be told to accept.

The core spawns `ssh -N -L 127.0.0.1:<port>:127.0.0.1:<remote_port> <target>` itself and
points the engine at the local end. The port is picked by the core at startup
(a random free one), so the model is reachable only from this core process:
no fixed port for another local process to find, no separate service to
babysit, and the forward dies with the core.

The forward is a child process, not an in-process socket: ssh handles
keepalives and rekeying, and a forward through a multi-hop path is exactly
what ssh is for. This module only adds supervision — a health loop that
notices the child died (the ssh path dropped) and brings it back.
"""

import asyncio
import logging
import socket
import subprocess
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("letclaw.ssh")

# Seconds between keepalive health checks of a live forward.
HEALTH_INTERVAL = 30
# How long ensure() will wait for a freshly-spawned forward to answer.
STARTUP_TRIES = 15
STARTUP_DELAY = 1.0
# How long the "is this port really our far side?" question may take. It runs
# once, before the first spawn, against whatever already holds the port — which
# may be something that never answers at all, so it has to be bounded.
PROBE_TIMEOUT = 3


def pick_free_port():
    """A local TCP port nothing is listening on, right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SSHForward:
    """One `ssh -N -L` child, supervised.

    The local port is always the core's own: a free one picked in __init__, so
    local_url() is valid before the process has even started. There is no
    config key for it — see _wanted() for why — and `local_port` here is for
    the tests, which need a port they already hold.
    """

    def __init__(self, name, target, remote_port, jump=None, model_id=None,
                 local_port=0):
        self.name = name
        self.target = target
        # The port the service listens on at the destination, on its own
        # loopback. The forward is 127.0.0.1:<local> -> 127.0.0.1:<this>.
        self.remote_port = int(remote_port)
        # Optional jump host, "user@ip" or "ip". None = direct connection.
        self.jump = jump
        # The model id the far side reports in /v1/models (models.<name>.
        # provider_name, or the entry's own name). It is what makes "is this
        # port ours?" answerable — see _far_side_is_ours().
        self.model_id = model_id
        self.local_port = int(local_port) or pick_free_port()
        self._proc = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._restarting = False
        # True once ensure() has succeeded: a keepalive failure for a forward
        # that never came up is logged once, not every cycle.
        self._started = False
        # pid of the exited child _healthy() has already complained about, so
        # ensure()'s fifteen retries do not print the same line fifteen times.
        self._warned_dead = None

    # -- lifecycle -----------------------------------------------------------

    def _spawn(self):
        cmd = ["ssh", "-o", "BatchMode=yes",
               "-o", "ExitOnForwardFailure=yes",
               "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6"]
        if self.jump:
            # A ProxyCommand, not -J: -J builds the inner ssh from the config
            # only, so command-line options never reach the jump hop. Some jump
            # hosts run old sshd that offers only ssh-rsa, which the inner hop
            # must be told to accept — hence the +ssh-rsa in the command itself.
            # (Harmless on a modern jump host: it only widens the allow-list.)
            # "user@ip" or bare "ip" — only a real @ splits them; partition()
            # on a bare ip would hand the whole string back as the user.
            if "@" in self.jump:
                juser, _, jhost = self.jump.partition("@")
            else:
                juser, jhost = "", self.jump
            jcmd = ["ssh", "-o", "BatchMode=yes",
                    "-o", "HostKeyAlgorithms=+ssh-rsa",
                    "-o", "PubKeyAcceptedAlgorithms=+ssh-rsa"]
            if juser:
                jcmd += ["-l", juser]
            jcmd += ["-W", "%h:%p", jhost]
            cmd += ["-o", "ProxyCommand=" + " ".join(jcmd)]
        remote = f"127.0.0.1:{self.remote_port}"
        cmd += ["-N", "-L", f"127.0.0.1:{self.local_port}:{remote}",
                self.target]
        via = f" (via {self.jump})" if self.jump else ""
        logger.info("ssh forward %s: %s -> %s (via %s)%s", self.name,
                    f"127.0.0.1:{self.local_port}", remote,
                    self.target, via)
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self._proc

    async def _accepts(self) -> bool:
        """True when something accepts TCP on the local end of the forward.

        A connect, not an HTTP probe: it is protocol-agnostic, and the port is
        listening only while the forward is actually up — ssh with
        ExitOnForwardFailure holds the port for the whole life of the child.
        """
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.local_port), timeout=3)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    async def _far_side_is_ours(self) -> bool:
        """True when whoever holds the local port answers as our model server.

        One hand-written HTTP/1.1 request rather than aiohttp: this module has
        no other HTTP dependency, and the question is a small one — GET
        /v1/models, is our model id in the answer. Anything else (a server
        that is not HTTP, one that never replies, one serving a different
        model) is not ours.

        The path is the OpenAI-compatible one, which is what every server this
        forwards to speaks. A server mounted under a prefix would fail the
        probe and cost us the reuse, not the forward: we take a fresh port and
        spawn, which is what would have happened with no port to reuse at all.
        """
        if not self.model_id:
            # Nothing to compare against, so nothing can be verified, so the
            # port is not ours. Unverifiable is the same as wrong here: the
            # cost of being wrong is our API key going to a stranger.
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.local_port),
                timeout=PROBE_TIMEOUT)
        except Exception:
            return False
        try:
            writer.write(("GET /v1/models HTTP/1.1\r\n"
                          f"Host: 127.0.0.1:{self.local_port}\r\n"
                          "Accept: application/json\r\n"
                          "Connection: close\r\n\r\n").encode())
            await writer.drain()
            # To EOF, which Connection: close promises us; bounded by the
            # timeout for whoever does not keep that promise.
            body = await asyncio.wait_for(reader.read(), timeout=PROBE_TIMEOUT)
        except Exception:
            return False
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        return self.model_id.encode() in body

    async def _healthy(self) -> bool:
        """True when the local end of the forward accepts TCP *and is ours*.

        A connect alone only proves *something* is listening. Once our own ssh
        has exited, the port is free for anything on this machine to bind, and
        the next thing to bind it inherits every request we then send down it —
        requests that carry the model's API key in a header. So once we have a
        child, it has to still be alive: a dead child plus an open port is not
        a healthy forward, it is a stranger on our port. ensure() then
        respawns, the new ssh cannot bind, and the forward reports itself down
        rather than handing out an engine pointed at whoever is there.

        Before the first spawn (_proc is None) an open port is still *allowed*
        to be reused — a forward set up by hand, or left behind by a previous
        run of the core, should be picked up rather than fought over — but the
        reuse is now verified rather than assumed. The connect proved something
        is there; _far_side_is_ours() asks it the one question only our far
        side answers. A stranger is not killed, because it is not ours to kill
        and it may be the very thing someone is working in: we take a different
        local port and say so, and the spawn below puts our own ssh on it.
        """
        if self._proc is not None and self._proc.poll() is not None:
            if self._warned_dead != self._proc.pid:
                self._warned_dead = self._proc.pid
                logger.warning("ssh forward %s: our ssh exited (status %s); "
                               "not trusting 127.0.0.1:%s until we own it again",
                               self.name, self._proc.poll(), self.local_port)
            return False
        if not await self._accepts():
            return False
        if self._proc is None and not await self._far_side_is_ours():
            taken = self.local_port
            self.local_port = pick_free_port()
            logger.warning("ssh forward %s: 127.0.0.1:%s is held by something "
                           "that does not answer for %r — leaving it alone and "
                           "taking 127.0.0.1:%s instead", self.name, taken,
                           self.model_id, self.local_port)
            return False
        return True

    async def ensure(self):
        """Bring the forward up and wait until it answers. Idempotent: a
        healthy forward is a cheap no-op (one TCP connect).

        Called from two places — the keepalive (which starts every configured
        forward at core startup and revives one whose path dropped) and the
        engine-build path (so the first engine for a model is never handed out
        before its forward is up). They serialize on the lock.

        Raises RuntimeError when the forward does not answer in time — the
        caller then keeps the old engine (if any) rather than handing out one
        that cannot reach its server.
        """
        async with self._lock:
            if self._closed:
                return
            if await self._healthy():
                self._started = True
                return
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
            for _ in range(STARTUP_TRIES):
                if await self._healthy():
                    self._started = True
                    return
                await asyncio.sleep(STARTUP_DELAY)
            logger.error("ssh forward %s did not come up "
                         "(127.0.0.1:%s -> %s via %s)", self.name,
                         self.local_port, f"127.0.0.1:{self.remote_port}",
                         self.target)
            raise RuntimeError(
                f"ssh forward {self.name} did not come up "
                f"(127.0.0.1:{self.local_port} -> 127.0.0.1:{self.remote_port} via {self.target}) — "
                "check the ssh binary is installed, the key for the target is "
                "authorized there, and the path (jump host, network) is reachable")

    async def maintain(self):
        """Keepalive pass: make sure the forward is up.

        The first pass is the eager start at core startup; later passes revive
        a forward whose ssh path dropped. ensure() logs a failure itself, and
        this swallows it so a persistently-down path does not spam every cycle
        — the engine-build path is the one that reports it to whoever needs
        the model.
        """
        try:
            await self.ensure()
        except Exception:
            pass

    async def close(self):
        async with self._lock:
            # Set under the lock so an ensure() that is queued behind it — or
            # an in-flight keepalive pass that captured this forward before a
            # refused reload unstaged it — cannot spawn a child nothing will
            # ever close again.
            self._closed = True
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    # And reap it. A killed child that is never waited on stays
                    # a zombie until this process exits, which used to be the
                    # end of it — but /restart re-execs in place, and the new
                    # image inherits the PID with no subprocess bookkeeping to
                    # collect anything, so the zombie would outlive every
                    # restart and one more would join it each time. SIGKILL is
                    # not refusable, so this returns almost at once; bounded
                    # anyway, because a process wedged in uninterruptible I/O
                    # must not hold a shutdown open.
                    try:
                        await asyncio.wait_for(self._wait(), timeout=2)
                    except asyncio.TimeoutError:
                        logger.warning("ssh forward %s: did not die after "
                                       "SIGKILL", self.name)
            self._proc = None

    async def _wait(self):
        while self._proc.poll() is None:
            await asyncio.sleep(0.1)

    # -- reporting -----------------------------------------------------------

    def local_url(self, base_url: str) -> str:
        """base_url with its port swapped for the forward's local port.

        The config's base_url supplies the scheme, host and the /v1 suffix;
        only the port is replaced, and only when ssh_forward is in play.
        """
        parts = urlsplit(base_url)
        return urlunsplit(parts._replace(netloc=f"127.0.0.1:{self.local_port}"))

    def status(self) -> dict:
        return {
            "target": self.target,
            "jump": self.jump,
            "remote": f"127.0.0.1:{self.remote_port}",
            "local": f"127.0.0.1:{self.local_port}",
            "pid": self._proc.pid if self._proc and self._proc.poll() is None else None,
        }


class SSHForwardManager:
    """All of the core's forwards, plus the keepalive that watches them.

    Reloads go through plan/stage/commit so a rejected reload never leaves the
    forward set half-changed: plan() computes the diff, stage() makes the
    additions visible to engine building, commit() drops the removals — only
    after the new engines have validated.
    """

    def __init__(self):
        self._forwards = {}
        # Forwards a reload removed, but a live engine still points at: the
        # ssh process stays up for that engine, and the keepalive closes it
        # when the last engine on its port goes.
        self._zombies = {}
        self._task = None
        self._referenced_ports = None

    @staticmethod
    def _wanted(config):
        """{model name: the spec a forward is built from}, from the config.

        The spec is exactly SSHForward's own arguments after the name, and
        plan() compares all of it — see there. There is deliberately no key
        for the local port: it is an implementation detail of a tunnel this
        process owns and nothing outside the core can usefully name it. The
        key `ssh_local_port` used to be read into the spec and then left out
        of plan()'s comparison, so editing it reported success and changed
        nothing until a restart. It is gone rather than honoured, and a config
        that still carries it is told so rather than having it quietly dropped.
        """
        want = {}
        models = config.get("models", {}) or {}
        for name, entry in models.items():
            if not isinstance(entry, dict) or "ssh_forward" not in entry:
                continue
            if "ssh_local_port" in entry:
                logger.warning("models.%s.ssh_local_port is ignored and no "
                               "longer exists — the core picks a free local "
                               "port for every forward; remove the key", name)
            jump = str(entry.get("ssh_jump") or "").strip() or None
            # ssh_remote_port is the port on the destination. The legacy
            # ssh_remote "host:port" spelling is still read (host ignored).
            rp = entry.get("ssh_remote_port")
            if rp is None:
                rp = str(entry.get("ssh_remote") or "").rsplit(":", 1)[-1]
            want[name] = (str(entry.get("ssh_forward")),
                          int(rp or 8888),
                          jump,
                          str(entry.get("provider_name") or name))
        return want

    def configure(self, config):
        """Startup: create the forwards the config wants. Synchronous, spawns
        nothing — the ssh processes start lazily (first use) or via the
        keepalive."""
        for name, spec in self._wanted(config).items():
            if name not in self._forwards:
                self._forwards[name] = SSHForward(name, *spec)

    def plan(self, config):
        """Diff the config against the current set, without applying it.

        Returns (added, removed): added are new SSHForward objects (or a
        replacement, when something in a forward's spec changed — the old one
        comes back in removed), removed are forwards the config no longer
        wants or that are being replaced.

        The comparison is the whole spec against the whole forward, with
        nothing left out. It used to carry a fourth element, the local port
        from `ssh_local_port`, that was built into the forward and then not
        compared here — so that one key could be edited, reloaded, reported
        applied, and have changed nothing. A spec whose elements are not all
        compared is a key that silently does not work; the key is gone now,
        and this stays exhaustive so the next one added cannot repeat it.
        """
        want = self._wanted(config)
        added, removed = {}, {}
        for name, spec in want.items():
            cur = self._forwards.get(name)
            if cur is None:
                added[name] = SSHForward(name, *spec)
            elif (cur.target, cur.remote_port, cur.jump, cur.model_id) != spec:
                removed[name] = cur
                added[name] = SSHForward(name, *spec)
        for name, fwd in self._forwards.items():
            if name not in want and name not in removed:
                removed[name] = fwd
        return added, removed

    def stage(self, added):
        """Make planned additions visible to forward_for() before the config
        swap is committed. The caller closes them if the reload is rejected."""
        self._forwards.update(added)

    def unstage(self, added, removed=None):
        """Undo a staged reload that was refused: drop the additions again.

        By identity, and putting back what stage() displaced. Popping by name
        deleted the *old* forward on a replacement reload — plan() reports one
        name on both sides of a change (the running forward in `removed`, its
        replacement in `added`), so stage() had overwritten the entry and this
        dropped it outright. The refused reload then left the core with no
        forward at all: the ssh child it was still serving kept running with
        nobody able to close it, and the next engine built for that model went
        to the raw base_url — straight at an address that is unreachable
        without the tunnel, which is the whole thing ssh_forward exists for.

        `removed` is the same plan's removals, so a replacement can be told
        from a plain addition. It is optional: with nothing to restore, an
        addition is simply withdrawn, which is what a pure addition is.
        """
        removed = removed or {}
        for name, fwd in added.items():
            if self._forwards.get(name) is not fwd:
                continue        # not ours any more; whoever put it there owns it
            prev = removed.get(name)
            if prev is not None and prev is not fwd:
                self._forwards[name] = prev      # put back the one stage() covered
            else:
                self._forwards.pop(name, None)

    def commit(self, removed):
        """Drop the planned removals from the set (after a successful reload).

        By identity, for the same reason as unstage(): on a replacement the
        name now maps to the *new* forward, and popping it by name would delete
        the forward this reload just built its engines on. The old one has
        already been displaced from the dict by stage(), so there is nothing
        here to drop — the caller closes it or parks it as a zombie.
        """
        for name, fwd in removed.items():
            if self._forwards.get(name) is fwd:
                del self._forwards[name]

    def set_zombies(self, zombies):
        """Track these as zombies, keeping any that were already pending.

        Merge, not replace. A zombie is a removed forward whose port a live
        engine still points at; the keepalive closes it once nothing points
        there any more. Replacing the dict dropped every zombie this reload
        did not name again — and nothing else holds a reference, so that ssh
        child was never closed and never would be, for the life of the core.

        Returns the forwards this call displaced (same name, a different
        object) so the caller, which is in async code, can close them.
        """
        displaced = [self._zombies[n] for n, f in zombies.items()
                     if n in self._zombies and self._zombies[n] is not f]
        self._zombies.update(zombies)
        return displaced

    def forward(self, model_name):
        return self._forwards.get(model_name)

    async def forward_for(self, model_name):
        """The model's forward, guaranteed up. None when the model has no
        forward (the normal case)."""
        fwd = self._forwards.get(model_name)
        if fwd is None:
            return None
        await fwd.ensure()
        return fwd

    def status(self):
        return {n: f.status() for n, f in self._forwards.items()}

    # -- keepalive -----------------------------------------------------------

    def start(self, referenced_ports=None, interval=None):
        """Start the health loop. Must be called from a running loop (the
        server's on_startup). referenced_ports() -> iterable of the local
        ports live engines point at: it is what lets the loop retire zombies.
        interval (core.ssh_health_sec) overrides HEALTH_INTERVAL."""
        self._referenced_ports = referenced_ports
        self._interval = float(interval or HEALTH_INTERVAL)
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self):
        # Everything below the sleep is wrapped, and the loop never breaks on
        # anything but a cancel. This task is the *only* thing that revives a
        # dropped forward, and nobody ever retrieves its exception: one raise
        # here — a zombie whose close() times out oddly, a referenced_ports
        # callback into a half-torn-down manager — used to end forward
        # recovery for the life of the process, silently. Every model behind
        # ssh would then stay down until the core was restarted, with the
        # traceback appearing only at interpreter exit, if at all.
        while True:
            try:
                # The first pass is immediate: the core starts its forwards at
                # startup, and a dead path is noticed and retried every cycle
                # after that.
                for fwd in list(self._forwards.values()):
                    await fwd.maintain()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ssh keepalive: maintenance pass failed")
            await asyncio.sleep(self._interval)
            try:
                # A zombie is retired the moment no engine points at its port
                # any more — the last session on that model has swapped or the
                # engine was closed, so the ssh process can go with it.
                if self._referenced_ports is not None:
                    try:
                        ports = set(self._referenced_ports())
                    except Exception:
                        ports = set()
                    for name in list(self._zombies):
                        if self._zombies[name].status()["local"] not in ports:
                            logger.info("ssh forward %s: last engine on its "
                                        "port is gone — closing", name)
                            # Popped before the await, not after: close() can
                            # take five seconds, and a second pass starting in
                            # that window would close the same forward twice.
                            await self._zombies.pop(name).close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ssh keepalive: retiring zombies failed")

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for fwd in list(self._forwards.values()) + list(self._zombies.values()):
            await fwd.close()
        self._forwards = {}
        self._zombies = {}
