"""Release selftest (spec 6.2.2). The updater runs it from the new release before
switching to it:

    PYTHONPATH=<release>/player python3 -m coral_player.selftest --config /etc/signage/config.toml

Checks that every module imports, the configuration and token are readable, the
fallback image exists, the data directory is writable and mpv answers on its IPC
socket. Exit code 0 means the release may be installed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import pkgutil
import sys
from collections.abc import Callable
from pathlib import Path

import coral_player

from .config import Config, ConfigError, load_config
from .mpv_ipc import MpvError, MpvIpc


class SelftestError(Exception):
    pass


def check_imports() -> str:
    names = [m.name for m in pkgutil.iter_modules(coral_player.__path__, "coral_player.")]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # any import-time failure disqualifies the release
            raise SelftestError(f"cannot import {name}: {exc!r}") from exc
    return f"{len(names)} modules import"


def check_config(path: Path) -> tuple[Config, str]:
    try:
        config = load_config(path)
        config.read_token()
    except ConfigError as exc:
        raise SelftestError(str(exc)) from exc
    return config, "config and token readable"


def check_fallback(config: Config) -> str:
    try:
        size = config.fallback_image.stat().st_size
    except OSError as exc:
        raise SelftestError(f"fallback image missing: {config.fallback_image}") from exc
    if size == 0:
        raise SelftestError("fallback image is empty")
    return "fallback image present"


def check_data_dir(config: Config) -> str:
    if not os.access(config.data_dir, os.W_OK):
        raise SelftestError(f"data directory {config.data_dir} is not writable")
    return "data directory writable"


async def check_mpv(config: Config, attempts: int = 3, delay_s: float = 2) -> str:
    last = None
    for _ in range(attempts):
        ipc = MpvIpc(config.mpv_ipc)
        try:
            await ipc.connect(timeout_s=5)
            version = await ipc.get_property("mpv-version")
            return f"mpv answers ({version})"
        except (OSError, TimeoutError, MpvError, ValueError) as exc:
            last = exc
            await asyncio.sleep(delay_s)
        finally:
            await ipc.close()
    raise SelftestError(f"mpv IPC not reachable at {config.mpv_ipc}: {last}")


def run(config_path: Path, out: Callable[[str], None] = print, mpv_delay_s: float = 2) -> bool:
    try:
        out("OK   " + check_imports())
        config, message = check_config(config_path)
        out("OK   " + message)
        out("OK   " + check_fallback(config))
        out("OK   " + check_data_dir(config))
        out("OK   " + asyncio.run(check_mpv(config, delay_s=mpv_delay_s)))
    except SelftestError as exc:
        out(f"FAIL {exc}")
        return False
    out(f"selftest passed (coral-player {coral_player.__version__})")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coral_player.selftest")
    parser.add_argument("--config", type=Path, default=Path("/etc/signage/config.toml"))
    args = parser.parse_args(argv)
    return 0 if run(args.config) else 1


if __name__ == "__main__":
    sys.exit(main())
