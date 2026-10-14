"""Editing the webui: block of config.yaml in place, as text.

A line-level edit, not a YAML round-trip: config.yaml is heavily commented and
dumping it back would delete every note in it. Existing keys are edited in
place — value replaced, inline comment kept — and keys the form sends that the
file lacks are appended inside the block.

Pure string -> string, with no dependency on anything else in the tree. It
lived inside SessionManager.save_webui, where it could only be reached by
building a manager and awaiting a save, which is why the two bugs below went
unnoticed for as long as they did.
"""
import re

import yaml

_THEME_KEY_RE = re.compile(r"^  theme\s*:\s*(.*)$")
# Anchored at four spaces so it matches a key nested under `pin:` and not an
# arbitrary four-space-indented key elsewhere in the block.
_PIN_KEY_RE = re.compile(r"^    (\w+)\s*:\s*(.*)$")


def yaml_scalar(v):
    """One value as a YAML line fragment, round-tripping through safe_load.

    Booleans are spelled by hand so the intent is visible at the call site;
    everything else goes through the dumper, which quotes exactly what needs
    quoting (a '#1e222b' color, a value containing ': ').
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    dumped = yaml.safe_dump({"_": v}, default_flow_style=False, width=10 ** 6)
    return dumped.splitlines()[0].split(": ", 1)[1]


def comment_start(line):
    """The index of the '#' that starts an inline comment, or None.

    A '#' with unbalanced quotes before it is inside a quoted value — a
    hex color like '#1e222b' — not a comment, so the scan keeps going.
    """
    for i, ch in enumerate(line):
        if ch == "#" and not (line[:i].count('"') % 2 or line[:i].count("'") % 2):
            return i
    return None


def revalue(line, new_head):
    """A key line with its value replaced, keeping the inline comment —
    spacing and all — and the line ending. The file's own notes survive
    the save, and a hex color in the value does not read as a comment.

    The comment keeps the *gap* it had, not the column: a longer value
    pushes it right. Holding the column would mean eating the whitespace,
    which cannot work once the new value is wider than the gap was."""
    eol = "\n"
    for ch in ("\r\n", "\n", "\r"):
        if line.endswith(ch):
            eol = ch
            line = line[: -len(ch)]
            break
    idx = comment_start(line)
    if idx is None:
        return new_head + eol
    sp = idx
    while sp > 0 and line[sp - 1] in " \t":
        sp -= 1
    return new_head + line[sp:idx] + line[idx:] + eol


def count_blocks(text):
    """How many top-level `webui:` keys the text has.

    More than one is data loss waiting to happen: yaml.safe_load keeps the
    last of a duplicated key, so a second block hides everything in the first.
    """
    return sum(1 for line in text.splitlines() if line.startswith("webui:"))


def _block_bounds(lines):
    """(start, tail) for the top-level `webui:` block, or None if it has none.

    `start` is the index of the `webui:` line; `tail` is where a new key
    belongs — one past the last indented key in the block.

    The block ends at the first line that begins in column 0 and is neither
    blank nor a comment. A comment at column 0 does *not* end it: the user is
    entitled to write a note hard against the left margin inside their own
    section, and treating one as the end used to send the leftover keys to a
    second `webui:` block at the foot of the file, where they shadowed the
    whole original on the next load.

    `tail` stops at the last real key rather than at the block boundary, so
    the blank line and the comment that introduce the next section stay where
    the user put them, above that section and not swallowed by this one.
    """
    start = next((i for i, ln in enumerate(lines) if ln.startswith("webui:")), None)
    if start is None:
        return None
    tail = start + 1
    i = start + 1
    while i < len(lines):
        line = lines[i]
        body = line.strip()
        if body and line[0] not in " \t":
            if not body.startswith("#"):
                break                 # the next top-level section
        elif body:
            tail = i + 1              # an indented key: the block reaches here
        i += 1
    return start, tail


def _pin_bounds(lines, start, tail):
    """(header, tail) for the `pin:` mapping inside the block, or None.

    Needed because a pin key appended at the end of the whole block nests
    under whatever key happens to come last, which is the pin mapping only by
    luck of ordering — put `pin:` above `theme:` and the appended key lands
    four spaces under `theme:` instead.
    """
    header = next((i for i in range(start + 1, tail)
                   if re.match(r"^  pin\s*:", lines[i])), None)
    if header is None:
        return None
    pin_tail = header + 1
    for i in range(header + 1, tail):
        body = lines[i].strip()
        if not body or body.startswith("#"):
            continue
        if lines[i].startswith("    "):
            pin_tail = i + 1
        else:
            break                     # a sibling key: the mapping is over
    return header, pin_tail


def _insert(lines, at, body):
    """Splice `body` in at `at`, making sure the line above it ends in one."""
    if not body:
        return
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines[at:at] = body


def edit_webui_block(text, values):
    """config.yaml with `values` written into its webui: block.

    `values` maps "theme" and "pin.<key>" to already-formatted YAML scalars
    (see yaml_scalar). Keys found in the block are edited in place; whatever
    is left over is inserted into it. Everything else in the file — comments,
    unknown keys, ordering, line endings — is returned untouched.

    Raises ValueError rather than return a file with the webui: block
    duplicated. The caller validates the result by parsing it, and a parse
    cannot see this: `isinstance(fresh["webui"], dict)` is just as true of the
    second block as of the first.
    """
    values = dict(values)
    if not values:
        return text

    lines = text.splitlines(keepends=True)
    bounds = _block_bounds(lines)

    if bounds is None:
        # No block at all. Start one at the foot of the file.
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines:
            lines.append("\n")
        lines.append("webui:\n")
        tail, pin = len(lines), None
    else:
        start, tail = bounds
        pin = _pin_bounds(lines, start, tail)
        for i in range(start + 1, tail):
            line = lines[i]
            if _THEME_KEY_RE.match(line) and "theme" in values:
                lines[i] = revalue(line, f"  theme: {values.pop('theme')}")
                continue
            # Only inside the pin mapping: a four-space key elsewhere in the
            # block belongs to some other nested setting, and rewriting it
            # with a pin value would put the colour in the wrong place.
            if pin is None or not pin[0] < i < pin[1]:
                continue
            m = _PIN_KEY_RE.match(line)
            if m and f"pin.{m.group(1)}" in values:
                key = f"pin.{m.group(1)}"
                lines[i] = revalue(line, f"    {m.group(1)}: {values.pop(key)}")

    # Whatever the file did not already have goes in — the pin keys into the
    # pin mapping, the rest at the end of the block.
    under_pin = [f"    {k.split('.', 1)[1]}: {v}\n"
                 for k, v in values.items() if k.startswith("pin.")]
    at_tail = [f"  {k}: {v}\n" for k, v in values.items() if not k.startswith("pin.")]
    if under_pin and pin is None:
        at_tail += ["  pin:\n"] + under_pin
        under_pin = []
    if under_pin:
        # The deeper insert first: it sits above `tail` and shifts it.
        _insert(lines, pin[1], under_pin)
        tail += len(under_pin)
    _insert(lines, tail, at_tail)

    result = "".join(lines)
    if count_blocks(result) != max(count_blocks(text), 1):
        raise ValueError(
            f"refusing to write a config with {count_blocks(result)} webui: "
            f"blocks; the later one would hide the earlier on load")
    return result
