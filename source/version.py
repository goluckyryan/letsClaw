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

**Every commit bumps this.** Semver still chooses which field moves; what it
does not get to choose is whether one moves at all. The rule is not about
releases — it is about who is working in this tree. Several agents commit here,
each seeing only its own change, and a number that moves on somebody's judgement
of "is this worth a bump?" stops moving at all: 1.0.0 through 1.0.5 covers the
pinned band, the sidebar drag, the turn digest, the turn/round stamps and the
prompt arrows, every one of them a feature, all of them filed as patches. If you
are writing a commit and have not touched the line below, the commit is not
finished.
"""

__version__ = "1.0.13"
