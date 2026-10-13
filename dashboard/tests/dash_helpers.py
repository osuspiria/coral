from __future__ import annotations

from coral_dashboard.manifest import ManifestContext
from coral_dashboard.repo import Store

CTX = ManifestContext(timezone="Europe/Lisbon", default_image_duration_s=10)


def add_media(store: Store, n: int = 1, kind: str = "image", size: int = 100) -> int:
    sha = f"{n:064x}"
    ext = "png" if kind == "image" else "mp4"
    return store.add_media(
        sha256=sha,
        stored_name=f"{sha}.{ext}",
        original_name=f"file{n}.{ext}",
        kind=kind,
        size=size,
        duration_s=None if kind == "image" else 12.5,
        codec="png" if kind == "image" else "h264",
        width=1920,
        height=1080,
    )


ADMIN_PASSWORD = "correct horse battery staple"
_ADMIN_HASH: str | None = None


def admin_hash() -> str:
    global _ADMIN_HASH
    if _ADMIN_HASH is None:
        from coral_dashboard.auth import hash_password

        _ADMIN_HASH = hash_password(ADMIN_PASSWORD)
    return _ADMIN_HASH


def make_settings(data_dir, **overrides):
    from coral_dashboard.config import Settings

    fields = {
        "data_dir": data_dir,
        "secret_key": "s" * 40,
        "admin_password_hash": admin_hash(),
        "cookie_secure": False,
        "min_free_mb": 0,
    }
    fields.update(overrides)
    return Settings(**fields)


def csrf_from(html: str) -> str:
    import re

    match = re.search(r'name="csrf-token" content="([^"]*)"', html)
    assert match, "no csrf meta tag"
    return match.group(1)


def login(client) -> str:
    """Log the test client in and return the session CSRF token."""
    import re

    page = client.get("/login").text
    login_csrf = re.search(r'name="csrf_token" value="([^"]*)"', page).group(1)
    response = client.post("/login", data={"password": ADMIN_PASSWORD, "csrf_token": login_csrf})
    assert response.status_code == 200, response.text
    return csrf_from(client.get("/players").text)
