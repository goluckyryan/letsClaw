"""The version of this checkout — one string, read by everything that shows it.

Separate from `PROTOCOL_VERSION` in core.py, which answers a different question.
The protocol number says whether a client and the core can talk to each other at
all, and changes only when the event stream does; this one says which build of
letsClaw is running, and moves on every release. A WebUI tab can be three
releases stale and still speak protocol 1 quite happily, which is exactly why
the page shows the core's figure rather than one baked into app.js.

Semantic versioning: major for a break in config.yaml or the on-disk state,
minor for features, patch for fixes. Bumped by hand here — there is no package
metadata to keep in step, and this file is the only place the number lives.
"""

__version__ = "1.0.4"
