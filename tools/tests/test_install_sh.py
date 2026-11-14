"""deploy/pi/install.sh in --dry-run mode (POSIX only): the actions it would take
and the cases it must refuse."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None, reason="needs bash (Linux)"
)

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/pi/install.sh"


@pytest.fixture
def env(tmp_path):
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_CODENAME=trixie\n'
    )
    return {
        **os.environ,
        "CORAL_OS_RELEASE": str(os_release),
        "CORAL_INSTALL_PYTHON": sys.executable,
    }


def run(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", *args], env=env, capture_output=True, text=True
    )


FIRST = [
    "--release",
    "/tmp/coral-player-0.3.4.tar.gz",
    "--pubkey",
    "/tmp/release-pubkey.pub",
    "--server",
    "https://coral.example.com",
]


def test_first_installation_plan(env):
    result = run(env, *FIRST)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    order = [
        "apt-get install -y -qq mpv polkitd minisign chrony wireguard-tools",
        "useradd --system",
        "minisign -V -p /etc/signage/release-pubkey.pub -m /tmp/coral-player-0.3.4.tar.gz",
        "tar xzf /tmp/coral-player-0.3.4.tar.gz",
        "write /etc/signage/config.toml",
        "50-signage.rules",
        "coral_updater",
        "write /etc/chrony/conf.d/signage.conf",
        "write /etc/systemd/system.conf.d/signage-watchdog.conf",
        "write /etc/apt/apt.conf.d/20auto-upgrades",
        "enable --now mpv.service signage-agent.service signage-updater.timer",
    ]
    positions = [out.index(item) for item in order]  # each present ...
    assert positions == sorted(positions)  # ... and in this order (verify before extract)
    assert "do_overlayfs" not in out and "mkfs" not in out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "--release is required"),
        (["--release", "x.tar.gz", "--pubkey", "k", "--server", "http://insecure"], "https://"),
        ([*FIRST, "--readonly-root"], "needs --data-partition"),
        (["--wireguard-only", "--wg-address", "10.77.0.2"], "needs --wg-endpoint"),
        (["--bogus"], "unknown option"),
    ],
)
def test_refusals(env, args, message):
    result = run(env, *args)
    assert result.returncode != 0
    assert message in result.stderr


def test_refuses_unsupported_system(env, tmp_path):
    Path(env["CORAL_OS_RELEASE"]).write_text(
        'PRETTY_NAME="Ubuntu"\nID=ubuntu\nVERSION_CODENAME=jammy\n'
    )
    result = run(env, *FIRST)
    assert result.returncode == 1 and "unsupported system" in result.stderr


def test_refuses_old_python(env, tmp_path):
    fake = tmp_path / "python-old"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    result = run({**env, "CORAL_INSTALL_PYTHON": str(fake)}, *FIRST)
    assert result.returncode == 1 and "3.11" in result.stderr


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("10\n", "max 10s, using 10s"),
        ("60\n", "max 60s, using 15s"),
        ("0\n", "max unknown (0), using 15s"),  # seen on the first Pi 5: never write 0s
        ("", "max unknown (not readable), using 15s"),
        (None, "max unknown (not readable), using 15s"),
    ],
)
def test_watchdog_timeout(env, tmp_path, content, expected):
    max_file = tmp_path / "max_timeout"
    if content is not None:
        max_file.write_text(content)
    result = run({**env, "CORAL_WATCHDOG_MAX_FILE": str(max_file)}, *FIRST)
    assert result.returncode == 0, result.stderr
    assert f"hardware watchdog: {expected}" in result.stdout


def test_readonly_root_plan(env):
    result = run(env, *FIRST, "--data-partition", "/dev/nvme0n1p3", "--readonly-root")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "mkfs.ext4 -q -L signage-data /dev/nvme0n1p3" in out
    assert out.index("mkfs.ext4") < out.index("tar xzf")  # releases land on the partition
    assert "raspi-config nonint do_overlayfs 0" in out
    assert "automatic OS updates OFF" in out


def test_wireguard_only_plan(env):
    result = run(
        env,
        "--wireguard-only",
        "--wg-address",
        "10.77.0.2",
        "--wg-endpoint",
        "coral.example.com:51820",
        "--wg-server-pubkey",
        "S" * 43 + "=",
    )
    assert result.returncode == 0, result.stderr
    assert "write /etc/wireguard/wg0.conf (600)" in result.stdout
    assert "write /etc/systemd/system/wg-quick@wg0.service.d/10-signage.conf (644)" in result.stdout
    out = result.stdout
    assert out.index("daemon-reload") < out.index("enable wg-quick@wg0")
    assert "apt-get" not in result.stdout


def test_ssh_only_wireguard_plan(env):
    result = run(env, *FIRST, "--ssh-only-wireguard")
    assert result.returncode == 0, result.stderr
    assert "write /etc/ssh/sshd_config.d/10-signage.conf" in result.stdout
    assert "signage-ssh-wg.service" in result.stdout
