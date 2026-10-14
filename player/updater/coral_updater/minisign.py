"""Release signature check with the minisign CLI (spec 6.1, 6.5)."""

from __future__ import annotations

import subprocess
from pathlib import Path


class SignatureError(Exception):
    pass


def trusted_comment(version: str) -> str:
    return f"coral-player {version}"


def verify(
    pubkey: Path, file: Path, signature: Path, version: str, minisign: str = "minisign"
) -> None:
    """Raise SignatureError unless `file` is signed by `pubkey` with the trusted
    comment for `version`. The trusted comment is covered by the signature, so a
    valid old release cannot be served under a new version number."""
    try:
        result = subprocess.run(
            [minisign, "-V", "-Q", "-p", str(pubkey), "-m", str(file), "-x", str(signature)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SignatureError(f"cannot run minisign: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise SignatureError(f"invalid signature ({detail[-1] if detail else 'no detail'})")
    comment = result.stdout.strip()
    if comment != trusted_comment(version):
        raise SignatureError(f"signed for {comment!r}, expected {trusted_comment(version)!r}")
