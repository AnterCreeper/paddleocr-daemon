# smoke

This directory contains local smoke-test tooling for `paddleocr-daemon`.

Contents:

- `setup.sh`: creates `.venv` and installs the Python dependencies needed by the smoke tools
- `client.py`: minimal local client that calls `paddleocr-daemon`, writes `content.md`, `images/`, `result.json`, and `timing.txt`
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

## Output bundle

Each run writes a directory containing:

- `content.md`: merged markdown content
- `images/`: extracted markdown image assets referenced by `content.md`
- `result.json`: raw API response for debugging
- `timing.txt`: total request duration in seconds
