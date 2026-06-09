#!/bin/bash
set -euo pipefail

load_config_defaults() {
    local config_path="$1"
    local line key

    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"

        case "$line" in
            ''|'#'*)
                continue
                ;;
        esac

        case "$line" in
            *=*)
                key="${line%%=*}"
                ;;
            *)
                continue
                ;;
        esac

        if [ -n "${!key+x}" ]; then
            continue
        fi

        export "$line"
    done < "$config_path"
}

if [ -f /etc/paddleocr/paddleocr.conf ]; then
    load_config_defaults /etc/paddleocr/paddleocr.conf
else
    echo "config file not found: /etc/paddleocr/paddleocr.conf" >&2
    exit 1
fi

# Keep shell-side behavior aligned with daemon.py defaults so optional config
# lines can stay omitted without causing start.sh to fail under `set -u`.
PADDLE_DEVICE="${PADDLE_DEVICE:-cpu}"
PIPELINE_VERSION="${PIPELINE_VERSION:-v1.6}"
VLM_BACKEND="${VLM_BACKEND:-llama-cpp-server}"
VLM_SERVER_URL="${VLM_SERVER_URL:-http://127.0.0.1:3000/v1}"
API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8080}"
API_TOKEN="${API_TOKEN:-}"
TMP_DIR="${TMP_DIR:-/tmp/paddleocr}"
OUTPUT_DIR="${OUTPUT_DIR:-/root/output}"

echo "=== Environment ==="
echo "PADDLE_DEVICE=$PADDLE_DEVICE"
echo "PIPELINE_VERSION=$PIPELINE_VERSION"
echo "VLM_BACKEND=$VLM_BACKEND"
echo "VLM_SERVER_URL=$VLM_SERVER_URL"
echo "API_HOST=$API_HOST"
echo "API_PORT=$API_PORT"
echo "API_TOKEN=${API_TOKEN:+<set>}"
echo "TMP_DIR=$TMP_DIR"
echo "OUTPUT_DIR=$OUTPUT_DIR"

echo "=== Daemon ==="
exec python /app/daemon.py
