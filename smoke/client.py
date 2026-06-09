#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import requests


def safe_image_name(name: str, index: int) -> str:
    name = name.strip().replace("\\", "/")
    name = name.split("/")[-1] or f"image_{index}.bin"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or f"image_{index}.bin"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path")
    parser.add_argument("output_dir")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    output_dir = Path(args.output_dir)
    images_dir = output_dir / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    pdf_bytes = pdf_path.read_bytes()
    payload = {
        "file": base64.b64encode(pdf_bytes).decode("ascii"),
        "fileType": 0,
    }

    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    health = requests.get(f"{args.base_url}/health", timeout=10)
    print("HEALTH", health.status_code, health.text)
    health.raise_for_status()

    ready = requests.get(f"{args.base_url}/health/ready", timeout=30)
    print("READY", ready.status_code, ready.text)
    ready.raise_for_status()

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
    content_parts: list[str] = []
    image_counter = 0

    for page_index, page in enumerate(pages, start=1):
        markdown = page.get("markdown", {})
        text = markdown.get("text", "") or ""
        images: dict[str, Any] = markdown.get("images", {}) or {}

        content_parts.append(f"<!-- page {page_index} -->")
        if text:
            content_parts.append(text)

        for original_name, encoded in images.items():
            image_counter += 1
            file_name = safe_image_name(str(original_name), image_counter)
            file_path = images_dir / file_name
            file_path.write_bytes(base64.b64decode(encoded))
            content_parts.append(f"\n![{file_name}](images/{file_name})\n")

    (output_dir / "content.md").write_text("\n\n".join(content_parts).strip() + "\n", encoding="utf-8")
    (output_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    (output_dir / "timing.txt").write_text(f"{elapsed:.3f}\n", encoding="utf-8")

    print(
        "SUMMARY",
        json.dumps(
            {
                "output_dir": str(output_dir),
                "pages": len(pages),
                "images": image_counter,
                "elapsed_seconds": round(elapsed, 3),
            },
            ensure_ascii=False,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
