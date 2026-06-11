#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

import requests


def safe_relative_image_path(name: str, index: int) -> Path:
    raw = (name or "").strip().replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in {"", ".", ".."}]
    if not parts:
        parts = [f"image_{index}.bin"]
    if parts[0] == "imgs":
        parts = parts[1:] or [f"image_{index}.bin"]
    return Path(*parts)


def write_image_file(base_dir: Path, relative_name: str, encoded: Any, index: int) -> str | None:
    if not isinstance(encoded, str) or not encoded:
        return None

    rel_path = safe_relative_image_path(relative_name, index)
    out_path = base_dir / rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(encoded))
    return rel_path.as_posix()


def _extract_pages(pages: list[dict[str, Any]], output_dir: Path) -> tuple[str, int]:
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
            write_image_file(images_dir, str(original_name), encoded, image_counter)

    return "\n\n".join(content_parts).strip() + "\n", image_counter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path")
    parser.add_argument("output_dir")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    output_dir = Path(args.output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "file": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
        "fileType": 0,
    }

    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    health = requests.get(f"{args.base_url}/health", timeout=30)
    print("HEALTH", health.status_code, health.text)
    health.raise_for_status()

    start = time.time()
    response = requests.post(
        f"{args.base_url}/layout-parsing",
        headers=headers,
        json=payload,
        timeout=5400,
    )
    elapsed = time.time() - start
    print("PARSE", response.status_code)
    print(response.text)
    response.raise_for_status()
    result = response.json()

    pages = result.get("result", {}).get("layoutParsingResults", [])
    content_md, image_count = _extract_pages(pages, output_dir)

    (output_dir / "content.md").write_text(content_md, encoding="utf-8")
    (output_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    (output_dir / "timing.txt").write_text(f"{elapsed:.3f}\n", encoding="utf-8")

    print("SUMMARY", json.dumps({
        "output_dir": str(output_dir),
        "pages": len(pages),
        "images": image_count,
        "elapsed_seconds": round(elapsed, 3),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
