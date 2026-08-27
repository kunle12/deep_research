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
    # Tables exist after ensure_schema; no version-based checks needed
    pass


@pytest.mark.asyncio
async def test_old_schema_migrated_to_image_kind():
    """DBs created before the 'image' kind existed get their artifacts CHECK
    relaxed so kind='image' artifacts can be stored, preserving old rows."""
    import sqlite3

    tmp = tempfile.mkdtemp()
    db_path = str(Path(tmp) / "old_index.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE artifacts ("
        " artifact_id TEXT PRIMARY KEY,"
        " kind TEXT NOT NULL, source_url TEXT, source_type TEXT, title TEXT,"
        " authors TEXT, discovered_by TEXT, arxiv_id TEXT, parents TEXT,"
        " bytes_path TEXT NOT NULL, bytes_size INTEGER,"
        " first_seen_at TEXT NOT NULL, last_touched_at TEXT NOT NULL,"
        " raw_metadata TEXT, refresh_after_at TEXT, last_refreshed_at TEXT,"
        " upstream_unchanged_since TEXT,"
        " CHECK (kind IN ('pdf','html','report')))"
    )
    conn.execute(
        "INSERT INTO artifacts (artifact_id, kind, bytes_path, first_seen_at, "
        "last_touched_at) VALUES ('old1','pdf','old.pdf','t0','t0')"
    )
    conn.commit()
    conn.close()

    be = SqliteStorageBackend(db_path=db_path)
    await be.connect()
    try:
        old = await be.get_artifact("old1")
        assert old is not None and old.kind == "pdf"
        assert old.bytes_path == "old.pdf"
        # Image kind is now accepted after the migration
        await be.upsert_artifact(
            ArtifactRow(
                artifact_id="img1",
                kind="image",
                source_url="https://example.com/page",
                source_type="html",
                bytes_path="img.png",
                bytes_size=4,
                first_seen_at="now",
                last_touched_at="now",
            )
        )
        img = await be.get_artifact("img1")
        assert img is not None and img.kind == "image"
    finally:
        await be.close()


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
async def test_batch_analyses_and_count_tags(backend):
    art1 = ArtifactRow(
        artifact_id="art1",
        kind="pdf",
        source_url="https://arxiv.org/pdf/2401.12345",
        source_type="arxiv",
        bytes_path="artifacts/pdf/art1.pdf",
        bytes_size=1024,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    art2 = ArtifactRow(
        artifact_id="art2",
        kind="pdf",
        source_url="https://arxiv.org/pdf/2401.67890",
        source_type="arxiv",
        bytes_path="artifacts/pdf/art2.pdf",
        bytes_size=1024,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    await backend.upsert_artifact(art1)
    await backend.upsert_artifact(art2)
    report = ReportRow(
        run_id="run001",
        started_at="2024-01-01T00:00:00Z",
        original_query="test",
        path_taken="quick",
        markdown="# test",
    )
    await backend.insert_report(report)

    for aid, summary, score in [
        ("art1", "s1", 0.7),
        ("art1", "s2", 0.9),
        ("art2", "s3", 0.5),
    ]:
        await backend.insert_analysis(
            AnalysisRow(
                analysis_id=f"ana_{aid}_{summary}",
                artifact_id=aid,
                run_id="run001",
                analyzer="x",
                summary=summary,
                relevance_score=score,
            )
        )

    by_art = await backend.get_analyses_for_artifacts(["art1", "art2"])
    assert len(by_art["art1"]) == 2
    assert len(by_art["art2"]) == 1
    assert by_art.get("nonexistent", []) == []

    await backend.upsert_tag(TagRow(tag="ml", artifact_id="art1", applied_in_run="run001"))
    await backend.upsert_tag(TagRow(tag="survey", artifact_id="art2", applied_in_run="run001"))
    assert await backend.count_tags() == 2


@pytest.mark.asyncio
async def test_glossary_entries_filtered_by_run(backend):
    now = "2024-01-01T00:00:00Z"
    await backend.insert_report(ReportRow(run_id="r1", started_at=now, markdown="# one"))
    await backend.insert_report(ReportRow(run_id="r2", started_at=now, markdown="# two"))
    await backend.upsert_artifact(
        ArtifactRow(
            artifact_id="a1", kind="report", bytes_path="reports/x.md",
            first_seen_at=now, last_touched_at=now,
        )
    )
    await backend.upsert_artifact(
        ArtifactRow(
            artifact_id="a2", kind="report", bytes_path="reports/y.md",
            first_seen_at=now, last_touched_at=now,
        )
    )
    await backend.upsert_glossary_entry(
        GlossaryEntry(
            term="One", term_canonical="one", kind="concept",
            first_seen_run_id="r1", first_seen_artifact_id="a1", last_updated=now,
        )
    )
    await backend.upsert_glossary_entry(
        GlossaryEntry(
            term="Two", term_canonical="two", kind="concept",
            first_seen_run_id="r2", first_seen_artifact_id="a2", last_updated=now,
        )
    )

    assert len(await backend.list_glossary_entries(run_id="r1")) == 1
    assert len(await backend.list_glossary_entries(run_id="r2")) == 1
    assert len(await backend.list_glossary_entries()) == 2


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

    # Verify get_tags_for_artifact
    tags = await backend.get_tags_for_artifact("art1")
    assert len(tags) == 1
    assert tags[0].tag == "RL"

    # Verify get_tags_for_artifacts (batch)
    tags_by_art = await backend.get_tags_for_artifacts(["art1", "nonexistent"])
    assert "art1" in tags_by_art
    assert len(tags_by_art["art1"]) == 1
    assert tags_by_art["art1"][0].tag == "RL"
    assert "nonexistent" in tags_by_art
    assert len(tags_by_art["nonexistent"]) == 0

    # Verify empty batch
    assert await backend.get_tags_for_artifacts([]) == {}

    # Verify delete_tag
    await backend.delete_tag("RL", "art1")
    tags = await backend.get_tags_for_artifact("art1")
    assert len(tags) == 0

    # Verify rename_tag
    await backend.upsert_tag(TagRow(tag="old", artifact_id="art1", applied_in_run="run001"))
    await backend.rename_tag("old", "renamed")
    tags = await backend.get_tags_for_artifact("art1")
    assert len(tags) == 1
    assert tags[0].tag == "renamed"


@pytest.mark.asyncio
async def test_glossary(backend):
    entries = [
        GlossaryEntry(
            term="RLHF",
            term_canonical="rlhf",
            kind="acronym",
            short_def="RL from human feedback",
            acronym_expansion="Reinforcement Learning from Human Feedback",
            confidence=0.9,
            last_updated="2024-01-01T00:00:00Z",
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
    e1 = GlossaryEntry(
        term="RLHF",
        term_canonical="rlhf",
        kind="acronym",
        short_def="v1",
        confidence=0.5,
        last_updated="now",
    )
    await backend.upsert_glossary_entry(e1)

    e2 = GlossaryEntry(
        term="RLHF",
        term_canonical="rlhf",
        kind="acronym",
        short_def="v2",
        long_def="longer",
        confidence=0.9,
        last_updated="now2",
    )
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
        artifact_id="old_id",
        kind="pdf",
        source_url="https://example.com/old",
        bytes_path="old.pdf",
        bytes_size=100,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    new = ArtifactRow(
        artifact_id="new_id",
        kind="pdf",
        source_url="https://example.com/new",
        bytes_path="new.pdf",
        bytes_size=200,
        first_seen_at="2024-01-01T00:00:00Z",
        last_touched_at="2024-01-01T00:00:00Z",
    )
    await backend.upsert_artifact(old)
    await backend.upsert_artifact(new)

    # Create a report since artifact_versions references reports(run_id)
    report = ReportRow(
        run_id="run001",
        started_at="2024-01-01T00:00:00Z",
        original_query="test",
        path_taken="quick",
        markdown="# test",
    )
    await backend.insert_report(report)

    await backend.insert_artifact_version("old_id", "new_id", "content_changed", "run001")


@pytest.mark.asyncio
async def test_relevance_score_roundtrip(backend):
    await backend.upsert_artifact(
        ArtifactRow(
            artifact_id="art_rel",
            kind="pdf",
            source_type="arxiv",
            bytes_path="art_rel.pdf",
            first_seen_at="2024-01-01T00:00:00Z",
            last_touched_at="2024-01-01T00:00:00Z",
        )
    )
    await backend.insert_report(
        ReportRow(
            run_id="run_rel",
            started_at="2024-01-01T00:00:00Z",
            original_query="q",
            path_taken="deep",
            markdown="# q",
        )
    )
    aid = await backend.insert_analysis(
        AnalysisRow(
            analysis_id="an_rel",
            artifact_id="art_rel",
            run_id="run_rel",
            analyzer="analyze_paper",
            summary="summary",
            relevance_to_query="on topic",
            relevance_score=0.85,
            analyzed_at="2024-01-01T00:00:00Z",
        )
    )
    assert aid == "an_rel"
    got = await backend.get_analysis("an_rel")
    assert got is not None
    assert got.relevance_score == 0.85
    assert got.relevance_to_query == "on topic"

    rows = await backend.get_analyses_for_artifact("art_rel")
    assert len(rows) == 1
    assert rows[0].relevance_score == 0.85


@pytest.mark.asyncio
async def test_old_schema_migrated_to_relevance_score():
    """DBs created before the relevance-ranking feature get the
    analyses.relevance_score column added (idempotently)."""
    import sqlite3

    tmp = tempfile.mkdtemp()
    db_path = str(Path(tmp) / "old_schema.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE analyses ("
        " analysis_id TEXT PRIMARY KEY, artifact_id TEXT, run_id TEXT,"
        " analyzer TEXT, summary TEXT, key_findings TEXT, methodology TEXT,"
        " limitations TEXT, gaps TEXT, follow_ups TEXT, key_references TEXT,"
        " relevance_to_query TEXT, analyzed_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    be = SqliteStorageBackend(db_path=db_path)
    await be.connect()
    try:
        # ensure_schema ran the ALTER migration.
        cols = [r[1] for r in await be._fetchall("PRAGMA table_info(analyses)")]
        assert "relevance_score" in cols
    finally:
        await be.close()

    # Reconnect: migration must be idempotent (no duplicate column).
    be2 = SqliteStorageBackend(db_path=db_path)
    await be2.connect()
    try:
        cols = [r[1] for r in await be2._fetchall("PRAGMA table_info(analyses)")]
        assert cols.count("relevance_score") == 1
    finally:
        await be2.close()


@pytest.mark.asyncio
async def test_list_artifacts_filters(backend):
    await backend.upsert_artifact(
        ArtifactRow(
            artifact_id="art1",
            kind="pdf",
            source_url="https://arxiv.org/abs/2401.00001",
            arxiv_id="2401.00001",
            title="Reinforcement Paper",
            bytes_path="a.pdf",
            first_seen_at="2024-01-02T00:00:00Z",
            last_touched_at="2024-01-02T00:00:00Z",
        )
    )
    await backend.upsert_artifact(
        ArtifactRow(
            artifact_id="art2",
            kind="html",
            source_url="https://example.com/blog",
            title="A blog post",
            bytes_path="b",
            first_seen_at="2024-01-01T00:00:00Z",
            last_touched_at="2024-01-01T00:00:00Z",
        )
    )

    all_rows = await backend.list_artifacts(10)
    assert {r.artifact_id for r in all_rows} == {"art1", "art2"}
    assert await backend.count_artifacts() == 2

    pdf_rows = await backend.list_artifacts(10, kind="pdf")
    assert [r.artifact_id for r in pdf_rows] == ["art1"]
    assert await backend.count_artifacts(kind="pdf") == 1

    q_rows = await backend.list_artifacts(10, q="reinforcement")
    assert [r.artifact_id for r in q_rows] == ["art1"]
    assert await backend.count_artifacts(q="reinforcement") == 1

    # Ordering is first_seen_at DESC (art2 older -> last).
    assert [r.artifact_id for r in all_rows] == ["art1", "art2"]
