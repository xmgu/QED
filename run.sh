#!/bin/bash
# Thin wrapper around the cross-platform Python entry point.
#
# The pipeline itself no longer needs a shell -- code/run.py does the
# work on every platform. This script stays so that the familiar
# `bash run.sh <problem> <output>` invocation keeps working.
set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_PATH="${SCRIPT_PATH//\\//}"
case "$SCRIPT_PATH" in
    [A-Za-z]:/*) SCRIPT_PATH="${SCRIPT_PATH:0:1}:/${SCRIPT_PATH:3}" ;;
esac
case "$SCRIPT_PATH" in
    */*) SCRIPT_DIR="${SCRIPT_PATH%/*}" ;;
    *)   SCRIPT_DIR="." ;;
esac
SCRIPT_DIR="$(cd "$SCRIPT_DIR" && pwd -P)"

if [ -n "${QED_PYTHON:-}" ]; then
    PYTHON="$QED_PYTHON"
elif [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif [ -x "$SCRIPT_DIR/.venv/Scripts/python.exe" ]; then
    PYTHON="$SCRIPT_DIR/.venv/Scripts/python.exe"
elif command -v conda >/dev/null 2>&1; then
    PYTHON="$(conda run -n agent which python)"
else
    echo "ERROR: no Python interpreter found." >&2
    echo "Create .venv in $SCRIPT_DIR, or set QED_PYTHON to a Python 3.10+ executable." >&2
    exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/code/run.py" "$@"
