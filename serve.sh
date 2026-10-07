#!/bin/bash
# letsClaw — the core service, and the Discord bot alongside it.
#
#   ./serve.sh                 the core, plus the bot when discord is enabled
#   ./serve.sh --no-discord    the core on its own
#   ./serve.sh --port 9000     anything else is passed straight to server.py
#
# Ctrl-C stops both. The bot is skipped, with a note, when discord.enabled is
# false or when no token is configured — so this stays the right command to
# run whether or not you have set Discord up. ./run_discord.sh still starts a
# bot by itself, for a bot on a different machine from its core.
cd "$(dirname "$0")"
PY=.venv/bin/python

want_discord=1
args=()
for a in "$@"; do
  case "$a" in
    --no-discord) want_discord=0 ;;
    *) args+=("$a") ;;
  esac
done

core_pid=""
bot_pid=""

cleanup() {
  trap - INT TERM EXIT
  # The bot first: it is the one holding a socket open on the core, and the
  # core's shutdown is quicker once nothing is attached.
  [ -n "$bot_pid" ] && kill "$bot_pid" 2>/dev/null
  [ -n "$core_pid" ] && kill -INT "$core_pid" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup INT TERM EXIT

"$PY" source/server.py "${args[@]}" &
core_pid=$!

if [ "$want_discord" = 1 ]; then
  # Only worth starting if the config says so. Reading the same config.yaml
  # the core just read, and honouring --config if one was passed.
  cfg_arg=""
  for ((i = 0; i < ${#args[@]}; i++)); do
    [ "${args[i]}" = "--config" ] && cfg_arg="${args[i+1]}"
  done
  # The exit code is the verdict: 0 = start it, 1 = no token, 2 = disabled.
  # Absent `enabled` means enabled, so a config that never had the key is
  # unaffected; a token that is set but disabled is a token that does nothing.
  if "$PY" -c "
import sys
sys.path.insert(0, 'source')
from core import load_config
cfg = load_config(sys.argv[1] or None)
d = cfg.get('discord') or {}
sys.exit(2 if d.get('enabled', True) is False else
         0 if str(d.get('token') or '').strip() else 1)
" "$cfg_arg" 2>/dev/null; then
    :
  elif [ $? -eq 2 ]; then
    echo "   (discord.enabled is false — Discord bot not started)"
    want_discord=0
  elif [ $? -eq 1 ]; then
    echo "   (no discord.token — Discord bot not started)"
    want_discord=0
  fi
  if [ "$want_discord" = 1 ]; then
    # A moment for the core to bind, purely so the first log lines read in
    # order — the bot reconnects with backoff regardless, so this is cosmetic.
    sleep 2
    "$PY" source/discord_client.py ${cfg_arg:+--config "$cfg_arg"} &
    bot_pid=$!
  fi
fi

# Exit when the core does, whatever happens to the bot: the bot is optional,
# the core is the thing you asked for.
wait "$core_pid"
