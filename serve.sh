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
# Set when a signal, rather than the core finishing on its own, started the
# shutdown. A signal-initiated stop is a clean stop and must exit 0:
# `systemctl --user stop letsClaw.service` sends SIGTERM to the whole cgroup,
# the core died of it, and `wait` handed us its status — so every ordinary
# stop left the unit in `failed (Result: exit-code, status=2)`, a red × in
# `systemctl status` and a page for anything watching the unit. The core's own
# exit status still stands when it exits by itself, which is the case
# Restart=on-failure is actually for.
signalled=0

cleanup() {
  trap - INT TERM EXIT
  # The bot first: it is the one holding a socket open on the core, and the
  # core's shutdown is quicker once nothing is attached.
  [ -n "$bot_pid" ] && kill "$bot_pid" 2>/dev/null
  [ -n "$core_pid" ] && kill -INT "$core_pid" 2>/dev/null
  wait 2>/dev/null
  [ "$signalled" = 1 ] && exit 0
}
on_signal() { signalled=1; cleanup; }
trap on_signal INT TERM
trap cleanup EXIT

"$PY" source/server.py "${args[@]}" &
core_pid=$!

if [ "$want_discord" = 1 ]; then
  # Only worth starting if the config says so. Reading the same config.yaml
  # the core just read, and honouring --config if one was passed.
  cfg_arg=""
  for ((i = 0; i < ${#args[@]}; i++)); do
    # Both spellings: argparse accepts --config=X as readily as --config X,
    # and the version that only looked for the separated form left cfg_arg
    # empty for the joined one — so the probe read the *default* config while
    # the core read the one that was asked for, and the two disagreed about
    # whether discord was even on.
    case "${args[i]}" in
      --config)   cfg_arg="${args[i+1]}" ;;
      --config=*) cfg_arg="${args[i]#--config=}" ;;
    esac
  done
  # The exit code is the verdict: 0 = start it, 2 = disabled, 3 = no token,
  # 4 = no config at all, and 1 = the probe itself raised. The table and the
  # reasons behind it live in source/discord_probe.py, which install.sh runs
  # too: this same question used to be asked twice, in two hand-written copies,
  # and they drifted until install.sh was calling every exception "no token".
  #
  # Run as a path rather than -c, so that argv[0] puts source/ on sys.path the
  # way `"$PY" source/server.py` above does.
  probe_err=$("$PY" source/discord_probe.py "$cfg_arg" 2>&1 >/dev/null)
  # Captured once: `[ $? -eq 2 ]` in an elif chain overwrites $? with its own
  # result, so every later test read the comparison, not the command.
  probe_status=$?
  # "No config" only means "first run" when nobody named one. The core, started
  # a moment ago in the background, is writing a starter config.yaml right now —
  # true when the path is the default, and a lie when --config pointed at a file
  # that is not there: nothing is going to appear at that path, and the message
  # sent the reader off waiting for it. A named path that is missing is just a
  # config that could not be read, so say that instead.
  if [ "$probe_status" = 4 ] && [ -n "$cfg_arg" ]; then
    probe_status=1
    probe_err="no config file at $cfg_arg"
  fi
  case "$probe_status" in
    0) ;;
    2) echo "   (discord.enabled is false — Discord bot not started)"
       want_discord=0 ;;
    3) echo "   (no discord.token — Discord bot not started)"
       want_discord=0 ;;
    4) echo "   (no config.yaml yet — the core just wrote a starter one; Discord bot not started)"
       want_discord=0 ;;
    *) echo "   ⚠️  could not read the discord config — bot not started"
       [ -n "$probe_err" ] && printf '%s\n' "$probe_err" | sed 's/^/      /'
       want_discord=0 ;;
  esac
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
