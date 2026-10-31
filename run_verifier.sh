#!/usr/bin/env bash
# Thin wrapper around the standalone verifier.
#
# The verifier itself is a Python script; this wrapper only locates an
# interpreter and forwards the arguments.
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

OUTPUT_DIR="$SCRIPT_DIR/standalone_verifier_result"
mkdir -p "$OUTPUT_DIR" 2>/dev/null || "$PYTHON" -c \
    "import os,sys; os.makedirs(sys.argv[1], exist_ok=True)" "$OUTPUT_DIR"

if [ $# -eq 0 ]; then
    # No arguments: default problem + proof from standalone_verifier/
    "$PYTHON" "$SCRIPT_DIR/verify/verify.py" \
        "$SCRIPT_DIR/standalone_verifier/problem.txt" \
        "$SCRIPT_DIR/standalone_verifier/proof.txt" \
        -o "$OUTPUT_DIR/report.md"
else
    # Pass all arguments through; argparse handles the rest
    "$PYTHON" "$SCRIPT_DIR/verify/verify.py" \
        -o "$OUTPUT_DIR/report.md" \
        "$@"
fi
