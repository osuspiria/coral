"""Player configuration from config.toml (spec 4.2: /etc/signage/config.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_FALLBACK = PACKAGE_DIR.parent / "assets" / "fallback.png"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    server_url: str
    token_file: Path = Path("/etc/signage/token")
    data_dir: Path = Path("/var/lib/signage")
    mpv_ipc: str = "unix:/run/signage/mpv.sock"
    fallback_image: Path = DEFAULT_FALLBACK
    fallback_duration_s: int = 30
    poll_interval_s: float = 60
    full_check_interval_s: float = 900
    reevaluate_interval_s: float = 300
    backoff_min_s: float = 5
    backoff_max_s: float = 900
    http_timeout_s: float = 20
    min_free_mb: int = 1024
    cleanup_grace_days: float = 7
    max_deletions_per_run: int = 50
    max_clock_skew_s: float = 120
    log_level: str = "INFO"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "media" / ".staging"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    def read_token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"Cannot read token file {self.token_file}: {exc}") from exc
        if not token:
            raise ConfigError(f"Token file {self.token_file} is empty")
        return token


_PATH_FIELDS = {"token_file", "data_dir", "fallback_image"}


def validate_server_url(url: str) -> str:
    """Only HTTPS, except plain http to localhost (tests and local development)."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url.rstrip("/")
    if parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}:
        return url.rstrip("/")
    raise ConfigError(f"server_url must be https:// (got {url!r})")


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"Unknown config keys: {', '.join(sorted(unknown))}")
    if "server_url" not in raw:
        raise ConfigError("server_url is required")
    values = {k: Path(v) if k in _PATH_FIELDS else v for k, v in raw.items()}
    values["server_url"] = validate_server_url(str(values["server_url"]))
    return Config(**values)
