"""Fetch worker: runs the scheduler (board / aggregator fetching, validation,
maintenance) in its own process so the API stays responsive.

    python -m app.worker
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from .db import init_db
from .engine import engine

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app.worker")


async def main() -> None:
    for _ in range(30):  # the database may still be starting up
        try:
            await init_db()
            break
        except (OSError, ConnectionError) as e:
            log.warning("database not ready (%s); retrying", e)
            await asyncio.sleep(2)
    else:
        raise RuntimeError("database unavailable")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await engine.start()
    if not engine.is_leader:
        log.warning("another process holds the scheduler lock; waiting for it to be released")
    try:
        while not engine.is_leader and not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                await engine.start()
        await stop.wait()
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
