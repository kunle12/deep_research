"""LibraryRegistry — resolves one StorageBackend per user.

Core multi-user idea (docs/MULTI_USER_PLAN.md §2/§4): a ``StorageBackend``
instance still represents exactly one personal library; multi-user means
resolving it per user instead of per process. The StorageBackend protocol,
both backend implementations, and LibraryWriter are untouched.

Layouts:

- ``single_user`` mode (legacy, default): the library lives directly at
  ``pdl.root_dir`` (``index.db``, ``artifacts/``, ``reports/``) and the
  registry wraps one pre-opened backend — behavior identical to before.
- ``multi_user`` mode: one library per user under
  ``<pdl.root_dir>/users/<user_id>/`` (SQLite) or one Postgres schema per user
  (``library_<user_id>``). ``user_id`` is always a uuid4 hex string — never an
  email — so paths stay safe and stable (plan §4/§9).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path

from deep_research.config import AgentTopConfig
from deep_research.library.storage.base import StorageBackend

logger = logging.getLogger(__name__)

# \Z (not $) so a trailing newline can never sneak into a filesystem path.
_USER_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


def validate_user_id(user_id: str) -> str:
    """Ensure *user_id* is a uuid4 hex string (path-traversal safe)."""
    if not _USER_ID_RE.fullmatch(user_id):
        raise ValueError(f"invalid user_id: {user_id!r}")
    return user_id


def new_user_id() -> str:
    return uuid.uuid4().hex


class LibraryRegistry:
    """Lazily opened, cached per-user storage backends (multi_user mode)."""

    def __init__(self, cfg: AgentTopConfig) -> None:
        self._cfg = cfg
        self._root = Path(cfg.pdl.root_dir)
        self._backends: dict[str, StorageBackend] = {}
        # Serializes first-open per user: concurrent requests for the same
        # not-yet-open user must not each open a backend (leaking the loser's
        # aiosqlite thread / asyncpg pool). H1, docs/MULTI_USER_IMPLEMENTATION_LOG.md.
        self._open_locks: dict[str, asyncio.Lock] = {}

    def root_for(self, user_id: str) -> Path:
        validate_user_id(user_id)
        return self._root / "users" / user_id

    def schema_for(self, user_id: str) -> str | None:
        """Postgres schema for this user's library (None for SQLite)."""
        validate_user_id(user_id)
        if self._cfg.pdl.storage.backend != "postgres":
            return None
        return f"library_{user_id}"

    def has_library(self, user_id: str) -> bool:
        """True when the user's library directory exists on disk (SQLite)."""
        if self._cfg.pdl.storage.backend != "sqlite":
            return True
        return (self.root_for(user_id) / "index.db").exists()

    def user_ids(self) -> list[str]:
        """All user ids that have a library directory on disk (SQLite)."""
        users_dir = self._root / "users"
        if not users_dir.is_dir():
            return []
        return sorted(
            p.name for p in users_dir.iterdir() if p.is_dir() and _USER_ID_RE.match(p.name)
        )

    async def backend_for(self, user_id: str) -> StorageBackend:
        """Open (once) and return the user's library backend."""
        validate_user_id(user_id)
        backend = self._backends.get(user_id)
        if backend is not None:
            return backend
        # Double-checked open under a per-user lock: concurrent first requests
        # share one _open; the loser re-reads the cache and returns it.
        lock = self._open_locks.get(user_id)
        if lock is None:
            lock = self._open_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            backend = self._backends.get(user_id)
            if backend is None:
                backend = await self._open(user_id)
                self._backends[user_id] = backend
            return backend

    def peek_backend(self, user_id: str) -> StorageBackend | None:
        """Already-open backend for *user_id*, or None (sync fast path)."""
        return self._backends.get(user_id)

    async def close_all(self) -> None:
        for user_id, backend in list(self._backends.items()):
            try:
                await backend.close()
            except Exception:
                logger.warning("failed to close backend for user %s", user_id, exc_info=True)
        self._backends.clear()
        self._open_locks.clear()

    async def _open(self, user_id: str) -> StorageBackend:
        storage = self._cfg.pdl.storage.backend
        if storage == "sqlite":
            from deep_research.library.storage.sqlite_backend import SqliteStorageBackend

            backend = SqliteStorageBackend(db_path=str(self.root_for(user_id) / "index.db"))
            await backend.connect()
            return backend
        if storage == "postgres":
            import os

            dsn = os.environ.get(self._cfg.pdl.storage.postgres_dsn_env, "")
            if not dsn:
                raise ValueError(
                    f"Postgres DSN not set. Set {self._cfg.pdl.storage.postgres_dsn_env} env var."
                )
            from deep_research.library.storage.postgres_backend import PostgresStorageBackend

            backend = PostgresStorageBackend(dsn=dsn, schema=f"library_{user_id}")
            await backend.connect()
            await backend.ensure_schema()
            return backend
        raise ValueError(f"Unknown storage backend: {storage}")


class StaticLibraryRegistry:
    """Wraps one pre-opened backend — single_user mode and injected test backends.

    Every user resolves to the same backend/root, which is exactly the legacy
    single-library behavior.
    """

    def __init__(self, backend: StorageBackend, root: str | Path) -> None:
        self._backend = backend
        self._root = Path(root)

    def root_for(self, user_id: str) -> Path:
        return self._root

    def schema_for(self, user_id: str) -> str | None:
        return None

    def has_library(self, user_id: str) -> bool:
        return True

    def user_ids(self) -> list[str]:
        return []

    async def backend_for(self, user_id: str) -> StorageBackend:
        return self._backend

    def peek_backend(self, user_id: str) -> StorageBackend | None:
        return self._backend

    async def close_all(self) -> None:
        await self._backend.close()
