"""Tests for Postgres storage backend (P12.0).

Uses mocking since no real Postgres instance is available in CI.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from deep_research.library.storage.postgres_backend import PostgresStorageBackend
from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    CitationEdgeRow,
    GlossaryEntry,
    ReportRow,
    TagRow,
)


@pytest.fixture
def mock_conn():
    """Create a mocked asyncpg connection with common defaults."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="OK")
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    return conn


@pytest.fixture
def backend(mock_conn):
    with patch("asyncpg.connect", return_value=mock_conn):
        be = PostgresStorageBackend(dsn="postgres://localhost/test")
        be._conn = mock_conn
        yield be


@pytest.mark.asyncio
async def test_connect_and_schema():
    """Test that connect and ensure_schema work with mocked asyncpg."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value="CREATE TABLE")
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=[])

    with patch("asyncpg.connect", return_value=mock_conn):
        backend = PostgresStorageBackend(dsn="postgres://localhost/test")
        await backend.connect()
        await backend.ensure_schema()
        await backend.close()


@pytest.mark.asyncio
async def test_upsert_artifact(backend, mock_conn):
    """upsert_artifact calls execute with INSERT SQL."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="test123",
        kind="pdf",
        source_type="arxiv",
        title="Test",
        discovered_by="arxiv",
        bytes_path="test.pdf",
        bytes_size=100,
        first_seen_at=now,
        last_touched_at=now,
    )
    aid = await backend.upsert_artifact(art)
    assert aid == "test123"
    assert mock_conn.execute.called


@pytest.mark.asyncio
async def test_get_artifact(backend, mock_conn):
    mock_conn.fetchrow = AsyncMock(
        return_value=(
            "art1",
            "pdf",
            "https://example.com",
            "arxiv",
            "Test Paper",
            None,
            None,
            None,
            None,
            "path.pdf",
            1024,
            "2024-01-01T00:00:00Z",
            "2024-01-01T00:00:00Z",
            None,
            None,
            None,
            None,
        )
    )
    art = await backend.get_artifact("art1")
    assert art is not None
    assert art.artifact_id == "art1"
    assert art.title == "Test Paper"


@pytest.mark.asyncio
async def test_find_artifact_by_url(backend, mock_conn):
    mock_conn.fetchrow = AsyncMock(
        return_value=(
            "art1",
            "pdf",
            "https://example.com",
            "html",
            "Test",
            None,
            None,
            None,
            None,
            "path.pdf",
            512,
            "2024-01-01T00:00:00Z",
            "2024-01-01T00:00:00Z",
            None,
            None,
            None,
            None,
        )
    )
    found = await backend.find_artifact_by_url("https://example.com")
    assert found is not None
    assert found.artifact_id == "art1"


@pytest.mark.asyncio
async def test_find_artifact_by_arxiv_id(backend, mock_conn):
    mock_conn.fetchrow = AsyncMock(
        return_value=(
            "art1",
            "pdf",
            "https://arxiv.org/pdf/2401.12345",
            "arxiv",
            "Arxiv Paper",
            None,
            None,
            "2401.12345",
            None,
            "path.pdf",
            2048,
            "2024-01-01T00:00:00Z",
            "2024-01-01T00:00:00Z",
            None,
            None,
            None,
            None,
        )
    )
    found = await backend.find_artifact_by_arxiv_id("2401.12345")
    assert found is not None
    assert found.artifact_id == "art1"


@pytest.mark.asyncio
async def test_insert_report(backend, mock_conn):
    report = ReportRow(
        run_id="run001",
        started_at="2024-01-01T00:00:00Z",
        original_query="test query",
        path_taken="quick",
        markdown="# Test",
    )
    await backend.insert_report(report)
    assert mock_conn.execute.called


@pytest.mark.asyncio
async def test_get_report(backend, mock_conn):
    mock_conn.fetchrow = AsyncMock(
        return_value=(
            "run001",
            "2024-01-01T00:00:00Z",
            "2024-01-01T01:00:00Z",
            "test query",
            "quick",
            "rationale",
            5,
            "{}",
            "# Test",
            "art1",
            "[]",
            "{}",
        )
    )
    report = await backend.get_report("run001")
    assert report is not None
    assert report.run_id == "run001"
    assert report.original_query == "test query"


@pytest.mark.asyncio
async def test_list_reports(backend, mock_conn):
    mock_conn.fetch = AsyncMock(
        return_value=[
            (
                "run001",
                "2024-01-01T00:00:00Z",
                "2024-01-01T01:00:00Z",
                "query1",
                "quick",
                "r1",
                5,
                "{}",
                "#1",
                "art1",
                "[]",
                "{}",
            ),
            (
                "run002",
                "2024-01-02T00:00:00Z",
                None,
                "query2",
                "deep",
                "r2",
                10,
                "{}",
                "#2",
                "art2",
                "[]",
                "{}",
            ),
        ]
    )
    reports = await backend.list_reports(10)
    assert len(reports) == 2
    assert reports[0].run_id == "run001"
    assert reports[1].run_id == "run002"


@pytest.mark.asyncio
async def test_insert_analysis(backend, mock_conn):
    analysis = AnalysisRow(
        analysis_id="ana1",
        artifact_id="art1",
        run_id="run001",
        analyzer="analyze_paper",
        summary="Test analysis",
        key_findings=json.dumps(["finding1"]),
        analyzed_at="2024-01-01T00:00:00Z",
    )
    aid = await backend.insert_analysis(analysis)
    assert aid == "ana1"


@pytest.mark.asyncio
async def test_get_analysis(backend, mock_conn):
    mock_conn.fetchrow = AsyncMock(
        return_value=(
            "ana1",
            "art1",
            "run001",
            "analyze_paper",
            "summary",
            '["finding1"]',
            "method",
            None,
            None,
            None,
            None,
            None,
            "2024-01-01T00:00:00Z",
        )
    )
    analysis = await backend.get_analysis("ana1")
    assert analysis is not None
    assert analysis.analysis_id == "ana1"


@pytest.mark.asyncio
async def test_get_analyses_for_artifact(backend, mock_conn):
    mock_conn.fetch = AsyncMock(
        return_value=[
            (
                "ana1",
                "art1",
                "run001",
                "analyze_paper",
                "summary",
                '["finding1"]',
                None,
                None,
                None,
                None,
                None,
                None,
                "2024-01-01T00:00:00Z",
            ),
        ]
    )
    analyses = await backend.get_analyses_for_artifact("art1")
    assert len(analyses) == 1


@pytest.mark.asyncio
async def test_insert_citation_edge(backend, mock_conn):
    edge = CitationEdgeRow(
        source_artifact_id="src1",
        target_arxiv_id="2401.54321",
        weight=0.8,
    )
    await backend.insert_citation_edge(edge)
    assert mock_conn.execute.called


@pytest.mark.asyncio
async def test_tag_ops(backend, mock_conn):
    # upsert_tag
    tag = TagRow(tag="RL", artifact_id="art1", applied_in_run="run001")
    await backend.upsert_tag(tag)
    assert mock_conn.execute.called

    # get_tags_for_artifact
    mock_conn.fetch = AsyncMock(return_value=[("RL", "art1", "run001")])
    tags = await backend.get_tags_for_artifact("art1")
    assert len(tags) == 1
    assert tags[0].tag == "RL"

    # get_tags_for_artifacts (batch)
    mock_conn.fetch = AsyncMock(return_value=[("RL", "art1", "run001")])
    tags_by_art = await backend.get_tags_for_artifacts(["art1", "art2"])
    assert "art1" in tags_by_art
    assert len(tags_by_art["art1"]) == 1

    # empty batch
    assert await backend.get_tags_for_artifacts([]) == {}

    # get_artifacts_by_tag
    mock_conn.fetch = AsyncMock(return_value=[("art1",)])
    aids = await backend.get_artifacts_by_tag("RL")
    assert aids == ["art1"]

    # delete_tag
    await backend.delete_tag("RL", "art1")
    assert mock_conn.execute.called

    # rename_tag
    await backend.rename_tag("old", "new")
    assert mock_conn.execute.called


@pytest.mark.asyncio
async def test_glossary_ops(backend, mock_conn):
    entry = GlossaryEntry(
        term="RLHF",
        term_canonical="rlhf",
        kind="acronym",
        short_def="RL from human feedback",
        acronym_expansion="Reinforcement Learning from Human Feedback",
        confidence=0.9,
        last_updated="2024-01-01T00:00:00Z",
    )
    await backend.upsert_glossary_entry(entry)
    assert mock_conn.execute.called

    # list_glossary_entries
    mock_conn.fetch = AsyncMock(
        return_value=[
            (
                1,
                "RLHF",
                "rlhf",
                "acronym",
                "def",
                None,
                "expansion",
                None,
                None,
                0.9,
                None,
                None,
                "2024-01-01T00:00:00Z",
            ),
        ]
    )
    entries = await backend.list_glossary_entries()
    assert len(entries) == 1
    assert entries[0].term == "RLHF"

    # get_glossary_entry
    mock_conn.fetchrow = AsyncMock(
        return_value=(
            1,
            "RLHF",
            "rlhf",
            "acronym",
            "def",
            None,
            "expansion",
            None,
            None,
            0.9,
            None,
            None,
            "2024-01-01T00:00:00Z",
        )
    )
    entry = await backend.get_glossary_entry("rlhf")
    assert entry is not None
    assert entry.term == "RLHF"


@pytest.mark.asyncio
async def test_refresh_jobs(backend, mock_conn):
    mock_conn.fetchval = AsyncMock(return_value="job001")
    job_id = await backend.start_refresh_job("source_type", "arxiv")
    assert isinstance(job_id, str) and len(job_id) > 0

    await backend.complete_refresh_job(job_id, 5, 2, "completed")

    mock_conn.fetchrow = AsyncMock(
        return_value=(
            job_id,
            "2024-01-01T00:00:00Z",
            "2024-01-01T01:00:00Z",
            "source_type",
            "arxiv",
            5,
            2,
            "completed",
            None,
        )
    )
    job = await backend.get_refresh_job(job_id)
    assert job is not None
    assert job.status == "completed"


@pytest.mark.asyncio
async def test_artifact_versions(backend, mock_conn):
    await backend.insert_artifact_version("old_id", "new_id", "content_changed", "run001")
    assert mock_conn.execute.called


@pytest.mark.asyncio
async def test_delete_report(backend, mock_conn):
    await backend.delete_report("run001")
    assert mock_conn.execute.called


@pytest.mark.asyncio
async def test_delete_artifact(backend, mock_conn):
    await backend.delete_artifact("art1")
    assert mock_conn.execute.called


@pytest.mark.asyncio
async def test_full_text_search(backend, mock_conn):
    mock_conn.fetch = AsyncMock(
        return_value=[
            ("art1", "Test Paper", None, "summary", "text", 0.95),
        ]
    )
    hits = await backend.full_text_search("transformer", kind="pdf", limit=10)
    assert len(hits) == 1
    assert hits[0].artifact_id == "art1"


@pytest.mark.asyncio
async def test_glossary_search(backend, mock_conn):
    mock_conn.fetch = AsyncMock(
        return_value=[
            (
                1,
                "Transformer",
                "transformer",
                "concept",
                "A neural network",
                None,
                None,
                None,
                None,
                0.9,
                None,
                None,
                "2024-01-01T00:00:00Z",
            ),
        ]
    )
    entries = await backend.glossary_search("transformer", limit=10)
    assert len(entries) == 1
    assert entries[0].term == "Transformer"
