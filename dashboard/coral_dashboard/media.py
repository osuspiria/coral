"""Media ingestion: streaming upload to disk, ffprobe validation, garbage collection."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .config import Settings, VideoProfile
from .repo import Store

log = logging.getLogger(__name__)

_IMAGE_FORMATS = {"image2", "png_pipe", "jpeg_pipe", "webp_pipe"}
_IMAGE_EXT = {"mjpeg": "jpg", "png": "png", "webp": "webp"}
_CHECK_SPACE_EVERY = 16 * 1024 * 1024


class Rejected(Exception):
    """The upload is not acceptable; the message is shown to the admin."""


@dataclass(frozen=True)
class MediaInfo:
    kind: str
    ext: str
    codec: str
    width: int
    height: int
    duration_s: float | None


def run_ffprobe(path: Path, ffprobe: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"ffprobe not found at {ffprobe!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise Rejected("The file could not be analysed (ffprobe timed out).") from exc
    if result.returncode != 0:
        raise Rejected("The file is not a readable image or video.")
    return json.loads(result.stdout or b"{}")


def _fps(stream: dict[str, Any]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if value and value != "0/0":
            try:
                return float(Fraction(value))
            except (ValueError, ZeroDivisionError):
                continue
    return None


def classify(probe: dict[str, Any], profile: VideoProfile) -> MediaInfo:
    """Check an ffprobe result against the accepted profile (spec §4.5)."""
    fmt = probe.get("format") or {}
    format_names = set(str(fmt.get("format_name", "")).split(","))
    streams = probe.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        raise Rejected("The file has no image or video stream.")
    stream = video[0]
    codec = str(stream.get("codec_name", ""))
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise Rejected("Could not read the image/video dimensions.")

    if format_names & _IMAGE_FORMATS:
        if codec not in profile.image_formats:
            raise Rejected(f"Unsupported image format ({codec}). Use JPEG, PNG or WebP.")
        if max(width, height) > profile.image_max_px:
            raise Rejected(
                f"Image is {width}x{height}; the maximum is {profile.image_max_px} px per side."
                " Resize it before uploading."
            )
        return MediaInfo("image", _IMAGE_EXT[codec], codec, width, height, None)

    if format_names & {"mov", "mp4"}:
        brand = str((fmt.get("tags") or {}).get("major_brand", "")).strip()
        ext = "mov" if brand == "qt" else "mp4"
    elif "matroska" in format_names:
        ext = "mkv"
    else:
        raise Rejected("Unsupported video container. Use MP4, MOV or MKV.")
    if codec not in profile.video_codecs:
        raise Rejected(f"Unsupported video codec ({codec}). Use H.264 or HEVC (H.265).")
    if width > profile.video_max_width or height > profile.video_max_height:
        raise Rejected(
            f"Video is {width}x{height}; the maximum is "
            f"{profile.video_max_width}x{profile.video_max_height}."
        )
    fps = _fps(stream)
    if fps is not None and fps > profile.video_max_fps + 0.01:
        raise Rejected(f"Video is {fps:.2f} fps; the maximum is {profile.video_max_fps:g} fps.")
    bit_rate = fmt.get("bit_rate") or stream.get("bit_rate")
    if bit_rate is not None:
        kbps = int(bit_rate) / 1000
        if kbps > profile.video_max_bitrate_kbps:
            raise Rejected(
                f"Video bitrate is {kbps / 1000:.1f} Mb/s; the maximum is "
                f"{profile.video_max_bitrate_kbps / 1000:g} Mb/s."
            )
    try:
        duration = float(fmt.get("duration") or stream.get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise Rejected("Could not read the video duration.")
    return MediaInfo("video", ext, codec, width, height, round(duration, 3))


def free_mb(path: Path) -> int:
    return shutil.disk_usage(path).free // (1024 * 1024)


async def receive_upload(chunks: AsyncIterator[bytes], settings: Settings) -> tuple[Path, str, int]:
    """Stream the body to media/.incoming while hashing. Returns (tmp, sha256, size)."""
    if free_mb(settings.media_dir) < settings.min_free_mb:
        raise Rejected("Not enough free disk space on the server for uploads.")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    tmp = settings.incoming_dir / f"{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    next_space_check = _CHECK_SPACE_EVERY
    try:
        with tmp.open("wb") as fh:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise Rejected(f"File is larger than {settings.max_upload_mb} MB.")
                if size >= next_space_check:
                    next_space_check += _CHECK_SPACE_EVERY
                    if free_mb(settings.media_dir) < settings.min_free_mb:
                        raise Rejected("Server ran out of free disk space during the upload.")
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise Rejected("The file is empty.")
    return tmp, digest.hexdigest(), size


def ingest(
    store: Store, settings: Settings, tmp: Path, sha256: str, size: int, original_name: str
) -> tuple[int, bool]:
    """Validate an uploaded temp file and store it as <sha256>.<ext>.

    Returns (media_id, duplicate). The temp file is always consumed.
    """
    try:
        existing = store.get_media_by_sha(sha256)
        if existing is not None and (settings.media_dir / existing["stored_name"]).is_file():
            media_id = store.add_media(**_row_fields(existing))  # revives if soft-deleted
            return media_id, True
        info = classify(run_ffprobe(tmp, settings.ffprobe_path), settings.profile)
        stored_name = f"{sha256}.{info.ext}"
        os.replace(tmp, settings.media_dir / stored_name)
    finally:
        tmp.unlink(missing_ok=True)
    media_id = store.add_media(
        sha256=sha256,
        stored_name=stored_name,
        original_name=_clean_original_name(original_name),
        kind=info.kind,
        size=size,
        duration_s=info.duration_s,
        codec=info.codec,
        width=info.width,
        height=info.height,
    )
    return media_id, False


def _row_fields(row: Any) -> dict[str, Any]:
    keys = (
        "sha256",
        "stored_name",
        "original_name",
        "kind",
        "size",
        "duration_s",
        "codec",
        "width",
        "height",
    )
    return {k: row[k] for k in keys}


def _clean_original_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip()
    return name[:200] or "upload"


def collect_garbage(store: Store, settings: Settings) -> int:
    """Remove files of media deleted longer than the grace period and unused."""
    removed = 0
    for row in store.media_to_collect(settings.media_grace_days):
        if store.purge_media_row(row["id"]):
            (settings.media_dir / row["stored_name"]).unlink(missing_ok=True)
            removed += 1
            log.info("Removed media file %s", row["stored_name"])
    # Leftovers of interrupted uploads (older than a day, so never an active one).
    cutoff = time.time() - 86400
    for stale in settings.incoming_dir.glob("*.part"):
        if stale.stat().st_mtime < cutoff:
            stale.unlink(missing_ok=True)
    return removed
