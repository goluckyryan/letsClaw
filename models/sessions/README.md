Per-session behavior files.

A file here named for a session — `<session name>.md` — is injected into that
session's system prompt, after the model's behavior file and before any
rollover handoff.

For the Discord bot this is per channel, because the bot names each session
after its channel (`auto_names`): `#solaris` in Dudu's Den is the session
`dudus-den-solaris`, so this directory's `dudus-den-solaris.md` is that
channel's file. The core never learns that Discord exists — it only ever sees
a session name.

    models/sessions/dudus-den-solaris.md     the #solaris channel
    models/sessions/terminal.md              ./terminalUI.sh
    models/sessions/daq-notes.md             a WebUI session called daq-notes

A session with no file here is unaffected. `ls` tells you which sessions are
tuned, which is the point of a directory rather than a config map.

Creating a file for a session that is already live needs a `/reload` to be
noticed — the read is cached, the same as `base.md` is.

This file is not a session behavior file unless you have a session called
"README", which you should not.

Everything in here except this README is gitignored. The files are named after
whatever sessions a given machine happens to have, and their contents go
straight into a system prompt — neither travels usefully between checkouts.
A fresh clone gets this directory with only this file in it, which is all the
layer needs to be switched on.
