from __future__ import annotations

import asyncio

from coral_player import selftest
from fake_mpv import FakeMpv


def write_config(tmp_path, mpv_address, **extra):
    data = tmp_path / "var"
    data.mkdir(exist_ok=True)
    token = tmp_path / "token"
    token.write_text("tok")
    fallback = tmp_path / "fallback.png"
    fallback.write_bytes(b"\x89PNG fake")
    lines = [
        'server_url = "https://example.com"',
        f'token_file = "{token.as_posix()}"',
        f'data_dir = "{data.as_posix()}"',
        f'mpv_ipc = "{mpv_address}"',
        f'fallback_image = "{fallback.as_posix()}"',
    ]
    lines += [f"{k} = {v!r}" for k, v in extra.items()]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def run_with_mpv(tmp_path, mutate=None):
    async def scenario():
        mpv = await FakeMpv().start()
        config = write_config(tmp_path, mpv.address)
        if mutate:
            mutate(tmp_path)
        lines = []
        ok = await asyncio.to_thread(selftest.run, config, lines.append, 0)
        await mpv.stop()
        return ok, lines

    return asyncio.run(scenario())


def test_selftest_passes(tmp_path):
    ok, lines = run_with_mpv(tmp_path)
    assert ok, lines
    assert lines[-1].startswith("selftest passed")
    assert any("modules import" in line for line in lines)


def test_selftest_fails_without_token(tmp_path):
    ok, lines = run_with_mpv(tmp_path, lambda p: (p / "token").unlink())
    assert not ok and "token" in lines[-1].lower()


def test_selftest_fails_without_fallback(tmp_path):
    ok, lines = run_with_mpv(tmp_path, lambda p: (p / "fallback.png").unlink())
    assert not ok and "fallback" in lines[-1]


def test_selftest_fails_without_mpv(tmp_path):
    config = write_config(tmp_path, "tcp:127.0.0.1:9")
    lines = []
    assert not selftest.run(config, lines.append, mpv_delay_s=0)
    assert "mpv IPC not reachable" in lines[-1]


def test_selftest_fails_on_broken_module(tmp_path, monkeypatch):
    def broken(name):
        raise SyntaxError("invalid syntax")

    monkeypatch.setattr(selftest.importlib, "import_module", broken)
    lines = []
    assert not selftest.run(tmp_path / "missing.toml", lines.append)
    assert "cannot import" in lines[-1]
