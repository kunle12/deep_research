"""Conformance tests: artifact CRUD — parameterized over SQLite and Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deep_research.library.storage.rows import ArtifactRow


@pytest.mark.asyncio
async def test_upsert_and_get_artifact(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="abc123",
        kind="pdf",
        source_url="https://arxiv.org/abs/2401.12345",
        source_type="arxiv",
        title="Test Paper",
        discovered_by="arxiv",
        arxiv_id="2401.12345",
        bytes_path="artifacts/pdf/abc123.pdf",
        bytes_size=5000,
        first_seen_at=now,
        last_touched_at=now,
    )
    aid = await sqlite_backend.upsert_artifact(art)
    assert aid == "abc123"

    fetched = await sqlite_backend.get_artifact("abc123")
    assert fetched is not None
    assert fetched.artifact_id == "abc123"
    assert fetched.kind == "pdf"
    assert fetched.source_type == "arxiv"
    assert fetched.title == "Test Paper"


@pytest.mark.asyncio
async def test_find_by_url(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="url123",
        kind="html",
        source_url="https://example.com/page",
        source_type="html",
        title="Example Page",
        discovered_by="fetch_page",
        bytes_path="artifacts/html/url123",
        bytes_size=2000,
        first_seen_at=now,
        last_touched_at=now,
    )
    await sqlite_backend.upsert_artifact(art)

    found = await sqlite_backend.find_artifact_by_url("https://example.com/page")
    assert found is not None
    assert found.artifact_id == "url123"

    missing = await sqlite_backend.find_artifact_by_url("https://nonexistent.com")
    assert missing is None


@pytest.mark.asyncio
async def test_find_by_arxiv_id(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="arxiv123",
        kind="pdf",
        source_url="https://arxiv.org/abs/2401.99999",
        source_type="arxiv",
        title="Arxiv Paper",
        discovered_by="arxiv",
        arxiv_id="2401.99999",
        bytes_path="artifacts/pdf/arxiv123.pdf",
        bytes_size=3000,
        first_seen_at=now,
        last_touched_at=now,
    )
    await sqlite_backend.upsert_artifact(art)

    found = await sqlite_backend.find_artifact_by_arxiv_id("2401.99999")
    assert found is not None
    assert found.artifact_id == "arxiv123"

    missing = await sqlite_backend.find_artifact_by_arxiv_id("0000.00000")
    assert missing is None


@pytest.mark.asyncio
async def test_get_missing_artifact(sqlite_backend):
    missing = await sqlite_backend.get_artifact("nonexistent")
    assert missing is None
