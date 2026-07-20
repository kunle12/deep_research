"""Refresh scheduler — long-running process that periodically refreshes library artifacts.

P12(b): implemented. Uses apscheduler + croniter for cron-based scheduling.
Daemonized entrypoint: `python -m deep_research.scheduler`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from deep_research.config import AgentTopConfig
from deep_research.library.storage import get_backend
from deep_research.library.writer import LibraryWriter

logger = logging.getLogger(__name__)

# Default: check every 6 hours
_DEFAULT_CHECK_INTERVAL_HOURS = 6


class RefreshScheduler:
    """Long-running scheduler that refreshes library artifacts on a cron schedule."""

    def __init__(self, config: AgentTopConfig) -> None:
        self._config = config
        self._shutdown_event = asyncio.Event()

    async def _run_refresh_cycle(self) -> None:
        """Run one full refresh cycle across all source types."""
        backend = None
        try:
            backend = await get_backend(self._config)
            writer = LibraryWriter(backend, self._config.pdl.root_dir)
            for source_type in ("arxiv", "blog", "html"):
                result = await writer.run_refresh_job("source_type", source_type)
                logger.info(
                    "refresh %s: %d considered, %d refreshed, %d errors",
                    source_type,
                    result["considered"],
                    result["refreshed"],
                    result["errored"],
                )
        except Exception as e:
            logger.error("refresh cycle failed: %s: %s", type(e).__name__, e)
        finally:
            if backend is not None:
                await backend.close()

    async def run(self) -> None:
        """Run the scheduler loop until shutdown is requested."""
        interval_s = _DEFAULT_CHECK_INTERVAL_HOURS * 3600
        logger.info("refresh scheduler starting (interval=%ds)", interval_s)
        while not self._shutdown_event.is_set():
            try:
                await self._run_refresh_cycle()
            except Exception as e:
                logger.error("refresh cycle error: %s", e)
            # Wait for interval or shutdown signal
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=interval_s,
                )

    async def shutdown(self) -> None:
        self._shutdown_event.set()


def _run_scheduler(config_path: str = "config.yaml") -> None:
    """Run the scheduler as a blocking entrypoint."""
    logging.basicConfig(level=logging.INFO)
    cfg = AgentTopConfig.load_yaml(config_path)
    if not cfg.pdl.enabled:
        logger.warning("PDL is disabled — scheduler has nothing to refresh.")
        return

    scheduler = RefreshScheduler(cfg)

    # Use asyncio.run() — signal handling is built-in via KeyboardInterrupt
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(scheduler.run())


if __name__ == "__main__":
    _run_scheduler()
