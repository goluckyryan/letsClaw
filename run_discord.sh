#!/bin/bash
# letsClaw — Discord client. Start the core first: ./serve.sh
cd "$(dirname "$0")"
exec .venv/bin/python source/discord_client.py "$@"
