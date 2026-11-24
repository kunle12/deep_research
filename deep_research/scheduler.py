"""Refresh scheduler — long-running process that periodically refreshes library artifacts.

P12(b): implemented as a fixed-interval loop (default every 6 hours, see
`_DEFAULT_CHECK_INTERVAL_HOURS`). Daemonized entrypoint:
`python -m deep_research.scheduler`.

Multi-user: in `multi_user` mode the cycle iterates every user's library
(`<root>/users/<user_id>/`), each with its own backend/writer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path

from deep_research.config import AgentTopConfig
from deep_research.library.storage import get_backend
from deep_research.library.writer import LibraryWriter

logger = logging.getLogger(__name__)

# Default: check every 6 hours
_DEFAULT_CHECK_INTERVAL_HOURS = 6

_USER_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _user_library_configs(config: AgentTopConfig) -> list[AgentTopConfig]:
    """One config per library to refresh (single config in single_user mode)."""
    if config.pdl.auth.mode != "multi_user":
        return [config]
    users_dir = Path(config.pdl.root_dir) / "users"
    if not users_dir.is_dir():
        return [config]
    scoped: list[AgentTopConfig] = []
    for entry in sorted(users_dir.iterdir()):
        if entry.is_dir() and _USER_ID_RE.match(entry.name):
            scoped.append(_scoped_config(config, str(entry)))
    return scoped or [config]


def _scoped_config(config: AgentTopConfig, root: str) -> AgentTopConfig:
    scoped = config.model_copy(deep=True)
    scoped.pdl.root_dir = root
    return scoped


class RefreshScheduler:
    """Long-running scheduler that refreshes library artifacts on a cron schedule."""

    def __init__(self, config: AgentTopConfig) -> None:
        self._config = config
        self._shutdown_event = asyncio.Event()
        self._backend = None

    async def _ensure_backend(self):
        if self._backend is None:
            self._backend = await get_backend(self._config)
        return self._backend

    async def _run_refresh_cycle(self) -> None:
        """Run one full refresh cycle across all source types (and users)."""
        for library_cfg in _user_library_configs(self._config):
            backend = None
            try:
                backend = await get_backend(library_cfg)
                writer = LibraryWriter(backend, library_cfg.pdl.root_dir)
                for source_type in ("arxiv", "blog", "html"):
                    result = await writer.run_refresh_job("source_type", source_type)
                    logger.info(
                        "refresh %s (%s): %d considered, %d refreshed, %d errors",
                        source_type,
                        library_cfg.pdl.root_dir,
                        result["considered"],
                        result["refreshed"],
                        result["errored"],
                    )
            except Exception as e:
                logger.error("refresh cycle failed: %s: %s", type(e).__name__, e)
            finally:
                if backend is not None:
                    with contextlib.suppress(Exception):
                        await backend.close()

    async def run(self) -> None:
        """Run the scheduler loop until shutdown is requested."""
        interval_s = _DEFAULT_CHECK_INTERVAL_HOURS * 3600
        logger.info("refresh scheduler starting (interval=%ds)", interval_s)
        try:
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
        finally:
            if self._backend is not None:
                await self._backend.close()

    async def shutdown(self) -> None:
        self._shutdown_event.set()


def _run_scheduler(config_path: str = "config.yaml") -> None:
    """Run the scheduler as a blocking entrypoint."""
    logging.basicConfig(level=logging.INFO)
    cfg = AgentTopConfig.load_yaml(config_path)
    cfg.ensure_dirs()
    if not cfg.pdl.enabled:
        logger.warning("PDL is disabled — scheduler has nothing to refresh.")
        return

    scheduler = RefreshScheduler(cfg)

    # Use asyncio.run() — signal handling is built-in via KeyboardInterrupt
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(scheduler.run())


if __name__ == "__main__":
    _run_scheduler()
