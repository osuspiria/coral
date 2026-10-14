"""Entry point for signage-updater.service: python3 -m coral_updater"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .updater import Paths, Updater


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coral_updater")
    parser.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("coral_updater").debug("coral-updater %s", __version__)
    try:
        Updater(Paths(args.root)).tick()
    except Exception:
        logging.getLogger("coral_updater").exception("updater tick failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
