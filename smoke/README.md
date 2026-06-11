# smoke

This directory contains local smoke-test tooling for `paddleocr-daemon`.

Contents:

- `setup.sh`: creates `.venv` and installs the Python dependencies needed by the smoke tools
- `client.py`: minimal client for either local deployed `paddleocr-daemon` or the AIStudio
  cloud jobs API; writes `content.md`, `imgs/`, `result.json`, and `timing.txt`
- `test.sh`: thin shell wrapper around `client.py`
- `artifacts/`: local output directory for generated inputs and parsed bundles

## Setup

```bash
bash smoke/setup.sh
```

## Run

```bash
bash smoke/test.sh
```

The script will:

- create `smoke/artifacts/` if needed
- download `https://arxiv.org/pdf/1706.03762` to `smoke/artifacts/1706.03762.pdf`
- run `client.py`
- write the parsed bundle to `smoke/artifacts/1706.03762_bundle/`

Optional flags are passed through to `client.py`, for example:

```bash
bash smoke/test.sh --base-url http://127.0.0.1:8080
```

To call the AIStudio cloud API instead, provide a token via the environment or
`--token`:

```bash
PADDLEOCR_TOKEN=... bash smoke/test.sh --backend cloud
```

The same `--token` flag is used for daemon Bearer auth and cloud Bearer auth.
If omitted, both modes read `PADDLEOCR_TOKEN`.

Cloud mode submits a job, polls until it finishes, downloads the returned
JSONL result, merges its `layoutParsingResults`, and downloads URL-backed image
assets into the same bundle layout used by daemon mode.

## Output bundle

Each run writes a directory containing:

- `content.md`: merged markdown content
- `imgs/`: extracted markdown image assets, written to match the `imgs/...`
  relative paths referenced by `content.md`; cloud URL-backed use same path definition
- `result.json`: raw API response for debugging; this keeps the unmodified server response
- `timing.txt`: total request duration in seconds

Notes:

- The bundle directory is deleted and recreated on each run, so old image assets do not linger between smoke tests.
- `content.md` is the cleaned export artifact, while `result.json` is the source-of-truth record of the API response.
