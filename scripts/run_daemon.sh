#!/bin/bash
# Wrapper script for Asmi eval daemon.
# Using bash as the entry point so macOS Full Disk Access applies to bash
# (a system binary), which has FDA by default.

export PYTHONUNBUFFERED=1
export PATH="/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/daemon.py"

exec "$PYTHON" "$SCRIPT"
