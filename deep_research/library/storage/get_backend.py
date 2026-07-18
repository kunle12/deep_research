"""Factory — resolves a StorageBackend from config.

Supports SQLite (P10.5a) and Postgres (P12.0).
"""

from __future__ import annotations

import logging
from pathlib import Path

from deep_research.config import AgentTopConfig
from deep_research.library.storage.base import StorageBackend

logger = logging.getLogger(__name__)


async def get_backend(config: AgentTopConfig) -> StorageBackend:
    """Return a StorageBackend matching the configured backend type."""
    backend_name = config.pdl.storage.backend
    root_dir = Path(config.pdl.root_dir)

    if backend_name == "sqlite":
        from deep_research.library.storage.sqlite_backend import SqliteStorageBackend

        backend = SqliteStorageBackend(db_path=str(root_dir / "index.db"))
        await backend.connect()
        await backend.ensure_schema()
        return backend

    elif backend_name == "postgres":
        import os

        dsn = os.environ.get(config.pdl.storage.postgres_dsn_env, "")
        if not dsn:
            raise ValueError(
                f"Postgres DSN not set. Set {config.pdl.storage.postgres_dsn_env} env var."
            )
        from deep_research.library.storage.postgres_backend import PostgresStorageBackend

        backend = PostgresStorageBackend(dsn=dsn)
        await backend.connect()
        await backend.ensure_schema()
        return backend

    else:
        raise ValueError(f"Unknown storage backend: {backend_name}")
