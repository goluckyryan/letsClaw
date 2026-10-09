"""ssh_forward.py — SSH local port-forwards owned by the core.

A model entry may name an `ssh_forward` beside its `base_url`:

    qwen38-flash-helios:
      base_url: "http://127.0.0.1:18888/v1"   # the port is IGNORED when
      ssh_forward: "heliosSpark"              # ssh_forward is set — the core
      ssh_remote: "127.0.0.1:8888"            # assigns its own local port

The core spawns `ssh -N -L 127.0.0.1:<port>:<remote> <target>` itself and
points the engine at the local end. The port is picked by the core at startup
(a random free one), so the model is reachable only from this core process:
no fixed port for another local process to find, no separate service to
babysit, and the forward dies with the core.

The forward is a child process, not an in-process socket: ssh already handles
the jump host (`ProxyJump` in ~/.ssh/config), keepalives and rekeying, and a
forward through a multi-hop path is exactly what ssh is for. This module only
adds supervision — a health loop that notices the child died (the ssh path
dropped) and brings it back.
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


def pick_free_port():
    """A local TCP port nothing is listening on, right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SSHForward:
    """One `ssh -N -L` child, supervised.

    The local port is chosen in __init__ (random free one when local_port is
    0), so local_url() is valid before the process has even started.
    """

    def __init__(self, name, target, remote, local_port=0):
        self.name = name
        self.target = target
        self.remote = remote
        self.local_port = int(local_port) or pick_free_port()
        self._proc = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._restarting = False
        # True once ensure() has succeeded: a keepalive failure for a forward
        # that never came up is logged once, not every cycle.
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    def _spawn(self):
        cmd = ["ssh", "-o", "BatchMode=yes",
               "-o", "ExitOnForwardFailure=yes",
               "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6",
               "-N", "-L", f"127.0.0.1:{self.local_port}:{self.remote}",
               self.target]
        logger.info("ssh forward %s: %s -> %s (via %s)", self.name,
                    f"127.0.0.1:{self.local_port}", self.remote, self.target)
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self._proc

    async def _healthy(self) -> bool:
        """True when the local end of the forward accepts TCP.

        A connect (not an HTTP probe) is the right test: it is
        protocol-agnostic, and the port is listening only while the forward is
        actually up — ssh with ExitOnForwardFailure holds the port for the
        whole life of the child.
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
                         self.local_port, self.remote, self.target)
            raise RuntimeError(
                f"ssh forward {self.name} did not come up "
                f"(127.0.0.1:{self.local_port} -> {self.remote} via {self.target})")

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
            "remote": self.remote,
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
        want = {}
        models = config.get("models", {}) or {}
        for name, entry in models.items():
            if not isinstance(entry, dict) or "ssh_forward" not in entry:
                continue
            want[name] = (str(entry.get("ssh_forward")),
                          str(entry.get("ssh_remote") or "127.0.0.1:8888"),
                          int(entry.get("ssh_local_port") or 0))
        return want

    def configure(self, config):
        """Startup: create the forwards the config wants. Synchronous, spawns
        nothing — the ssh processes start lazily (first use) or via the
        keepalive."""
        for name, (target, remote, local) in self._wanted(config).items():
            if name not in self._forwards:
                self._forwards[name] = SSHForward(name, target, remote, local)

    def plan(self, config):
        """Diff the config against the current set, without applying it.

        Returns (added, removed): added are new SSHForward objects (or a
        replacement, when a forward's target or remote changed — the old one
        comes back in removed), removed are forwards the config no longer
        wants or that are being replaced.
        """
        want = self._wanted(config)
        added, removed = {}, {}
        for name, spec in want.items():
            cur = self._forwards.get(name)
            if cur is None:
                added[name] = SSHForward(name, *spec)
            elif (cur.target, cur.remote) != (spec[0], spec[1]):
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

    def unstage(self, added):
        """Undo a staged reload that was refused: drop the additions again."""
        for name in added:
            self._forwards.pop(name, None)

    def commit(self, removed):
        """Drop the planned removals from the set (after a successful reload)."""
        for name in removed:
            self._forwards.pop(name, None)

    def set_zombies(self, zombies):
        self._zombies = dict(zombies)

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
        while True:
            # The first pass is immediate: the core starts its forwards at
            # startup, and a dead path is noticed and retried every cycle
            # after that.
            for fwd in list(self._forwards.values()):
                await fwd.maintain()
            await asyncio.sleep(self._interval)
            # A zombie is retired the moment no engine points at its port any
            # more — the last session on that model has swapped or the engine
            # was closed, so the ssh process can go with it.
            if self._referenced_ports is not None:
                try:
                    ports = set(self._referenced_ports())
                except Exception:
                    ports = set()
                for name in list(self._zombies):
                    if self._zombies[name].status()["local"] not in ports:
                        logger.info("ssh forward %s: last engine on its port "
                                    "is gone — closing", name)
                        await self._zombies.pop(name).close()

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
