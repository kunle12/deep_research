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
    assert await nw.archive_image("https://example.com", b"\x89PNG") == ""
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
async def test_archive_pdf_distinct_content_no_collision(writer, tmp_path):
    """Two different PDFs without an arxiv_id but with the same title must not
    collide on the same on-disk file (dest is sha-derived)."""
    pdf1 = tmp_path / "one.pdf"
    pdf2 = tmp_path / "two.pdf"
    pdf1.write_bytes(b"%PDF first")
    pdf2.write_bytes(b"%PDF second")

    aid1 = await writer.archive_pdf(pdf1, title="Same Title", source_type="pdf")
    aid2 = await writer.archive_pdf(pdf2, title="Same Title", source_type="pdf")
    assert aid1 and aid2 and aid1 != aid2

    art1 = await writer.storage.get_artifact(aid1)
    art2 = await writer.storage.get_artifact(aid2)
    assert art1 is not None and art2 is not None
    assert art1.bytes_path != art2.bytes_path
    # Each stored file must actually contain its own content.
    root = writer.root_dir
    assert (root / art1.bytes_path).read_bytes() == b"%PDF first"
    assert (root / art2.bytes_path).read_bytes() == b"%PDF second"


@pytest.mark.asyncio
async def test_archive_image(writer):
    """A webpage screenshot is archived as an image artifact with the PNG bytes."""
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"fake image payload"
    aid = await writer.archive_image("https://example.com/page", png_bytes)
    assert aid
    art = await writer.storage.get_artifact(aid)
    assert art is not None
    assert art.kind == "image"
    assert art.source_type == "html"
    assert art.source_url == "https://example.com/page"
    assert (writer.root_dir / art.bytes_path).read_bytes() == png_bytes


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
async def test_begin_report_placeholder_then_archive(writer):
    """begin_report creates the reports row up-front; archive_report then
    overwrites the placeholder fields. Idempotent: calling begin_report again
    must NOT clobber an already-completed report."""
    from datetime import UTC, datetime

    await writer.begin_report("run_b", "test query")
    placeholder = await writer.storage.get_report("run_b")
    assert placeholder is not None
    assert placeholder.markdown == ""  # placeholder not yet set

    report = Report(
        markdown="# Done\n\ncontent",
        citations=[],
        path="quick",
        classifier_rationale="test",
        created_at=datetime.now(UTC),
    )
    await writer.archive_report(report, "run_b")
    archived = await writer.storage.get_report("run_b")
    assert archived.markdown == "# Done\n\ncontent"
    assert archived.path_taken == "quick"

    # begin_report again must not revert the completed report to placeholder
    await writer.begin_report("run_b", "test query")
    again = await writer.storage.get_report("run_b")
    assert again.markdown == "# Done\n\ncontent"


@pytest.mark.asyncio
async def test_begin_report_enables_glossary_fresh_run(writer):
    """Regression: on a fresh run the glossary upsert used to fail the FK
    `first_seen_run_id -> reports(run_id)` because the reports row was only
    created by archive_report (after extraction). begin_report fixes that."""
    import json
    from types import SimpleNamespace

    from deep_research.nodes.glossarize import extract_glossary_from_report

    async def _create(**kw):
        payload = json.dumps(
            {"glossary": [{"term": "RLHF", "kind": "acronym", "short_def": "RL from feedback"}]}
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
        )

    fake_llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))



    await writer.begin_report("run_g", "glossary query")
    entries = await extract_glossary_from_report(
        "# Report about RLHF", fake_llm, "gpt-test", writer, "run_g"
    )

    assert len(entries) == 1, "glossary extraction should persist on a fresh run"
    rows = await writer.storage.list_glossary_entries()
    assert any(r.term_canonical == "rlhf" for r in rows)
    rlhf = next(r for r in rows if r.term_canonical == "rlhf")
    assert rlhf.first_seen_run_id == "run_g"



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

    from deep_research.library.storage.rows import ArtifactRow, ReportRow

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
    # Insert a report row so the FK constraint on applied_in_run is satisfied
    report = ReportRow(run_id="test_run", started_at=now, markdown="# Report")
    await writer.storage.insert_report(report)

    await writer.tag("art1", ["test-tag", "another-tag"], run_id="test_run")
    tags = await writer.storage.get_tags_for_artifact("art1")
    assert len(tags) == 2
    assert tags[0].tag in ("test-tag", "another-tag")
    assert tags[0].applied_in_run == "test_run"
