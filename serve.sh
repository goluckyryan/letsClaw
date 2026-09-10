#!/bin/bash
# letsClaw — core service. Start this before any client.
cd "$(dirname "$0")"
exec .venv/bin/python server.py "$@"
