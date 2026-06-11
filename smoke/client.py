#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import time
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import requests

DEFAULT_CLOUD_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_CLOUD_MODEL = "PaddleOCR-VL-1.6"


def safe_relative_image_path(name: str, index: int) -> Path:
    raw = (name or "").strip().replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in {"", ".", ".."}]
    if not parts:
        parts = [f"image_{index}.bin"]
    if parts[0] == "imgs":
        parts = parts[1:] or [f"image_{index}.bin"]
    return Path(*parts)


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _read_image_bytes(value: Any, timeout: int) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None

    if _is_url(value):
        response = requests.get(value, timeout=timeout)
        response.raise_for_status()
        return response.content

    return base64.b64decode(value)


def write_image_file(
    base_dir: Path,
    relative_name: str,
    image_data: Any,
    index: int,
    timeout: int,
) -> str | None:
    raw = _read_image_bytes(image_data, timeout)
    if raw is None:
        return None

    rel_path = safe_relative_image_path(relative_name, index)
    out_path = base_dir / rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return rel_path.as_posix()


def _extract_pages(
    pages: list[dict[str, Any]],
    output_dir: Path,
    timeout: int,
) -> tuple[str, int]:
    content_parts: list[str] = []
    image_counter = 0
    images_dir = output_dir / "imgs"

    for page_index, page in enumerate(pages, start=1):
        markdown = page.get("markdown", {})
        text = markdown.get("text", "") or ""
        images: dict[str, Any] = markdown.get("images", {}) or {}

        content_parts.append(f"<!-- page {page_index} -->")
        if text:
            content_parts.append(text)

        for original_name, encoded in images.items():
            image_counter += 1
            write_image_file(images_dir, str(original_name), encoded, image_counter, timeout)

        output_images = page.get("outputImages", {}) or {}
        if isinstance(output_images, dict):
            for original_name, image_data in output_images.items():
                image_counter += 1
                name = f"output/{page_index}_{original_name}.jpg"
                write_image_file(images_dir, name, image_data, image_counter, timeout)

    return "\n\n".join(content_parts).strip() + "\n", image_counter


def _write_bundle(
    output_dir: Path,
    result: dict[str, Any],
    elapsed: float,
    timeout: int,
) -> tuple[int, int]:
    pages = result.get("result", {}).get("layoutParsingResults", [])
    content_md, image_count = _extract_pages(pages, output_dir, timeout)

    (output_dir / "content.md").write_text(content_md, encoding="utf-8")
    (output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "timing.txt").write_text(f"{elapsed:.3f}\n", encoding="utf-8")
    return len(pages), image_count


def _auth_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    return os.environ.get("PADDLEOCR_TOKEN", "")


def _run_daemon(args: argparse.Namespace) -> tuple[dict[str, Any], float]:
    pdf_path = Path(args.pdf_path)
    payload = {
        "file": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
        "fileType": 0,
    }

    headers = {"Content-Type": "application/json"}
    token = _auth_token(args)
    if token:
        headers["Authorization"] = f"bearer {token}"

    health = requests.get(f"{args.base_url}/health", timeout=30)
    print("HEALTH", health.status_code, health.text)
    health.raise_for_status()

    start = time.time()
    response = requests.post(
        f"{args.base_url}/layout-parsing",
        headers=headers,
        json=payload,
        timeout=args.timeout,
    )
    elapsed = time.time() - start
    print("PARSE", response.status_code)
    print(response.text)
    response.raise_for_status()
    return response.json(), elapsed


def _cloud_headers(args: argparse.Namespace) -> dict[str, str]:
    token = _auth_token(args)
    if not token:
        raise ValueError("missing cloud token; set --token or PADDLEOCR_TOKEN")
    return {"Authorization": f"bearer {token}"}


def _submit_cloud_job(args: argparse.Namespace, headers: dict[str, str]) -> str:
    optional_payload = {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }

    print(f"Processing file with cloud API: {args.pdf_path}")
    if _is_url(args.pdf_path):
        payload = {
            "fileUrl": args.pdf_path,
            "model": args.cloud_model,
            "optionalPayload": optional_payload,
        }
        job_response = requests.post(
            args.cloud_job_url,
            json=payload,
            headers={**headers, "Content-Type": "application/json"},
            timeout=args.timeout,
        )
    else:
        pdf_path = Path(args.pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"file not found: {pdf_path}")

        data = {
            "model": args.cloud_model,
            "optionalPayload": json.dumps(optional_payload),
        }
        with pdf_path.open("rb") as f:
            job_response = requests.post(
                args.cloud_job_url,
                headers=headers,
                data=data,
                files={"file": f},
                timeout=args.timeout,
            )

    print("JOB", job_response.status_code, job_response.text)
    job_response.raise_for_status()
    payload = job_response.json()
    return str(payload["data"]["jobId"])


def _poll_cloud_job(args: argparse.Namespace, headers: dict[str, str], job_id: str) -> str:
    while True:
        response = requests.get(
            f"{args.cloud_job_url.rstrip('/')}/{job_id}",
            headers=headers,
            timeout=args.timeout,
        )
        print("POLL", response.status_code, response.text)
        response.raise_for_status()
        data = response.json()["data"]
        state = data["state"]

        if state == "done":
            return data["resultUrl"]["jsonUrl"]
        if state == "failed":
            raise RuntimeError(f"cloud job failed: {data.get('errorMsg', 'unknown error')}")

        time.sleep(args.poll_interval)


def _load_cloud_jsonl(jsonl_url: str, timeout: int) -> dict[str, Any]:
    response = requests.get(jsonl_url, timeout=timeout)
    response.raise_for_status()

    merged_pages: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        records.append(record)
        pages = record.get("result", {}).get("layoutParsingResults", [])
        merged_pages.extend(pages)

    return {
        "logId": records[0].get("logId", "") if records else "",
        "errorCode": 0,
        "errorMsg": "Success",
        "result": {"layoutParsingResults": merged_pages},
        "jsonlRecords": records,
    }


def _run_cloud(args: argparse.Namespace) -> tuple[dict[str, Any], float]:
    headers = _cloud_headers(args)
    start = time.time()
    job_id = _submit_cloud_job(args, headers)
    print(f"Job submitted successfully. job id: {job_id}")
    jsonl_url = _poll_cloud_job(args, headers, job_id)
    result = _load_cloud_jsonl(jsonl_url, args.timeout)
    return result, time.time() - start


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path")
    parser.add_argument("output_dir")
    parser.add_argument("--backend", choices=("daemon", "cloud"), default="daemon")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default="")
    parser.add_argument("--cloud-job-url", default=DEFAULT_CLOUD_JOB_URL)
    parser.add_argument("--cloud-model", default=DEFAULT_CLOUD_MODEL)
    parser.add_argument("--poll-interval", type=int, default=5)
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("PREDICT_TIMEOUT", "600")),
        help="request timeout in seconds; defaults to PREDICT_TIMEOUT",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.backend == "cloud":
        result, elapsed = _run_cloud(args)
    else:
        result, elapsed = _run_daemon(args)
    page_count, image_count = _write_bundle(output_dir, result, elapsed, args.timeout)

    print("SUMMARY", json.dumps({
        "output_dir": str(output_dir),
        "backend": args.backend,
        "pages": page_count,
        "images": image_count,
        "elapsed_seconds": round(elapsed, 3),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
