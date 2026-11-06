"""Scheduler stub. The real version (plan I4) reads cron times from settings."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("scheduler")


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper())
    log.info("tick")


if __name__ == "__main__":
    main()
