"""Tests for SQLite storage backend (P10.5a)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    CitationEdgeRow,
    GlossaryEntry,
    ReportRow,
    TagRow,
)
from deep_research.library.storage.sqlite_backend import SqliteStorageBackend


@pytest.fixture
async def backend():
    tmp = tempfile.mkdtemp()
    db_path = str(Path(tmp) / "test_index.db")
    be = SqliteStorageBackend(db_path=db_path)
    await be.connect()
    yield be
    await be.close()


@pytest.mark.asyncio
async def test_schema_creation(backend):
    version = await backend.current_schema_version()
    assert version >= 3  # v1+v2+v3 applied


@pytest.mark.asyncio
async def test_artifact_crud(backend):
    art = ArtifactRow(
        artifact_id="abc123",
        kind="pdf",
        source_url="https://arxiv.org/pdf/2401.12345",
        source_type="arxiv",
        title="Test Paper",
        bytes_path="artifacts/pdf/abc123.pdf",
        bytes_size=1024,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    aid = await backend.upsert_artifact(art)
    assert aid == "abc123"

    fetched = await backend.get_artifact("abc123")
    assert fetched is not None
    assert fetched.title == "Test Paper"
    assert fetched.source_type == "arxiv"


@pytest.mark.asyncio
async def test_find_by_url(backend):
    art = ArtifactRow(
        artifact_id="def456",
        kind="html",
        source_url="https://example.com/page",
        source_type="html",
        title="Example Page",
        bytes_path="artifacts/html/def456",
        bytes_size=512,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    await backend.upsert_artifact(art)
    found = await backend.find_artifact_by_url("https://example.com/page")
    assert found is not None
    assert found.artifact_id == "def456"


@pytest.mark.asyncio
async def test_find_by_arxiv_id(backend):
    art = ArtifactRow(
        artifact_id="ghi789",
        kind="pdf",
        source_url="https://arxiv.org/pdf/2401.12345",
        source_type="arxiv",
        title="Arxiv Paper",
        arxiv_id="2401.12345",
        bytes_path="artifacts/pdf/ghi789.pdf",
        bytes_size=2048,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    await backend.upsert_artifact(art)
    found = await backend.find_artifact_by_arxiv_id("2401.12345")
    assert found is not None
    assert found.artifact_id == "ghi789"


@pytest.mark.asyncio
async def test_report_crud(backend):
    report = ReportRow(
        run_id="run001",
        started_at="2024-01-01T00:00:00Z",
        original_query="test query",
        path_taken="quick",
        markdown="# Test\n\ncontent",
    )
    await backend.insert_report(report)

    fetched = await backend.get_report("run001")
    assert fetched is not None
    assert fetched.original_query == "test query"
    assert fetched.path_taken == "quick"

    reports = await backend.list_reports(10)
    assert len(reports) >= 1


@pytest.mark.asyncio
async def test_analysis_crud(backend):
    art = ArtifactRow(
        artifact_id="art1",
        kind="pdf",
        source_url="https://arxiv.org/pdf/2401.12345",
        source_type="arxiv",
        bytes_path="artifacts/pdf/art1.pdf",
        bytes_size=1024,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    await backend.upsert_artifact(art)

    # Create a report first since analyses references reports(run_id)
    report = ReportRow(
        run_id="run001",
        started_at="2024-01-01T00:00:00Z",
        original_query="test",
        path_taken="quick",
        markdown="# test",
    )
    await backend.insert_report(report)

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

    fetched = await backend.get_analysis("ana1")
    assert fetched is not None
    assert fetched.summary == "Test analysis"


@pytest.mark.asyncio
async def test_citation_edge(backend):
    art = ArtifactRow(
        artifact_id="src1",
        kind="pdf",
        source_url="https://arxiv.org/pdf/2401.12345",
        source_type="arxiv",
        bytes_path="artifacts/pdf/src1.pdf",
        bytes_size=1024,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    await backend.upsert_artifact(art)

    edge = CitationEdgeRow(
        source_artifact_id="src1",
        target_arxiv_id="2401.54321",
        weight=0.8,
    )
    await backend.insert_citation_edge(edge)


@pytest.mark.asyncio
async def test_tag(backend):
    art = ArtifactRow(
        artifact_id="art1",
        kind="pdf",
        source_url="https://arxiv.org/pdf/2401.12345",
        source_type="arxiv",
        bytes_path="artifacts/pdf/art1.pdf",
        bytes_size=1024,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    await backend.upsert_artifact(art)

    # Create a report since tags reference reports(run_id)
    report = ReportRow(
        run_id="run001",
        started_at="2024-01-01T00:00:00Z",
        original_query="test",
        path_taken="quick",
        markdown="# test",
    )
    await backend.insert_report(report)

    tag = TagRow(tag="RL", artifact_id="art1", applied_in_run="run001")
    await backend.upsert_tag(tag)


@pytest.mark.asyncio
async def test_glossary(backend):
    entries = [
        GlossaryEntry(
            term="RLHF", term_canonical="rlhf", kind="acronym",
            short_def="RL from human feedback",
            acronym_expansion="Reinforcement Learning from Human Feedback",
            confidence=0.9, last_updated="2024-01-01T00:00:00Z",
        ),
    ]
    for e in entries:
        await backend.upsert_glossary_entry(e)

    all_entries = await backend.list_glossary_entries()
    assert len(all_entries) == 1
    assert all_entries[0].term == "RLHF"

    found = await backend.get_glossary_entry("rlhf")
    assert found is not None
    assert found.term == "RLHF"


@pytest.mark.asyncio
async def test_glossary_dedup(backend):
    e1 = GlossaryEntry(term="RLHF", term_canonical="rlhf", kind="acronym",
                        short_def="v1", confidence=0.5, last_updated="now")
    await backend.upsert_glossary_entry(e1)

    e2 = GlossaryEntry(term="RLHF", term_canonical="rlhf", kind="acronym",
                        short_def="v2", long_def="longer",
                        confidence=0.9, last_updated="now2")
    await backend.upsert_glossary_entry(e2)

    entries = await backend.list_glossary_entries()
    assert len(entries) == 1
    assert entries[0].confidence == 0.9
    assert entries[0].long_def == "longer"


@pytest.mark.asyncio
async def test_refresh_jobs(backend):
    job_id = await backend.start_refresh_job("source_type", "arxiv")
    assert job_id

    await backend.complete_refresh_job(job_id, 5, 2, "completed")

    job = await backend.get_refresh_job(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.artifacts_considered == 5
    assert job.artifacts_refreshed == 2


@pytest.mark.asyncio
async def test_artifact_versions(backend):
    # Create artifacts first since artifact_versions references artifacts
    old = ArtifactRow(
        artifact_id="old_id", kind="pdf", source_url="https://example.com/old",
        bytes_path="old.pdf", bytes_size=100,
        first_seen_at="2024-01-01T00:00:00Z", last_touched_at="2024-01-01T00:00:00Z",
    )
    new = ArtifactRow(
        artifact_id="new_id", kind="pdf", source_url="https://example.com/new",
        bytes_path="new.pdf", bytes_size=200,
        first_seen_at="2024-01-01T00:00:00Z", last_touched_at="2024-01-01T00:00:00Z",
    )
    await backend.upsert_artifact(old)
    await backend.upsert_artifact(new)

    # Create a report since artifact_versions references reports(run_id)
    report = ReportRow(
        run_id="run001", started_at="2024-01-01T00:00:00Z",
        original_query="test", path_taken="quick", markdown="# test",
    )
    await backend.insert_report(report)

    await backend.insert_artifact_version(
        "old_id", "new_id", "content_changed", "run001"
    )
