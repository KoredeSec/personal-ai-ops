"""Liveness heartbeat.

A scheduled job writes the current time to a file. The Docker healthcheck
(`scripts/healthcheck.py`) fails if the file is stale, which catches a hung
event loop, a dead scheduler or a crashed polling loop that a plain
"can I open the database" check would miss.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)


def write_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(time.time()))
    os.replace(tmp, path)  # atomic: readers never see a partial file


def heartbeat_age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - float(path.read_text().strip()))
    except (OSError, ValueError):
        return None


def start_watchdog(path: Path, max_age_seconds: float, check_every: float = 30.0,
                   grace_seconds: float = 180.0,
                   on_stale: Callable[[float | None], None] | None = None) -> threading.Thread:
    """Exit the process if the event-loop heartbeat goes stale.

    Runs on its own OS thread, so it still works when the asyncio loop is
    blocked. Docker's `restart: unless-stopped` then restarts the container
    (Docker does not restart merely *unhealthy* containers on its own).
    """
    started = time.monotonic()

    def _default_on_stale(age: float | None) -> None:
        logger.critical("Watchdog: heartbeat stale (age=%s); exiting for restart", age)
        logging.shutdown()
        os._exit(70)

    action = on_stale or _default_on_stale

    def _run() -> None:
        while True:
            time.sleep(check_every)
            if time.monotonic() - started < grace_seconds:
                continue
            age = heartbeat_age_seconds(path)
            if age is None or age > max_age_seconds:
                action(age)
                return

    t = threading.Thread(target=_run, name="watchdog", daemon=True)
    t.start()
    return t
