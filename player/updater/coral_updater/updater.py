"""One updater tick (spec 6.2). Run every minute by signage-updater.timer.

State machine, all on disk so a power cut at any point is safe:

- pending.json present  -> wait for the agent's commit; past the deadline, roll back.
- request.json asks for a version that is not running -> download, verify the
  minisign signature, extract to releases/<v>.tmp, selftest, rename, write
  pending.json, switch the `current` symlink, restart the agent and mpv.

The commit deadline is measured with the monotonic clock of the current boot
(the Pi has no RTC, so wall time can jump); after a reboot the new boot gets a
fresh deadline.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import fsops, minisign

log = logging.getLogger("coral_updater")

VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
COMMIT_DEADLINE_S = 180.0
KEEP_RELEASES = 3
MAX_RELEASE_BYTES = 200 * 1024 * 1024
CHUNK = 256 * 1024

Runner = Callable[[Sequence[str]], int]


class UpdateError(Exception):
    """The update cannot proceed; reported, nothing changed on screen."""


class Rejected(UpdateError):
    """The release itself is bad (signature or selftest); never retried."""


@dataclass(frozen=True)
class Paths:
    root: Path = Path("/")

    @property
    def opt(self) -> Path:
        return self.root / "opt/signage"

    @property
    def releases(self) -> Path:
        return self.opt / "releases"

    @property
    def current(self) -> Path:
        return self.opt / "current"

    @property
    def previous(self) -> Path:
        return self.opt / "previous"

    @property
    def etc(self) -> Path:
        return self.root / "etc/signage"

    @property
    def agent_update(self) -> Path:
        """Written by the unprivileged agent: request.json, commit.json."""
        return self.root / "var/lib/signage/update"

    @property
    def state(self) -> Path:
        """The updater's own state (root)."""
        return self.root / "var/lib/signage-updater"

    @property
    def downloads(self) -> Path:
        return self.state / "downloads"


def default_runner(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(list(argv), timeout=300, check=False).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("%s failed: %s", argv[0], exc)
        return -1


def boot_id(root: Path) -> str:
    try:
        return (root / "proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


class Updater:
    def __init__(
        self,
        paths: Paths | None = None,
        *,
        run: Runner = default_runner,
        monotonic: Callable[[], float] = time.monotonic,
        minisign_bin: str = "minisign",
        python: str = "/usr/bin/python3",
        agent_user: str = "signage",
        boot: Callable[[], str] | None = None,
    ) -> None:
        self.paths = paths or Paths()
        self.run = run
        self.monotonic = monotonic
        self.minisign_bin = minisign_bin
        self.python = python
        self.agent_user = agent_user
        self.boot = boot or (lambda: boot_id(Path("/")))

    # --------------------------------------------------------------- status

    def status(
        self, state: str, version: str | None = None, error: str | None = None, **extra: Any
    ) -> None:
        fsops.write_json(
            self.paths.state / "status.json",
            {"state": state, "version": version, "last_error": error, **extra},
        )
        log.info("status %s %s%s", state, version or "", f": {error}" if error else "")

    def current_version(self) -> str | None:
        target = fsops.link_target(self.paths.current)
        return target.name if target is not None else None

    # ----------------------------------------------------------------- tick

    def tick(self) -> None:
        self.paths.state.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.paths.downloads.mkdir(mode=0o700, exist_ok=True)
        self.paths.releases.mkdir(parents=True, exist_ok=True)
        pending = fsops.read_json(self.paths.state / "pending.json")
        if isinstance(pending, dict) and pending.get("version"):
            self._check_pending(pending)
            return
        self._cleanup_tmp()
        request = fsops.read_untrusted_json(self.paths.agent_update / "request.json")
        version = request.get("version") if isinstance(request, dict) else None
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            return
        if version == self.current_version():
            return
        failed = fsops.read_json(self.paths.state / "failed.json") or {}
        if failed.get("version") == version:
            return  # never retry a failed version on our own (spec 6.2.7)
        try:
            self.install(version)
        except Rejected as exc:
            fsops.write_json(self.paths.state / "failed.json", {"version": version})
            self.status("rejected", version, str(exc))
        except UpdateError as exc:
            self.status("failed", version, str(exc))  # e.g. network: retried next tick

    # -------------------------------------------------------------- install

    def _server(self) -> tuple[str, str]:
        """Server URL and player token from the agent's configuration."""
        try:
            config = tomllib.loads((self.paths.etc / "config.toml").read_text(encoding="utf-8"))
            url = str(config["server_url"]).rstrip("/")
            # The token lives in /etc/signage (relative to the root, for tests).
            token_name = Path(str(config.get("token_file", "/etc/signage/token"))).name
            token = (self.paths.etc / token_name).read_text(encoding="utf-8").strip()
        except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
            raise UpdateError(f"cannot read the player configuration: {exc}") from exc
        parts = urlsplit(url)
        local = parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"}
        if not (parts.scheme == "https" or local):
            raise UpdateError("server_url must be https://")
        return url, token

    def _download(self, url: str, token: str, path: str, dest: Path, max_bytes: int) -> None:
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"Authorization": f"Bearer {token}", "User-Agent": "coral-updater"}
        if have:
            headers["Range"] = f"bytes={have}-"
        request = urllib.request.Request(url + path, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                mode = "ab" if response.status == 206 else "wb"
                written = have if mode == "ab" else 0
                with open(part, mode) as fh:
                    while chunk := response.read(CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            raise UpdateError("download larger than allowed")
                        fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                part.unlink(missing_ok=True)
            raise UpdateError(f"HTTP {exc.code} downloading {path}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise UpdateError(f"network error downloading {path}: {exc}") from exc
        os.replace(part, dest)

    def _extract(self, tarball: Path, dest: Path) -> None:
        try:
            with tarfile.open(tarball) as tar:
                for member in tar.getmembers():
                    name = member.name
                    if not (
                        name.startswith(("player/", "deploy/")) or name in {"player", "deploy"}
                    ):
                        raise Rejected(f"unexpected path in release: {name!r}")
                tar.extractall(dest, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise Rejected(f"cannot extract release: {exc}") from exc

    def _selftest(self, release: Path) -> None:
        argv = [
            "runuser",
            "-u",
            self.agent_user,
            "--",
            "env",
            f"PYTHONPATH={release / 'player'}",
            self.python,
            "-m",
            "coral_player.selftest",
            "--config",
            str(self.paths.etc / "config.toml"),
        ]
        if self.run(argv) != 0:
            raise Rejected("selftest failed (see the updater journal)")

    def install(self, version: str) -> None:
        url, token = self._server()
        self.status("downloading", version)
        tarball = self.paths.downloads / f"coral-player-{version}.tar.gz"
        signature = tarball.with_name(tarball.name + ".minisig")
        self._download(url, token, f"/api/v1/releases/{version}", tarball, MAX_RELEASE_BYTES)
        self._download(url, token, f"/api/v1/releases/{version}/signature", signature, 4096)
        try:
            minisign.verify(
                self.paths.etc / "release-pubkey.pub",
                tarball,
                signature,
                version,
                self.minisign_bin,
            )
        except minisign.SignatureError as exc:
            tarball.unlink(missing_ok=True)
            signature.unlink(missing_ok=True)
            raise Rejected(str(exc)) from exc

        self.status("installing", version)
        tmp = self.paths.releases / f"{version}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            self._extract(tarball, tmp)
            self._selftest(tmp)
        except Rejected:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        final = self.paths.releases / version
        if final.exists():  # never the running one: that version is not requested
            shutil.rmtree(final)
        os.replace(tmp, final)
        tarball.unlink(missing_ok=True)
        signature.unlink(missing_ok=True)

        old = fsops.link_target(self.paths.current)
        nonce = secrets.token_hex(8)
        # Pending first: if the power goes before the switch, the deadline simply
        # "rolls back" to the version that is still current.
        fsops.write_json(
            self.paths.state / "pending.json",
            {
                "version": version,
                "previous": str(old) if old else None,
                "nonce": nonce,
                "boot_id": self.boot(),
                "deadline": self.monotonic() + COMMIT_DEADLINE_S,
            },
        )
        if old is not None:
            fsops.switch_symlink(self.paths.previous, old)
        fsops.switch_symlink(self.paths.current, final)
        self.status("pending_commit", version, nonce=nonce)
        self._restart()

    def _restart(self) -> None:
        if self.run(["systemctl", "restart", "signage-agent.service", "mpv.service"]) != 0:
            log.error("could not restart signage-agent/mpv")

    # ------------------------------------------------------- commit/rollback

    def _check_pending(self, pending: dict[str, Any]) -> None:
        version = pending["version"]
        commit = fsops.read_untrusted_json(self.paths.agent_update / "commit.json")
        if (
            isinstance(commit, dict)
            and commit.get("version") == version
            and commit.get("nonce") == pending.get("nonce")
        ):
            fsops.remove(self.paths.state / "pending.json")
            self.status("committed", version)
            self.prune()
            return
        if pending.get("boot_id") != self.boot():
            # Rebooted while pending: give this boot a full deadline.
            pending = {
                **pending,
                "boot_id": self.boot(),
                "deadline": self.monotonic() + COMMIT_DEADLINE_S,
            }
            fsops.write_json(self.paths.state / "pending.json", pending)
            return
        if self.monotonic() >= float(pending.get("deadline", 0)):
            self.rollback(pending)

    def rollback(self, pending: dict[str, Any]) -> None:
        version = pending["version"]
        previous = pending.get("previous")
        log.error("no commit for %s before the deadline; rolling back", version)
        if previous:
            fsops.switch_symlink(self.paths.current, Path(previous))
        fsops.write_json(self.paths.state / "failed.json", {"version": version})
        fsops.remove(self.paths.state / "pending.json")
        self.status("rolled_back", version, "the new release did not confirm a healthy start")
        self._restart()
        if previous and Path(previous).name != version:
            shutil.rmtree(self.paths.releases / version, ignore_errors=True)

    # ------------------------------------------------------------ retention

    def _cleanup_tmp(self) -> None:
        for path in self.paths.releases.glob("*.tmp"):
            shutil.rmtree(path, ignore_errors=True)

    def prune(self) -> None:
        """Keep the newest KEEP_RELEASES releases, plus current and previous."""
        keep = {
            t.name
            for t in (fsops.link_target(self.paths.current), fsops.link_target(self.paths.previous))
            if t is not None
        }
        releases = [
            p for p in self.paths.releases.iterdir() if p.is_dir() and VERSION_RE.fullmatch(p.name)
        ]
        releases.sort(key=lambda p: tuple(int(x) for x in p.name.split(".")), reverse=True)
        for path in releases[KEEP_RELEASES:]:
            if path.name not in keep:
                shutil.rmtree(path, ignore_errors=True)
                log.info("removed old release %s", path.name)
