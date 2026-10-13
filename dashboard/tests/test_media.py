from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time

import pytest
from coral_dashboard import media
from coral_dashboard.config import VideoProfile
from dash_helpers import login, make_settings

PROFILE = VideoProfile()


def image_probe(codec="png", width=1920, height=1080, fmt="png_pipe"):
    return {
        "format": {"format_name": fmt},
        "streams": [{"codec_type": "video", "codec_name": codec, "width": width, "height": height}],
    }


def video_probe(
    codec="h264",
    width=1920,
    height=1080,
    fps="25/1",
    bit_rate="8000000",
    fmt="mov,mp4,m4a,3gp,3g2,mj2",
    brand="isom",
    duration="12.5",
):
    return {
        "format": {
            "format_name": fmt,
            "bit_rate": bit_rate,
            "duration": duration,
            "tags": {"major_brand": brand},
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": width,
                "height": height,
                "avg_frame_rate": fps,
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }


# ------------------------------------------------------------ classification


def test_accepts_profile_images():
    assert media.classify(image_probe(), PROFILE).ext == "png"
    jpeg = media.classify(image_probe("mjpeg", fmt="image2"), PROFILE)
    assert (jpeg.kind, jpeg.ext) == ("image", "jpg")
    assert media.classify(image_probe("webp", fmt="webp_pipe"), PROFILE).ext == "webp"


def test_rejects_large_or_unknown_images():
    with pytest.raises(media.Rejected, match="4096"):
        media.classify(image_probe(width=5000), PROFILE)
    with pytest.raises(media.Rejected, match="image format"):
        media.classify(image_probe("bmp", fmt="image2"), PROFILE)


def test_accepts_profile_videos():
    info = media.classify(video_probe(), PROFILE)
    assert (info.kind, info.ext, info.codec, info.duration_s) == ("video", "mp4", "h264", 12.5)
    assert media.classify(video_probe(codec="hevc", brand="qt"), PROFILE).ext == "mov"
    assert media.classify(video_probe(fmt="matroska,webm"), PROFILE).ext == "mkv"
    assert media.classify(video_probe(fps="30000/1001"), PROFILE)


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        (video_probe(codec="vp9"), "codec"),
        (video_probe(width=3840, height=2160), "3840x2160"),
        (video_probe(fps="60/1"), "fps"),
        (video_probe(bit_rate="50000000"), "Mb/s"),
        (video_probe(fmt="avi"), "container"),
        (video_probe(duration="0"), "duration"),
        ({"format": {}, "streams": [{"codec_type": "audio"}]}, "no image or video"),
    ],
)
def test_rejects_incompatible_videos(probe, message):
    with pytest.raises(media.Rejected, match=message):
        media.classify(probe, PROFILE)


# ------------------------------------------------------------ upload endpoint


@pytest.fixture
def fake_ffprobe(monkeypatch):
    result = {"probe": image_probe()}
    monkeypatch.setattr(media, "run_ffprobe", lambda path, ffprobe: result["probe"])
    return result


def upload(client, csrf, data: bytes, name="pic.png"):
    return client.put(
        f"/media/upload?filename={name}",
        content=data,
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/octet-stream"},
    )


def test_upload_stores_sha_named_file(client, settings, app_store, fake_ffprobe):
    csrf = login(client)
    data = b"fake png bytes" * 100
    response = upload(client, csrf, data, name="../../evil.png")
    assert response.status_code == 200, response.text
    sha = hashlib.sha256(data).hexdigest()
    assert response.json() == {"id": 1, "sha256": sha, "duplicate": False}
    assert (settings.media_dir / f"{sha}.png").read_bytes() == data
    row = app_store.get_media_by_sha(sha)
    assert row["original_name"] == "evil.png"
    assert row["size"] == len(data)
    assert list(settings.incoming_dir.iterdir()) == []

    again = upload(client, csrf, data)
    assert again.json()["duplicate"] is True


def test_upload_requires_csrf(client, fake_ffprobe):
    login(client)
    response = client.put("/media/upload?filename=a.png", content=b"x")
    assert response.status_code == 403


def test_rejected_upload_leaves_nothing(client, settings, app_store, fake_ffprobe):
    fake_ffprobe["probe"] = video_probe(codec="vp9")
    csrf = login(client)
    response = upload(client, csrf, b"video bytes", name="clip.webm")
    assert response.status_code == 422
    assert "H.264" in response.json()["detail"]
    assert app_store.list_media() == []
    assert [p.name for p in settings.media_dir.iterdir()] == [".incoming"]
    assert list(settings.incoming_dir.iterdir()) == []


def test_upload_refused_when_disk_is_low(tmp_path, fake_ffprobe):
    from coral_dashboard.main import create_app
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path / "data", min_free_mb=10**9)
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        response = upload(client, csrf, b"data")
    assert response.status_code == 422
    assert "disk space" in response.json()["detail"]


def test_upload_size_limit(tmp_path, fake_ffprobe):
    from coral_dashboard.main import create_app
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path / "data", max_upload_mb=1)
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        response = upload(client, csrf, b"x" * (1024 * 1024 + 1))
    assert response.status_code == 422
    assert list(settings.incoming_dir.iterdir()) == []


def test_delete_and_collect(client, settings, app_store, fake_ffprobe):
    csrf = login(client)
    data = b"some image"
    media_id = upload(client, csrf, data).json()["id"]
    path = settings.media_dir / f"{hashlib.sha256(data).hexdigest()}.png"

    response = client.post(f"/media/{media_id}/delete", data={"csrf_token": csrf})
    assert response.status_code == 200
    assert app_store.list_media() == []
    assert media.collect_garbage(app_store, settings) == 0  # grace period
    assert path.exists()

    app_store.conn.execute("UPDATE media SET deleted_at = '2000-01-01T00:00:00+00:00'")
    assert media.collect_garbage(app_store, settings) == 1
    assert not path.exists()


def test_collect_removes_only_old_partial_uploads(settings, app_store):
    old = settings.incoming_dir / "old.part"
    new = settings.incoming_dir / "new.part"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    media.collect_garbage(app_store, settings)
    assert not old.exists()
    assert new.exists()


# ------------------------------------------------------------ real ffprobe

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffprobe") and shutil.which("ffmpeg")), reason="ffmpeg/ffprobe not installed"
)


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=120)


@needs_ffmpeg
def test_real_ffprobe_accepts_png_and_h264(tmp_path):
    png = tmp_path / "a.png"
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=320x240", "-frames:v", "1", str(png))
    assert media.classify(media.run_ffprobe(png, "ffprobe"), PROFILE).ext == "png"

    mp4 = tmp_path / "a.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=s=640x360:r=25:d=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(mp4),
    )
    info = media.classify(media.run_ffprobe(mp4, "ffprobe"), PROFILE)
    assert (info.kind, info.codec, info.ext) == ("video", "h264", "mp4")


@needs_ffmpeg
def test_real_ffprobe_rejects_incompatible_video(tmp_path):
    clip = tmp_path / "a.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=s=640x360:r=60:d=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(clip),
    )
    with pytest.raises(media.Rejected, match="fps"):
        media.classify(media.run_ffprobe(clip, "ffprobe"), PROFILE)

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all")
    with pytest.raises(media.Rejected):
        media.classify(media.run_ffprobe(junk, "ffprobe"), PROFILE)
