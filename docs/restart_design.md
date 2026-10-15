# `/restart` — a design check, not an implementation

Nothing here is in the tree, and nothing here is scheduled. This is the record of a
design question asked on 2026-10-13 and re-checked on 2026-10-15, kept so that
whoever picks it up starts from what was already established rather than
rediscovering it. `systemctl --user restart letsClaw.service` covers the need in
the meantime, which is most of why this has stayed unwritten.

The design is **still sound**. Three facts it rests on were re-verified against the
current tree:

- `WSCloseCode.SERVICE_RESTART` exists (1012). There is no `RESTARTING`.
- Log fds are `FD_CLOEXEC` under the `RotatingFileHandler` that `logging_setup`
  installs, so they close on `execv` and reopen in the new image.
- `serve.sh` still ends in `wait "$core_pid"`, so a same-PID re-exec keeps both the
  supervisor and systemd's `Restart=` working.

## What changed under it

Line numbers are deliberately absent below — every one of them in the previous draft
had drifted within two days. Grep for the named function.

1. **`Session.close()` now cancels running turns.** "Refuse if any turn is running"
   is therefore a *choice* the `force` flag makes, not a constraint the code imposes.
2. **`_on_shutdown` already closes every socket** with `GOING_AWAY`, so a restart path
   needs no close loop of its own. It is also now safe to rely on: the socket closes
   go through one `asyncio.gather(..., return_exceptions=True)` over a snapshot of the
   `WeakSet`, and `mgr.close()` is in a `finally`. Previously a client reconnecting
   mid-shutdown raised out of the handler and skipped the persist, so a restart that
   hit that window was a restart that lost the sessions. That is fixed; the restart
   path inherits the fix.
3. **`chat.py` still exits on close** with no reconnect. The terminal client needs
   either a reconnect or a clear "the core restarted, run it again" line — otherwise
   a restart issued from the WebUI silently kills every terminal attached to it.
