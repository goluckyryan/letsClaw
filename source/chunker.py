"""chunker.py — split an answer into messages Discord will accept.

A port of openclaw's `extensions/discord/src/chunk.ts`, kept deliberately close
to the original so its test cases port with it.

Discord takes 2000 characters per message, and its clients collapse very tall
messages, so a long answer has to be cut twice over — by length and by line
count. Cutting text at arbitrary points breaks Markdown, and the failure is
ugly in a specific way: a code fence opened in one message and closed in the
next renders as an unterminated block that swallows everything after it. So the
splitter tracks the open fence, closes it at the end of each chunk and reopens
it at the top of the next, and reserves the room to do that before deciding a
line fits.

No Discord imports and no I/O: this is a string function, which is what makes
the fence and italics rules testable on their own.

One thing this deliberately does *not* do is measure the way Discord counts.
The limit is 2000 UTF-16 code units, not 2000 characters, and the two differ
wherever an astral character (most emoji) appears. Measuring in code points is
what keeps this readable and its tests meaningful; re-measuring for the wire is
the sender's job — see `_hard_split` in discord_client.py.
"""

import re

DEFAULT_MAX_CHARS = 2000
# Discord clients clip or collapse very tall messages. A soft cap on lines keeps
# a multi-paragraph answer readable as a sequence of messages instead of one
# wall the client decides to fold away.
DEFAULT_MAX_LINES = 17

# Up to three spaces of indent, then three or more backticks or tildes — the
# CommonMark fence rule, which is what Discord follows.
FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


class _Fence:
    """An open code fence: what it takes to close one and to reopen it."""

    __slots__ = ("indent", "char", "length", "open_line")

    def __init__(self, indent, char, length, open_line):
        self.indent = indent
        self.char = char
        self.length = length
        self.open_line = open_line

    @property
    def close_line(self):
        return self.indent + self.char * self.length


def _count_lines(text):
    return len(text.split("\n")) if text else 0


def _parse_fence(line):
    m = FENCE_RE.match(line)
    if not m:
        return None
    marker = m.group(2)
    return _Fence(m.group(1), marker[0], len(marker), line)


def _close_if_needed(text, fence):
    """Terminate an open fence so this chunk renders on its own."""
    if fence is None:
        return text
    if not text:
        return fence.close_line
    joiner = "" if text.endswith("\n") else "\n"
    return f"{text}{joiner}{fence.close_line}"


def _split_long_line(line, max_chars, preserve_whitespace):
    """Cut one over-long line into pieces that fit.

    Inside a fence the text is code, so it is cut at exactly the limit —
    reflowing it would change what it says. Outside one, the cut backs up to the
    last space in the window and the separator rides along to the next piece, so
    the chunks still concatenate back to the original and words stay whole.
    """
    limit = max(1, int(max_chars))
    if len(line) <= limit:
        return [line]

    out, remaining = [], line
    while len(remaining) > limit:
        if preserve_whitespace:
            out.append(remaining[:limit])
            remaining = remaining[limit:]
            continue
        window = remaining[:limit]
        brk = -1
        for i in range(len(window) - 1, -1, -1):
            if window[i].isspace():
                brk = i
                break
        # No space to break on — a URL, or a CJK run, which has no spaces at
        # all. Cut at the limit rather than hand back an over-long piece.
        if brk <= 0:
            brk = limit
        out.append(remaining[:brk])
        remaining = remaining[brk:]
    if remaining:
        out.append(remaining)
    return out


def chunk_discord_text(text, max_chars=DEFAULT_MAX_CHARS, max_lines=DEFAULT_MAX_LINES):
    """Split `text` into messages, each within both limits and self-contained.

    Returns [] for empty input, and the text unchanged in a single-element list
    when it already fits — the common case, and worth not walking line by line.
    """
    max_chars = max(1, int(max_chars))
    max_lines = max(1, int(max_lines))

    body = text or ""
    if not body:
        return []
    if len(body) <= max_chars and _count_lines(body) <= max_lines:
        return [body]

    chunks = []
    current = ""
    current_lines = 0
    fence = None

    def flush():
        """Emit what has accumulated, then reopen the fence on the next chunk."""
        nonlocal current, current_lines
        if not current:
            return
        payload = _close_if_needed(current, fence)
        if payload.strip():
            chunks.append(payload)
        current, current_lines = "", 0
        if fence is not None:
            current = fence.open_line
            current_lines = 1

    for original in body.split("\n"):
        found = _parse_fence(original)
        was_inside = fence is not None
        next_fence = fence
        if found is not None:
            if fence is None:
                next_fence = found
            elif found.char == fence.char and found.length >= fence.length:
                # A closing marker has to match the opener's character and be at
                # least as long — ``` does not close ````.
                next_fence = None

        # Room held back for the closing line this chunk may have to add.
        reserve_chars = len(next_fence.close_line) + 1 if next_fence else 0
        reserve_lines = 1 if next_fence else 0
        # A limit small enough that the reserve eats all of it is no limit at
        # all; fall back to the raw one rather than loop forever on zero.
        effective_chars = max_chars - reserve_chars
        effective_lines = max_lines - reserve_lines
        char_limit = effective_chars if effective_chars > 0 else max_chars
        line_limit = effective_lines if effective_lines > 0 else max_lines

        prefix = len(current) + 1 if current else 0
        segments = _split_long_line(original, max(1, char_limit - prefix),
                                    preserve_whitespace=was_inside)

        for index, segment in enumerate(segments):
            continuation = index > 0
            delimiter = "" if continuation else ("\n" if current else "")
            addition = delimiter + segment
            over_chars = len(current) + len(addition) > char_limit
            over_lines = current_lines + (0 if continuation else 1) > line_limit
            if (over_chars or over_lines) and current:
                flush()
            if current:
                current += addition
                if not continuation:
                    current_lines += 1
            else:
                current = segment
                current_lines = 1

        fence = next_fence

    if current:
        payload = _close_if_needed(current, fence)
        if payload.strip():
            chunks.append(payload)

    return _rebalance_italics(body, chunks)


def _rebalance_italics(source, chunks):
    """Keep a wholly-italicised block italic in every chunk.

    Reasoning is posted as one `_…_` span. Split that across messages and every
    chunk after the first renders as plain text with a stray underscore, so each
    chunk is closed and the next reopened. Only whole-span italics are touched —
    text with its own inline emphasis is left exactly as written.
    """
    if len(chunks) <= 1:
        return chunks
    if not (source.startswith("Reasoning:\n_") and source.rstrip().endswith("_")):
        return chunks

    out = list(chunks)
    for i, current in enumerate(out):
        if not current.rstrip().endswith("_"):
            out[i] = current + "_"
        if i == len(out) - 1:
            break
        nxt = out[i + 1]
        lead = len(nxt) - len(nxt.lstrip())
        if not nxt[lead:].startswith("_"):
            out[i + 1] = nxt[:lead] + "_" + nxt[lead:]
    return out
