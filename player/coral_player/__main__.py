"""Entry point: python -m coral_player --config /etc/signage/config.toml"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from . import __version__
from .agent import Agent
from .changes import Backoff, PollingChangeSource
from .client import ServerClient
from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signage-agent")
    parser.add_argument("--config", type=Path, default=Path("/etc/signage/config.toml"))
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        token = config.read_token()
    except ConfigError as exc:
        print(f"signage-agent: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(levelname)s %(name)s: %(message)s",  # journald adds timestamps
    )
    logging.getLogger(__name__).info("signage-agent %s starting", __version__)
    client = ServerClient(config.server_url, token, config.http_timeout_s)
    changes = PollingChangeSource(
        config.poll_interval_s,
        config.full_check_interval_s,
        Backoff(config.backoff_min_s, config.backoff_max_s),
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Agent(config, client, changes).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
