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

Returns HTTP `200` while the daemon is running:

```json
{"logId": "...", "errorCode": 0, "errorMsg": "Healthy"}
```

### Readiness

```bash
curl http://127.0.0.1:8080/ready
```

Probes whether the configured VLM backend is reachable. Returns `200` if
ready, `503` otherwise:

```json
{"logId": "...", "errorCode": 0, "errorMsg": "Ready"}
```

```json
{"logId": "...", "errorCode": 1, "errorMsg": "Service unavailable"}
```

Note: the daemon does not implement the full PaddleOCR cloud `jobs` API/SDK
contract, but the health/ready response shape follows the AIStudio convention
(`logId` / `errorCode` / `errorMsg`).

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

`fileType=0` means PDF, `fileType=1` means image (the default when omitted).

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
        },
      }
    ]
  }
}
```

Notes:

- `markdown.text` is normalized from PaddleOCR-VL's local `markdown_texts` field.
- `markdown.images` is normalized from PaddleOCR-VL's local `markdown_images` field and returned as a per-page embedded asset map.
- `layoutParsingResults` is a per-page array. The daemon does not merge pages into a single markdown document.
- `markdown.images` keys are intended to match the relative image references embedded in `markdown.text`.
- In current PaddleOCR-VL output, embedded image references often use paths such as `imgs/...`, but this is part of the markdown content generated upstream rather than a daemon-enforced directory contract.
- Per-page pass-through fields (`prunedResult`, `outputImages`, `inputImage`, `exports`): the daemon preserves these fields verbatim from the upstream pipeline result when present. In current PaddleOCR-VL versions these fields are typically absent; they are kept as a forward-compatibility slot in case future pipeline versions emit additional metadata.

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

## Known Limitations

### In-Flight Task Cancellation Not Supported

Once a `POST /layout-parsing` request is accepted and the underlying
`PaddleOCRVL.predict()` call has started, **the task cannot be cancelled**,
even if the client disconnects or closes the HTTP connection early.

If the request is still queued behind another parse job, the daemon can cancel
it before it enters `PaddleOCRVL.predict()`.

This is a fundamental limitation of the current PaddleOCR / PaddleX Python
API:

- `PaddleOCRVL.predict()` is a synchronous, blocking call with no exposed
cancellation mechanism.
- Internally it performs CPU-intensive layout detection and GPU-bound VLM
inference; neither stage accepts an external abort signal.
- The daemon's `ThreadPoolExecutor` can cancel queued work, but it does not
terminate an already-running thread.

Consequences:
- A disconnected client only stops server-side work if the request is still
queued; once prediction has started, resources (CPU, GPU memory, VLM backend
slots) remain occupied until the pipeline finishes or hits `PREDICT_TIMEOUT`
(default 600 s).
- Retries from the client can stack up and worsen resource contention.

## Future Roadmap

Two complementary directions are under consideration to mitigate the
above limitation.

### 1. Process-Per-Request with External Kill

Replace the single-thread executor with lightweight worker **processes**
(e.g. `multiprocessing.Process`).  If the client connection drops, the
daemon can send `SIGTERM` (or `SIGKILL`) to the specific worker process,
forcibly reclaiming resources.  This is the only robust way to cancel an
in-flight PaddleOCR task without upstream library changes.

Trade-offs:
- Higher per-request overhead (process spawn / model re-initialisation
unless a pre-fork pool is used).
- Requires careful handling of shared state and temporary files.

### 2. Result Caching via `OUTPUT_DIR`

Use `OUTPUT_DIR` as a persistent cache layer:

1. Hash the incoming file payload (e.g. SHA-256 of the base64-decoded
bytes).
2. Check whether a folder named after that hash already exists under
`OUTPUT_DIR`.
   - **Hit**: Return the previously computed result directly without
calling `pipeline.predict()`.
   - **Miss**: Run the pipeline, persist the result under the hash-named
folder, and return it.

Benefits:
- Eliminates redundant work for identical files.
- Reduces the *effective* time a client must wait, lowering the chance
of timeouts and disconnections.
- Aligns with the existing forward-looking `OUTPUT_DIR` contract.

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
  - `imgs/`
  - `result.json`
  - `timing.txt`

Smoke bundle behavior:

- The API response is page-oriented. `client.py` performs the local export-time merge into a single `content.md` file.
- `client.py` inserts `<!-- page N -->` markers while merging pages so the bundle keeps page boundaries visible.
- `content.md` is the cleaned export artifact from per-page `markdown.text` content.
- `imgs/` is the current default bundle layout used by `client.py` so local files match upstream markdown image references such as `imgs/...`.
- The `imgs/` directory is a smoke-export convention, not an HTTP API requirement. SDK or application code may choose a different layout by rewriting markdown references and materializing images accordingly.
- `result.json` keeps the raw API response for debugging and comparison against the cleaned markdown export.
- Each smoke run deletes and recreates the target bundle directory so stale files do not linger between runs.
