"""Updater tests. POSIX only (symlinks, O_NOFOLLOW): run them on Linux/WSL."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="updater needs POSIX")

from coral_updater import fsops, minisign  # noqa: E402
from coral_updater.updater import COMMIT_DEADLINE_S, Paths, Updater  # noqa: E402

TOKEN = "tok"  # noqa: S105

# A stand-in for the minisign CLI: a signature is valid when it holds the sha256
# of the file; it prints the trusted comment like `minisign -V -Q`.
FAKE_MINISIGN = """#!{python}
import hashlib, sys
args = sys.argv[1:]
m = args[args.index("-m") + 1]; x = args[args.index("-x") + 1]
sig = open(x).read().splitlines()
digest = hashlib.sha256(open(m, "rb").read()).hexdigest()
if len(sig) < 2 or sig[0] != "FAKESIG " + digest:
    print("Signature verification failed", file=sys.stderr); sys.exit(1)
print(sig[1])
"""


def tarball(version, extra=None, broken_paths=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        files = {
            "player/coral_player/__init__.py": f'__version__ = "{version}"\n',
            "player/assets/fallback.png": "png",
            "deploy/pi/mpv.service": "[Service]\n",
        }
        files.update(extra or {})
        if broken_paths:
            files["../../etc/evil"] = "x"
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def fake_signature(data, version):
    return f"FAKESIG {hashlib.sha256(data).hexdigest()}\ncoral-player {version}\n"


class ReleaseServer:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.up = True
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                if not server.up:
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self.send_response(401)
                    self.end_headers()
                    return
                data = server.files.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def publish(self, version, data=None, signature=None):
        data = data if data is not None else tarball(version)
        self.files[f"/api/v1/releases/{version}"] = data
        self.files[f"/api/v1/releases/{version}/signature"] = (
            signature if signature is not None else fake_signature(data, version)
        ).encode()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Runner:
    def __init__(self):
        self.calls = []
        self.selftest_rc = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "coral_player.selftest" in argv:
            return self.selftest_rc
        return 0

    def restarts(self):
        return [c for c in self.calls if c[:2] == ["systemctl", "restart"]]


@pytest.fixture
def env(tmp_path):
    server = ReleaseServer()
    root = tmp_path / "root"
    paths = Paths(root)
    (paths.releases / "0.2.0/player/coral_player").mkdir(parents=True)
    os.symlink(paths.releases / "0.2.0", paths.current)
    paths.etc.mkdir(parents=True)
    (paths.etc / "config.toml").write_text(
        f'server_url = "{server.url}"\ntoken_file = "/etc/signage/token"\n'
    )
    (paths.etc / "token").write_text(TOKEN)
    (paths.etc / "release-pubkey.pub").write_text("pub")
    paths.agent_update.mkdir(parents=True)
    fake = tmp_path / "fake-minisign"
    fake.write_text(FAKE_MINISIGN.format(python=sys.executable))
    fake.chmod(0o755)
    clock, runner = Clock(), Runner()
    boot = {"id": "boot-1"}
    updater = Updater(
        paths, run=runner, monotonic=clock, minisign_bin=str(fake), boot=lambda: boot["id"]
    )

    class Env:
        pass

    e = Env()
    e.server, e.paths, e.updater, e.clock, e.runner, e.boot = (
        server,
        paths,
        updater,
        clock,
        runner,
        boot,
    )
    yield e
    server.httpd.shutdown()


def request(paths, version):
    (paths.agent_update / "request.json").write_text(json.dumps({"version": version}))


def status(paths):
    return json.loads((paths.state / "status.json").read_text())


def commit(paths, version, nonce):
    (paths.agent_update / "commit.json").write_text(
        json.dumps({"version": version, "nonce": nonce})
    )


# ------------------------------------------------------------------ good update


def test_good_update_installs_and_commits(env):
    env.server.publish("0.3.0")
    request(env.paths, "0.3.0")
    env.updater.tick()
    assert fsops.link_target(env.paths.current) == env.paths.releases / "0.3.0"
    assert fsops.link_target(env.paths.previous) == env.paths.releases / "0.2.0"
    assert (env.paths.releases / "0.3.0/player/coral_player/__init__.py").exists()
    st = status(env.paths)
    assert st["state"] == "pending_commit" and st["nonce"]
    assert any("coral_player.selftest" in c for c in env.runner.calls)
    assert env.runner.restarts()
    assert list(env.paths.downloads.iterdir()) == []

    commit(env.paths, "0.3.0", st["nonce"])
    env.updater.tick()
    assert status(env.paths)["state"] == "committed"
    assert not (env.paths.state / "pending.json").exists()


def test_nothing_to_do_when_running_requested_version(env):
    request(env.paths, "0.2.0")
    env.updater.tick()
    assert env.runner.calls == []


# ------------------------------------------------------------------- rollback


def test_no_commit_rolls_back_after_deadline(env):
    env.server.publish("0.3.0")
    request(env.paths, "0.3.0")
    env.updater.tick()
    env.clock.t += COMMIT_DEADLINE_S - 1
    env.updater.tick()
    assert status(env.paths)["state"] == "pending_commit"  # still within the deadline
    env.clock.t += 2
    env.updater.tick()
    assert fsops.link_target(env.paths.current) == env.paths.releases / "0.2.0"
    st = status(env.paths)
    assert (st["state"], st["version"]) == ("rolled_back", "0.3.0")
    assert not (env.paths.releases / "0.3.0").exists()
    assert len(env.runner.restarts()) == 2  # after install and after rollback
    # Not retried while the agent keeps asking for the same version.
    calls = len(env.runner.calls)
    env.updater.tick()
    assert len(env.runner.calls) == calls


def test_stale_commit_with_other_nonce_is_ignored(env):
    env.server.publish("0.3.0")
    request(env.paths, "0.3.0")
    env.updater.tick()
    commit(env.paths, "0.3.0", "not-the-nonce")
    env.clock.t += COMMIT_DEADLINE_S + 1
    env.updater.tick()
    assert status(env.paths)["state"] == "rolled_back"


def test_reboot_during_pending_gets_a_fresh_deadline(env):
    env.server.publish("0.3.0")
    request(env.paths, "0.3.0")
    env.updater.tick()
    env.boot["id"] = "boot-2"
    env.clock.t = 5.0  # monotonic restarts with the new boot
    env.updater.tick()
    assert status(env.paths)["state"] == "pending_commit"
    env.clock.t += COMMIT_DEADLINE_S + 1
    env.updater.tick()
    assert status(env.paths)["state"] == "rolled_back"


# ------------------------------------------------------------------- rejection


@pytest.mark.parametrize("case", ["bad_signature", "other_version_comment", "tampered_tarball"])
def test_bad_signatures_are_rejected(env, case):
    data = tarball("0.3.0")
    if case == "bad_signature":
        env.server.publish("0.3.0", data, "garbage\n")
    elif case == "other_version_comment":
        env.server.publish("0.3.0", data, fake_signature(data, "0.2.0"))
    else:
        env.server.publish("0.3.0", data + b"x", fake_signature(data, "0.3.0"))
    request(env.paths, "0.3.0")
    env.updater.tick()
    st = status(env.paths)
    assert (st["state"], st["version"]) == ("rejected", "0.3.0")
    assert fsops.link_target(env.paths.current) == env.paths.releases / "0.2.0"
    assert not (env.paths.releases / "0.3.0").exists()
    assert env.runner.calls == []  # no selftest, no restart
    assert list(env.paths.downloads.iterdir()) == []


def test_selftest_failure_is_rejected(env):
    env.server.publish("0.3.0")
    env.runner.selftest_rc = 1
    request(env.paths, "0.3.0")
    env.updater.tick()
    assert status(env.paths)["state"] == "rejected"
    assert fsops.link_target(env.paths.current) == env.paths.releases / "0.2.0"
    assert not (env.paths.releases / "0.3.0").exists()
    assert not (env.paths.releases / "0.3.0.tmp").exists()
    assert env.runner.restarts() == []


def test_tarball_with_paths_outside_release_is_rejected(env):
    env.server.publish("0.3.0", tarball("0.3.0", broken_paths=True))
    request(env.paths, "0.3.0")
    env.updater.tick()
    assert status(env.paths)["state"] == "rejected"
    assert not (env.paths.root / "etc/evil").exists()


def test_network_failure_is_retried(env):
    env.server.publish("0.3.0")
    env.server.up = False
    request(env.paths, "0.3.0")
    env.updater.tick()
    assert status(env.paths)["state"] == "failed"
    assert not (env.paths.state / "failed.json").exists()
    env.server.up = True
    env.updater.tick()
    assert status(env.paths)["state"] == "pending_commit"


# --------------------------------------------------------------- untrusted input


def test_malicious_requests_are_ignored(env, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"version": "0.3.0"}))
    os.symlink(secret, env.paths.agent_update / "request.json")
    env.updater.tick()
    assert env.runner.calls == []

    (env.paths.agent_update / "request.json").unlink()
    (env.paths.agent_update / "request.json").write_text("x" * 10000)
    env.updater.tick()
    request(env.paths, "../../0.3.0")
    env.updater.tick()
    assert env.runner.calls == []
    assert not (env.paths.state / "status.json").exists()


# ------------------------------------------------------------------- retention


def test_prune_keeps_three_and_current_previous(env):
    for v in ("0.1.0", "0.1.5", "0.4.0", "0.5.0"):
        (env.paths.releases / v).mkdir()
    fsops.switch_symlink(env.paths.previous, env.paths.releases / "0.1.0")
    (env.paths.releases / "junk").mkdir()
    env.updater.prune()
    names = sorted(p.name for p in env.paths.releases.iterdir())
    assert names == ["0.1.0", "0.2.0", "0.4.0", "0.5.0", "junk"]


def test_orphan_tmp_is_cleaned(env):
    (env.paths.releases / "0.3.0.tmp/player").mkdir(parents=True)
    env.updater.tick()
    assert not (env.paths.releases / "0.3.0.tmp").exists()


# ------------------------------------------------------------- real minisign


@pytest.mark.skipif(shutil.which("minisign") is None, reason="minisign not installed")
def test_real_minisign_sign_and_verify(tmp_path):
    pub, sec = tmp_path / "k.pub", tmp_path / "k.key"
    subprocess.run(["minisign", "-G", "-W", "-p", pub, "-s", sec], check=True, capture_output=True)
    other_pub, other_sec = tmp_path / "o.pub", tmp_path / "o.key"
    subprocess.run(
        ["minisign", "-G", "-W", "-p", other_pub, "-s", other_sec], check=True, capture_output=True
    )
    file = tmp_path / "coral-player-0.3.0.tar.gz"
    file.write_bytes(b"release")
    sig = tmp_path / "file.minisig"
    subprocess.run(
        ["minisign", "-S", "-s", sec, "-m", file, "-x", sig, "-t", "coral-player 0.3.0"],
        check=True,
        capture_output=True,
    )
    minisign.verify(pub, file, sig, "0.3.0")
    with pytest.raises(minisign.SignatureError, match="expected"):
        minisign.verify(pub, file, sig, "0.3.1")  # trusted comment is for 0.3.0
    with pytest.raises(minisign.SignatureError):
        minisign.verify(other_pub, file, sig, "0.3.0")  # wrong key
    file.write_bytes(b"tampered")
    with pytest.raises(minisign.SignatureError):
        minisign.verify(pub, file, sig, "0.3.0")
