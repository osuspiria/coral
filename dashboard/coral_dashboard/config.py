"""Dashboard settings, read from environment variables (optionally a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing variables."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class VideoProfile:
    """Accepted media profile (spec §4.5). Tune after tests on the real screen."""

    image_formats: frozenset[str] = frozenset({"mjpeg", "png", "webp"})
    image_max_px: int = 4096
    video_codecs: frozenset[str] = frozenset({"h264", "hevc"})
    video_containers: frozenset[str] = frozenset({"mov", "mp4", "matroska"})
    video_max_width: int = 1920
    video_max_height: int = 1080
    video_max_fps: float = 30.0
    video_max_bitrate_kbps: int = 20000


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secret_key: str
    admin_password_hash: str
    cookie_secure: bool = True
    timezone: str = "Europe/Lisbon"
    default_image_duration_s: int = 10
    min_free_mb: int = 2048
    max_upload_mb: int = 2048
    media_grace_days: int = 7
    session_hours: int = 12
    heartbeat_interval_s: int = 60
    ffprobe_path: str = "ffprobe"
    profile: VideoProfile = field(default_factory=VideoProfile)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "coral.sqlite3"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "media" / ".incoming"

    def validate(self) -> None:
        if len(self.secret_key) < 32:
            raise ValueError("CORAL_SECRET_KEY must be at least 32 characters")
        if not self.admin_password_hash.startswith("$argon2id$"):
            raise ValueError("CORAL_ADMIN_PASSWORD_HASH must be an argon2id hash")
        ZoneInfo(self.timezone)
        if self.default_image_duration_s <= 0:
            raise ValueError("CORAL_DEFAULT_IMAGE_DURATION_S must be positive")


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def settings_from_env(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    defaults = VideoProfile()
    profile = VideoProfile(
        image_max_px=int(env.get("CORAL_IMAGE_MAX_PX", defaults.image_max_px)),
        video_max_width=int(env.get("CORAL_VIDEO_MAX_WIDTH", defaults.video_max_width)),
        video_max_height=int(env.get("CORAL_VIDEO_MAX_HEIGHT", defaults.video_max_height)),
        video_max_fps=float(env.get("CORAL_VIDEO_MAX_FPS", defaults.video_max_fps)),
        video_max_bitrate_kbps=int(
            env.get("CORAL_VIDEO_MAX_BITRATE_KBPS", defaults.video_max_bitrate_kbps)
        ),
    )
    settings = Settings(
        data_dir=Path(env.get("CORAL_DATA_DIR", "./data")).resolve(),
        secret_key=env.get("CORAL_SECRET_KEY", ""),
        admin_password_hash=env.get("CORAL_ADMIN_PASSWORD_HASH", ""),
        cookie_secure=_bool(env.get("CORAL_COOKIE_SECURE", "true")),
        timezone=env.get("CORAL_TIMEZONE", "Europe/Lisbon"),
        default_image_duration_s=int(env.get("CORAL_DEFAULT_IMAGE_DURATION_S", "10")),
        min_free_mb=int(env.get("CORAL_MIN_FREE_MB", "2048")),
        max_upload_mb=int(env.get("CORAL_MAX_UPLOAD_MB", "2048")),
        media_grace_days=int(env.get("CORAL_MEDIA_GRACE_DAYS", "7")),
        ffprobe_path=env.get("CORAL_FFPROBE", "ffprobe"),
        # Spec 5.1: heartbeat every 30-60 s.
        heartbeat_interval_s=min(60, max(30, int(env.get("CORAL_HEARTBEAT_INTERVAL_S", "60")))),
        profile=profile,
    )
    settings.validate()
    return settings
