#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
ARTIFACTS_DIR="$ROOT_DIR/smoke/artifacts"
PDF_URL="https://arxiv.org/pdf/1706.03762"
PDF_PATH="$ARTIFACTS_DIR/1706.03762.pdf"
OUTPUT_DIR="$ARTIFACTS_DIR/1706.03762_bundle"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "virtualenv not found: $VENV_DIR" >&2
    echo "run smoke/setup.sh first" >&2
    exit 1
fi

source "$VENV_DIR/bin/activate"

mkdir -p "$ARTIFACTS_DIR"

if [ ! -f "$PDF_PATH" ]; then
    wget -O "$PDF_PATH" "$PDF_URL"
fi

python "$ROOT_DIR/smoke/client.py" "$PDF_PATH" "$OUTPUT_DIR" "$@"
