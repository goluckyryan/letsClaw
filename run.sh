#!/bin/bash
# letsClaw — Terminal agent launcher
cd "$(dirname "$0")"
exec .venv/bin/python chat.py "$@"
