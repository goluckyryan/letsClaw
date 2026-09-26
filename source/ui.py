"""UI helpers — terminal indicators for long LLM waits."""

import asyncio
import sys
import time

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_INTERVAL = 0.1
_GRACE = 0.3  # don't flash the indicator for fast responses


class thinking_indicator:
    """Live '🐱 ⠋ thinking… N.Ns' line shown while waiting on the LLM.

    Usage:
        ind = thinking_indicator()
        ind.start()
        ... await the LLM; call ind.stop() on first token (or when done) ...
        ttft = ind.ttft  # seconds

    - Only draws after a short grace period, so fast answers stay clean.
    - No animation when stdout is not a TTY (timing still recorded).
    - stop() is idempotent; safe to call multiple times.
    - `tokens` and `total`, when set, ride along on the line: what this run of
      thinking has produced, over the turn's running total once a tool round has
      split the turn into more than one run. Both are writable while running —
      the caller just assigns to them — and both are constructor arguments, so a
      spinner restarted after a tool result picks the count back up instead of
      dropping it.
    """

    def __init__(self, label="thinking", tokens=None, total=None):
        self.label = label
        self.tokens = tokens
        self.total = total
        self.ttft = None
        self._task = None
        self._t0 = None
        self._visible = False
        self._tty = sys.stdout.isatty()

    def _thought(self):
        """' · 6 / 839 tok' — this run of thinking over the turn's total."""
        total = self.total or self.tokens or 0
        block = self.tokens or 0
        if not total:
            return ""
        if not block:            # a tool is running; nothing is being thought
            return f" · {total:,} tok this turn"
        if block >= total:       # one run so far, so the two are the same number
            return f" · {block:,} tok"
        return f" · {block:,} / {total:,} tok"

    def start(self):
        """Begin timing (and animate, on a TTY)."""
        if self._t0 is not None:
            return
        self._t0 = time.monotonic()
        if self._tty:
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        i = 0
        while True:
            await asyncio.sleep(_INTERVAL)
            elapsed = time.monotonic() - self._t0
            if elapsed < _GRACE:
                continue
            frame = _FRAMES[i % len(_FRAMES)]
            i += 1
            self._visible = True
            sys.stdout.write(
                f"\r🐱 {frame} {self.label}… {elapsed:.1f}s{self._thought()}\x1b[K")
            sys.stdout.flush()

    def stop(self):
        """Stop the animation, clear the line, record time-to-first-token."""
        if self._t0 is not None and self.ttft is None:
            self.ttft = time.monotonic() - self._t0
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._visible:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
            self._visible = False
