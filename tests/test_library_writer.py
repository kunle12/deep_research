"""Tests for LibraryWriter (P10.5a)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from deep_research.library.storage.rows import GlossaryEntry
from deep_research.library.storage.sqlite_backend import SqliteStorageBackend
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.state import Citation, Report


@pytest.fixture
async def writer():
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    db_path = str(root / "index.db")
    backend = SqliteStorageBackend(db_path=db_path)
    await backend.connect()
    w = LibraryWriter(backend, str(root))
    w.set_run_id("test_run")
    yield w
    await backend.close()


@pytest.mark.asyncio
async def test_null_writer():
    nw = NullLibraryWriter()
    assert await nw.archive_pdf(Path("/nonexistent")) == ""
    assert await nw.archive_report(Report(markdown="test", path="quick"), "run1") == ""
    assert await nw.upsert_glossary_entries([], "run1") == 0
    assert await nw.refresh_needed("test") == []
    result = await nw.run_refresh_job("source_type", "arxiv")
    assert result["considered"] == 0


@pytest.mark.asyncio
async def test_archive_pdf(writer, tmp_path):
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF test content")
    aid = await writer.archive_pdf(
        pdf_path,
        arxiv_id="2401.12345",
        source_url="https://arxiv.org/pdf/2401.12345",
        title="Test Paper",
    )
    assert aid  # non-empty sha


@pytest.mark.asyncio
async def test_archive_report(writer):
    report = Report(
        markdown="# Test Report\n\nContent",
        citations=[
            Citation(url="https://example.com", title="Test", snippet="test", confidence_score=0.8)
        ],
        path="quick",
        classifier_rationale="test",
    )
    aid = await writer.archive_report(report, "test_run", {"key": "value"})
    assert aid


@pytest.mark.asyncio
async def test_record_analysis(writer):
    # First create an artifact so the analysis can reference it
    from datetime import UTC, datetime

    from deep_research.library.storage.rows import ArtifactRow

    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="art1",
        kind="pdf",
        source_url="https://example.com/paper.pdf",
        source_type="arxiv",
        bytes_path="artifacts/pdf/art1.pdf",
        bytes_size=1024,
        first_seen_at=now,
        last_touched_at=now,
    )
    await writer.storage.upsert_artifact(art)

    # Create a report first since analyses references reports(run_id)
    from deep_research.library.storage.rows import ReportRow

    report_row = ReportRow(
        run_id="test_run",
        started_at=now,
        original_query="test query",
        path_taken="quick",
        markdown="# test",
    )
    await writer.storage.insert_report(report_row)

    analysis_dict = {
        "summary": "Test analysis",
        "key_findings": ["finding1", "finding2"],
        "methodology": "test method",
    }
    aid = await writer.record_analysis("art1", analysis_dict, "test_run", "analyze_paper")
    assert aid


@pytest.mark.asyncio
async def test_upsert_glossary_entries(writer):
    entries = [
        GlossaryEntry(
            term="RLHF",
            term_canonical="rlhf",
            kind="acronym",
            short_def="RL from human feedback",
            acronym_expansion="Reinforcement Learning from Human Feedback",
            confidence=0.9,
            last_updated="now",
        ),
    ]
    count = await writer.upsert_glossary_entries(entries, "test_run")
    assert count == 1


@pytest.mark.asyncio
async def test_refresh_job(writer):
    result = await writer.run_refresh_job("source_type", "arxiv", dry_run=True)
    assert isinstance(result, dict)
    assert "job_id" in result
    assert result["considered"] == 0


@pytest.mark.asyncio
async def test_tag(writer):
    # First create an artifact
    from datetime import UTC, datetime

    from deep_research.library.storage.rows import ArtifactRow

    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="art1",
        kind="pdf",
        source_url="https://example.com/paper.pdf",
        source_type="arxiv",
        bytes_path="artifacts/pdf/art1.pdf",
        bytes_size=1024,
        first_seen_at=now,
        last_touched_at=now,
    )
    await writer.storage.upsert_artifact(art)

    await writer.tag("art1", ["test-tag", "another-tag"], run_id="test_run")
    tags = await writer.storage.get_tags_for_artifact("art1")
    assert len(tags) == 2
    assert tags[0].tag in ("test-tag", "another-tag")
    assert tags[0].applied_in_run == "test_run"
