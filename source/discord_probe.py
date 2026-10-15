#!/usr/bin/env python3
"""Should the Discord bot be started? One answer, for both shell scripts.

serve.sh asks before starting the bot; install.sh asks before installing
discord.py. They each used to carry their own copy of this question, and the
copies drifted exactly as two copies do: serve.sh learned to tell an exception
apart from a verdict, install.sh kept the version where `else 1` and a
discarded stderr made any uncaught exception — a renamed import, a bad
--config, a missing config.yaml — look precisely like "no token". A fresh
install with the bot enabled then got no discord.py, and the reason was thrown
away. One script, called twice, is what stops that happening again.

The verdict is the exit code, and 1 is deliberately not one of them: 1 is what
Python exits with on an uncaught exception, so a 1 means this script itself
broke and the traceback on stderr is the reason. Both callers print it.

    0   start it        enabled (or unset, which means enabled) and a token
    2   disabled        discord.enabled: false — a token that does nothing
    3   no token        enabled, but nothing to log in with
    4   no config       there is no config file at all, yet
    1   anything else   this script raised; see stderr

Usage:  discord_probe.py [config path]   — empty or absent means the default.
"""
import sys

from config import load_config


def verdict(path=None):
    """The exit code for this config. See the module docstring for the table."""
    try:
        cfg = load_config(path or None)
    except FileNotFoundError:
        # No config file. Whether that is a first run or a typo on --config is
        # not this script's call to make — it reports what it found, and the
        # caller, which knows whether a path was asked for by name, says what
        # it means.
        return 4
    discord = cfg.get("discord") or {}
    # Absent `enabled` means enabled, so a config that never had the key is
    # unaffected; a token that is set but disabled is a token that does nothing.
    if discord.get("enabled", True) is False:
        return 2
    return 0 if str(discord.get("token") or "").strip() else 3


if __name__ == "__main__":
    sys.exit(verdict(sys.argv[1] if len(sys.argv) > 1 else None))
