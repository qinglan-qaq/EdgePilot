"""下载 OpenCV 官方演示视频 ``vtest.avi``，供 Vision Center 真实推理演示。

从仓库任意目录运行：

    python backend/scripts/fetch_vision_sample.py
    python backend/scripts/fetch_vision_sample.py --dest D:/tmp/vtest.avi --force

默认保存到 ``vision-service/var/samples/vtest.avi``（``var/`` 已在 .gitignore
中，二进制不会进入 git）。下载先写同目录 ``.part`` 临时文件，校验非空、AVI
魔数（``RIFF`` + 偏移 8 的 ``AVI ``）与最小体积后才原子替换目标文件。

退出码：

* 0 下载成功，或已有文件校验通过（skipped）；
* 1 已存在文件无效（用 ``--force`` 重新下载）；
* 2 下载或校验失败（绝不谎报成功）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEST = REPO_ROOT / "vision-service" / "var" / "samples" / "vtest.avi"
SAMPLE_URL = "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/vtest.avi"
DOWNLOAD_TIMEOUT_S = 180
CHUNK_SIZE = 1024 * 1024
# vtest.avi 是数 MB 的真实视频；小于该阈值基本是 HTML 错误页或截断文件。
MIN_VALID_BYTES = 256 * 1024
USER_AGENT = "edgepilot-fetch-vision-sample/1.0"


class FetchError(RuntimeError):
    """下载或校验失败；调用方据此返回退出码 2。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_video(path: Path) -> tuple[bool, str]:
    """校验体积与 AVI 魔数；返回 (是否有效, 原因)。"""
    if not path.is_file():
        return False, "文件不存在"
    size = path.stat().st_size
    if size <= 0:
        return False, "文件为空"
    if size < MIN_VALID_BYTES:
        return False, f"文件过小（{size} bytes < {MIN_VALID_BYTES}）"
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError as exc:
        return False, f"无法读取文件头: {exc}"
    if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"AVI ":
        return False, f"不是 AVI 容器（文件头: {header[:12]!r}）"
    return True, "ok"


def describe(path: Path) -> None:
    size = path.stat().st_size
    print(f"dest:    {path}")
    print(f"size:    {size} bytes ({size / (1024 * 1024):.2f} MiB)")
    print(f"sha256:  {sha256_file(path)}")


def download(url: str, destination: Path) -> None:
    """流式下载到 ``.part`` 并在校验通过后原子替换目标文件。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    print(f"url:     {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        try:
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise FetchError(f"unexpected HTTP status {status} for {url}")
                with part.open("wb") as handle:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        handle.write(chunk)
        except FetchError:
            raise
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            raise FetchError(f"download failed from {url}: {exc!r}") from exc

        valid, reason = validate_video(part)
        if not valid:
            raise FetchError(f"downloaded file failed verification: {reason}")
        os.replace(part, destination)
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 OpenCV vtest.avi 演示视频")
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"目标文件路径（默认: {DEFAULT_DEST}）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使已有有效文件也重新下载",
    )
    args = parser.parse_args(argv)

    destination = args.dest
    if not destination.is_absolute():
        destination = (REPO_ROOT / destination).resolve()

    if destination.exists() and not args.force:
        valid, reason = validate_video(destination)
        if valid:
            print(f"url:     {SAMPLE_URL}")
            describe(destination)
            print("skipped: 已有有效文件，未重新下载（--force 可覆盖）")
            return 0
        print(
            f"error: 已存在文件无效（{reason}）: {destination}；"
            "请用 --force 重新下载",
            file=sys.stderr,
        )
        return 1

    try:
        download(SAMPLE_URL, destination)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    describe(destination)
    print("ok: 下载并通过 AVI 校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
