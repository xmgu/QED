#!/bin/bash
set -euo pipefail

# Resolve this script's directory without relying on external tools.
# When Git Bash is launched from PowerShell or cmd.exe it inherits the
# Windows PATH, which does not contain Git's /usr/bin -- so `dirname`,
# `cp` and friends may be unavailable. Bash builtins cover everything
# this script needs.
#
# The path is normalized to forward slashes *before* the directory is
# extracted: bash's ${var%/*} does not split a Windows backslash path
# reliably, so stripping the filename first would yield the wrong path.
SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_PATH="${SCRIPT_PATH//\\//}"

case "$SCRIPT_PATH" in
    # Git Bash resolves a Windows drive path ("D:/x/y") directly, so no
    # drive-letter rewrite is needed -- "cd" handles it. What does need
    # fixing is the separator: MSYS only splits on "/".
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
    # Windows virtualenv layout (Scripts/ instead of bin/).
    PYTHON="$SCRIPT_DIR/.venv/Scripts/python.exe"
elif command -v conda >/dev/null 2>&1; then
    PYTHON="$(conda run -n agent which python)"
else
    echo "ERROR: no Python interpreter found." >&2
    echo "Create .venv in $SCRIPT_DIR, or set QED_PYTHON to a Python 3.10+ executable." >&2
    exit 1
fi

PROBLEM_FILE="${1:-$SCRIPT_DIR/problem/problem.tex}"
OUTPUT_DIR="${2:-$SCRIPT_DIR/proof_output}"
CONFIG="${3:-$SCRIPT_DIR/config.yaml}"

# Normalize backslash paths coming from a Windows caller. Without this,
# `mkdir -p "D:\path"` creates a directory literally named "D:\path".
case "$PROBLEM_FILE" in *\\*) PROBLEM_FILE="${PROBLEM_FILE//\\//}" ;; esac
case "$OUTPUT_DIR"  in *\\*) OUTPUT_DIR="${OUTPUT_DIR//\\//}" ;; esac
case "$CONFIG"      in *\\*) CONFIG="${CONFIG//\\//}" ;; esac

echo "============================================================"
echo "  Running smoke tests..."
echo "============================================================"
"$PYTHON" "$SCRIPT_DIR/code/smoke_test.py" --config "$CONFIG"
echo ""

# Copy global human_help into the output directory so the pipeline reads
# everything from proof_output.  Existing files are not overwritten
# (e.g. edited by the UI before a resume).
mkdir -p "$OUTPUT_DIR/human_help" 2>/dev/null || "$PYTHON" -c \
    "import os,sys; os.makedirs(sys.argv[1], exist_ok=True)" "$OUTPUT_DIR/human_help"

for f in "$SCRIPT_DIR/human_help"/*; do
    [ -f "$f" ] || continue
    target="$OUTPUT_DIR/human_help/${f##*/}"
    [ -e "$target" ] && continue
    # Prefer `cp` when available; fall back to the interpreter so the
    # script works even when Git's coreutils are not on PATH.
    if command -v cp >/dev/null 2>&1; then
        cp "$f" "$target" 2>/dev/null || true
    else
        "$PYTHON" -c \
            "import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2])" \
            "$f" "$target" 2>/dev/null || true
    fi
done

echo "============================================================"
echo "  Proof Agent Pipeline"
echo "============================================================"
echo "  Problem:  $PROBLEM_FILE"
echo "  Output:   $OUTPUT_DIR"
echo "  Config:   $CONFIG"
echo ""

exec "$PYTHON" "$SCRIPT_DIR/code/pipeline.py" \
    --input "$PROBLEM_FILE" \
    --output "$OUTPUT_DIR" \
    --config "$CONFIG"
