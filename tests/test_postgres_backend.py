"""Tests for Postgres storage backend (P12.0).

Uses mocking since no real Postgres instance is available in CI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from deep_research.library.storage.postgres_backend import PostgresStorageBackend
from deep_research.library.storage.rows import ArtifactRow


@pytest.mark.asyncio
async def test_connect_and_schema():
    """Test that connect and ensure_schema work with mocked asyncpg."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value="CREATE TABLE")
    mock_conn.fetchval = AsyncMock(return_value=None)  # No schema_meta yet
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[])

    with patch("asyncpg.connect", return_value=mock_conn):
        backend = PostgresStorageBackend(dsn="postgres://localhost/test")
        await backend.connect()
        # Schema initialized; no version-based checks needed
        await backend.ensure_schema()
        await backend.close()


@pytest.mark.asyncio
async def test_upsert_artifact():
    """upsert_artifact calls execute with INSERT SQL."""
    from datetime import UTC, datetime

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value="INSERT 1")
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[])

    with patch("asyncpg.connect", return_value=mock_conn):
        backend = PostgresStorageBackend(dsn="postgres://localhost/test")
        await backend.connect()

        now = datetime.now(UTC).isoformat()
        art = ArtifactRow(
            artifact_id="test123", kind="pdf", source_type="arxiv",
            title="Test", discovered_by="arxiv",
            bytes_path="test.pdf", bytes_size=100,
            first_seen_at=now, last_touched_at=now,
        )
        aid = await backend.upsert_artifact(art)
        assert aid == "test123"
        assert mock_conn.execute.called
        await backend.close()
