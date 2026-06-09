#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------
sed -i 's|http://deb.debian.org|http://mirrors.cernet.edu.cn|g' /etc/apt/sources.list.d/debian.sources
apt-get update
apt-get install -y -qq --no-install-recommends \
    fonts-dejavu-core \
    fonts-noto-cjk \
    fonts-liberation \
    fonts-freefont-ttf \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    curl

# Clean apt cache
apt-get clean
rm -rf /var/lib/apt/lists/*

# Install the default runtime config into the image so the daemon can start
# with sane defaults even before a host config is mounted.
install -Dm644 paddleocr.conf.example /etc/paddleocr/paddleocr.conf
sed -i 's|^VLM_SERVER_URL=http://127\.0\.0\.1:|VLM_SERVER_URL=http://172.17.0.1:|g' /etc/paddleocr/paddleocr.conf
export PADDLE_PDX_MODEL_SOURCE="${PADDLE_PDX_MODEL_SOURCE:-modelscope}"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="${PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK:-True}"

# ---------------------------------------------------------------------------
# PaddlePaddle (CPU by default; override PADDLE_DEVICE=gpu for CUDA 12.6)
# ---------------------------------------------------------------------------
PADDLE_DEVICE="${PADDLE_DEVICE:-cpu}"

if [ "$PADDLE_DEVICE" = "gpu" ]; then
    pip install "paddlepaddle-gpu==3.2.1" \
        -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
else
    pip install "paddlepaddle==3.2.1" \
        -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
fi

# ---------------------------------------------------------------------------
# PaddleOCR + PaddleX (doc-parser pipeline)
# ---------------------------------------------------------------------------
# PaddleOCR 3.6.0 is currently the pinned baseline for this image.
pip install "paddleocr[doc-parser]==3.6.0" \
        -i https://mirrors.cernet.edu.cn/pypi/web/simple

# ---------------------------------------------------------------------------
# Pre-download models (optional, controlled by PREFORK_MODELS env)
# ---------------------------------------------------------------------------
PREFORK="${PREFORK_MODELS:-0}"

if [ "$PREFORK" = "1" ]; then
    echo "Pre-downloading PaddleOCR-VL models"
    python - <<'PYEOF'
from paddleocr import PaddleOCRVL
import os

vlm_url = os.environ.get("VLM_SERVER_URL", "http://127.0.0.1:3000/v1")
vlm_key = os.environ.get("VLM_API_KEY", "no-key")
vlm_backend = os.environ.get("VLM_BACKEND", "llama-cpp-server")
version = os.environ.get("PIPELINE_VERSION", "v1.6")

print(f"Creating pipeline (version={version}, backend={vlm_backend})...")
_pipeline = PaddleOCRVL(
    pipeline_version=version,
    vl_rec_backend=vlm_backend,
    vl_rec_server_url=vlm_url,
    vl_rec_api_key=vlm_key
)
print("Models pre-downloaded successfully.")
PYEOF
else
    echo "Skipping model pre-download (PREFORK_MODELS != 1)."
fi
