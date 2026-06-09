# paddleocr-daemon

Minimal PaddleOCR-VL HTTP wrapper for PDF and image parsing.

The daemon keeps PaddleOCR-style response semantics and returns Markdown text
plus extracted images for each parsed page.

This service is intended as a minimal local HTTP wrapper around the local
PaddleOCR-VL Python pipeline. It does not implement the official cloud `jobs`
API/SDK contract.

## Files

- `daemon.py`: HTTP API server
- `paddleocr.conf.example`: runtime config template
- `docker-compose.yml`: Docker deployment example
- `paddleocr-daemon.service.example`: systemd service example
- `smoke/`: local smoke-test tools and output bundle generation
- `llama.cpp/`: reference files used for the host-side `llama-server` setup

## API

### Health

```bash
curl http://127.0.0.1:8080/health
```

### Readiness

```bash
curl http://127.0.0.1:8080/health/ready
```

This endpoint verifies:

- the configured `VLM_SERVER_URL` is syntactically valid
- the remote OpenAI-compatible `/v1/models` endpoint returns a parseable model list
- the local PaddleOCR-VL pipeline can initialize successfully

### JSON API

```bash
curl -X POST http://127.0.0.1:8080/layout-parsing \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-token" \
  -d '{
    "file": "<base64>",
    "fileType": 0
  }'
```

`fileType=0` means PDF, `fileType=1` means image.

Extra request fields are ignored.

Current primary parsing entrypoint:

- `POST /layout-parsing`

Input scope:

- Supported: base64 file content in JSON
- Not supported: remote `fileUrl` / URL fetching

### Response Shape

```json
{
  "logId": "...",
  "errorCode": 0,
  "errorMsg": "Success",
  "result": {
    "layoutParsingResults": [
      {
        "markdown": {
          "text": "...",
          "images": {
            "img1.png": "<base64>"
          }
        }
      }
    ]
  }
}
```

## Configuration

Runtime config file:

```bash
/etc/paddleocr/paddleocr.conf
```

Start from the example file:

```bash
cp paddleocr.conf.example /etc/paddleocr/paddleocr.conf
```

Important fields:

- `VLM_BACKEND`: remote VLM backend name
- `VLM_SERVER_URL`: remote VLM endpoint
- `VLM_API_KEY`: for OpenAI-compatible / llama.cpp-server style backends,
  keep this non-empty; `no-key` is the default placeholder and is safer than
  leaving it blank
- `API_HOST`, `API_PORT`: listen address
- `API_TOKEN`: optional Bearer token for POST endpoints
- `PADDLE_PDX_MODEL_SOURCE`: preferred official model source, default `modelscope`
- `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK`: skip model source connectivity check
- `TMP_DIR`: temporary storage for uploaded request inputs; incoming PDF/image
  data is materialized here as a local file before `pipeline.predict(...)`
- `OUTPUT_DIR`: reserved for a future feature that will persist request results;
  current versions create the directory but do not write outputs there yet

Config file constraints:

- Use plain `KEY=VALUE` lines
- Do not wrap values in quotes
- Do not use `~` in paths; use absolute paths instead

About `OUTPUT_DIR`:

- It is intentionally kept in the config as a forward-looking contract
- The intended use is to capture all parsed request results for user-data
  analysis and system-effectiveness research
- The current daemon does not implement this persistence behavior yet

## Docker Compose

Prepare config:

```bash
mkdir -p /root/.paddleocr
cp paddleocr.conf.example /root/.paddleocr/paddleocr.conf
```

Then start:

```bash
docker compose up -d --build
```

Current compose behavior:

- publishes `8080:8080`
- mounts `/root/.paddleocr` to `/etc/paddleocr`
- persists PaddleX model cache in `/root/.paddlex`

The `/root/.paddleocr` host path is only an example. Change it to match your
deployment environment if needed.

If the VLM server runs on the Docker host, the default installed config inside
the image rewrites `127.0.0.1` to `172.17.0.1` for the fallback container-side
path.

## systemd

Suggested layout:

```bash
/opt/paddleocr-daemon
/etc/paddleocr/paddleocr.conf
```

Install example service:

```bash
cp paddleocr-daemon.service.example /etc/systemd/system/paddleocr-daemon.service
systemctl daemon-reload
systemctl enable --now paddleocr-daemon
```

The service uses:

- `EnvironmentFile=-/etc/paddleocr/paddleocr.conf`
- `ExecStart=/usr/bin/python3 /opt/paddleocr-daemon/daemon.py`

Unlike Docker, the systemd service does not use `start.sh`.

## Build-Time Pre-Download

`install.sh` can instantiate `PaddleOCRVL(...)` during image build to trigger
download of PaddleX / PaddleOCR official models into the cache.

This behavior is controlled by:

```conf
PREFORK_MODELS=1
```

If disabled, the required local models are downloaded on first real use.

## Smoke Test

Set up the local virtualenv and dependencies:

```bash
bash smoke/setup.sh
```

Run the one-shot smoke test:

```bash
bash smoke/test.sh
```

This script will:

- ensure `smoke/artifacts/` exists
- download the example arXiv PDF `1706.03762` into `smoke/artifacts/`
- call `client.py`, which submits the PDF to `layout-parsing`
- export a bundle directory containing:
  - `content.md`
  - `images/`
  - `result.json`
  - `timing.txt`
