#!/bin/bash
# letsClaw — terminal client. Start the core first: ./serve.sh
cd "$(dirname "$0")"
exec .venv/bin/python chat.py "$@"
