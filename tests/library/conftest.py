"""Conformance test fixtures for storage backends.

SQLite always runs in CI. Postgres runs when DEEP_RESEARCH_TEST_PG_DSN is set.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from deep_research.library.storage.sqlite_backend import SqliteStorageBackend


@pytest.fixture
async def sqlite_backend():
    """Fixture that provides a fresh SQLite backend."""
    tmp = tempfile.mkdtemp()
    db_path = str(Path(tmp) / "test_index.db")
    backend = SqliteStorageBackend(db_path)
    await backend.connect()
    await backend.ensure_schema()
    yield backend
    await backend.close()


@pytest.fixture
async def postgres_backend():
    """Fixture that provides a fresh Postgres backend.

    Skips with a clean message when DEEP_RESEARCH_TEST_PG_DSN is unset.
    """
    dsn = os.environ.get("DEEP_RESEARCH_TEST_PG_DSN")
    if not dsn:
        pytest.skip("DEEP_RESEARCH_TEST_PG_DSN not set — skipping Postgres conformance tests")
        yield None
        return

    from deep_research.library.storage.postgres_backend import PostgresStorageBackend

    backend = PostgresStorageBackend(dsn=dsn)
    await backend.connect()
    # Drop all tables for fresh state
    conn = backend._conn
    for t in ["refresh_jobs", "artifact_versions", "glossary", "tags",
              "citation_edges", "analyses", "reports", "artifacts", "schema_meta"]:
        await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    await backend.ensure_schema()
    yield backend
    await backend.close()
