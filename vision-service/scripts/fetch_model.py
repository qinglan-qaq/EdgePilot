#!/usr/bin/env python3
"""Fetch the YOLO11n weights used by the EdgePilot vision service.

The download URL is pinned to the official Ultralytics release assets so the
weights are reproducible and auditable. The file is downloaded to a temporary
``.part`` sibling first and only moved into place after its size and zip
checkpoint magic have been verified. The script prints the file size and
SHA-256 of the resulting weights; it never claims success for a failed or
partial download.

Usage:
    python scripts/fetch_model.py [--force] [--dest-dir DIR]

Exit codes:
    0  weights already valid (skipped) or freshly downloaded
    1  an existing destination file is invalid (rerun with --force)
    2  download / verification failure
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

MODEL_FILENAME = "yolo11n.pt"
# Official Ultralytics release asset (AGPL-3.0).
MODEL_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt"
SERVICE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST_DIR = SERVICE_ROOT / "weights"
DOWNLOAD_TIMEOUT_S = 180
CHUNK_SIZE = 1024 * 1024
MIN_VALID_BYTES = 1_000_000  # yolo11n.pt is ~5.4MB; anything tiny is an error page
ZIP_MAGIC = b"PK\x03\x04"  # torch checkpoints are zip archives
USER_AGENT = "edgepilot-vision-fetch-model/1.0"


class FetchError(RuntimeError):
    """Raised when weights cannot be downloaded or verified."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_zip_checkpoint(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == ZIP_MAGIC
    except OSError:
        return False


def describe(path: Path) -> None:
    size = path.stat().st_size
    print(f"weights: {path}")
    print(f"size:    {size} bytes ({size / (1024 * 1024):.2f} MiB)")
    print(f"sha256:  {sha256_file(path)}")


def validate_existing(path: Path) -> bool:
    """Return True when the existing file looks like a complete checkpoint."""

    if not path.is_file():
        return False
    size = path.stat().st_size
    if size < MIN_VALID_BYTES or not is_zip_checkpoint(path):
        return False
    return True


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"url:     {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    temp_name: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise FetchError(f"unexpected HTTP status {status} for {url}")
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, suffix=".part", delete=False
            ) as temp:
                temp_name = temp.name
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    temp.write(chunk)
        temp_path = Path(temp_name)
        size = temp_path.stat().st_size
        if size < MIN_VALID_BYTES:
            raise FetchError(
                f"downloaded file is too small ({size} bytes); "
                f"likely an error page instead of the checkpoint"
            )
        if not is_zip_checkpoint(temp_path):
            raise FetchError(
                "downloaded file is not a zip-based torch checkpoint "
                f"(first bytes: {temp_path.read_bytes()[:4]!r})"
            )
        os.replace(temp_path, destination)
        temp_name = None
    except FetchError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise FetchError(f"download failed from {url}: {exc!r}") from exc
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download yolo11n.pt weights")
    parser.add_argument(
        "--dest-dir",
        type=Path,
        default=DEFAULT_DEST_DIR,
        help=f"destination directory (default: {DEFAULT_DEST_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even when a valid weights file already exists",
    )
    args = parser.parse_args(argv)

    destination = Path(args.dest_dir) / MODEL_FILENAME
    if destination.is_file() and not args.force:
        if validate_existing(destination):
            print("weights already present and valid; skipping download")
            describe(destination)
            return 0
        print(
            f"error: existing file is not a valid checkpoint: {destination}; "
            "rerun with --force to replace it",
            file=sys.stderr,
        )
        return 1

    try:
        download(MODEL_URL, destination)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    describe(destination)
    print("license: AGPL-3.0 (https://www.ultralytics.com/license)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
