"""Conformance tests: report CRUD."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deep_research.library.storage.rows import ArtifactRow, ReportRow, TagRow


@pytest.mark.asyncio
async def test_insert_and_get_report(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="rep_art_1",
        kind="pdf",
        source_type="research_report",
        title="Test Report",
        discovered_by="research",
        bytes_path="reports/test.md",
        bytes_size=1000,
        first_seen_at=now,
        last_touched_at=now,
    )
    await sqlite_backend.upsert_artifact(art)

    report = ReportRow(
        run_id="run_001",
        started_at=now,
        completed_at=now,
        original_query="test query",
        path_taken="deep",
        markdown="# Test Report\n\nContent.",
        artifact_id="rep_art_1",
    )
    await sqlite_backend.insert_report(report)

    fetched = await sqlite_backend.get_report("run_001")
    assert fetched is not None
    assert fetched.run_id == "run_001"
    assert fetched.original_query == "test query"
    assert fetched.markdown == "# Test Report\n\nContent."


@pytest.mark.asyncio
async def test_list_reports(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="rep_art_2",
        kind="pdf",
        source_type="research_report",
        title="Reports",
        discovered_by="research",
        bytes_path="reports/",
        bytes_size=500,
        first_seen_at=now,
        last_touched_at=now,
    )
    await sqlite_backend.upsert_artifact(art)

    for i in range(3):
        r = ReportRow(
            run_id=f"run_{i:03d}",
            started_at=now,
            original_query=f"query {i}",
            path_taken="quick",
            markdown=f"# Report {i}",
            artifact_id="rep_art_2",
        )
        await sqlite_backend.insert_report(r)

    reports = await sqlite_backend.list_reports(limit=10)
    assert len(reports) >= 3


@pytest.mark.asyncio
async def test_get_missing_report(sqlite_backend):
    missing = await sqlite_backend.get_report("nonexistent")
    assert missing is None


async def test_list_reports_pagination_and_filters(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    for aid in ("rep_art_ml", "rep_art_plain"):
        art = ArtifactRow(
            artifact_id=aid,
            kind="pdf",
            source_type="research_report",
            title="Reports",
            bytes_path="reports/",
            bytes_size=500,
            first_seen_at=now,
            last_touched_at=now,
        )
        await sqlite_backend.upsert_artifact(art)

    for i in range(5):
        r = ReportRow(
            run_id=f"pg_{i:03d}",
            started_at=f"2026-07-0{i + 1}T10:00:00Z",
            original_query=f"query {i}",
            path_taken="deep" if i % 2 else "quick",
            markdown=f"# Report {i}\n\ncontent {i}",
            artifact_id="rep_art_ml" if i == 0 else "rep_art_plain",
        )
        await sqlite_backend.insert_report(r)
    await sqlite_backend.upsert_tag(
        TagRow(tag="ml", artifact_id="rep_art_ml", applied_in_run="pg_000")
    )

    page1 = await sqlite_backend.list_reports(limit=2, offset=0)
    page2 = await sqlite_backend.list_reports(limit=2, offset=2)
    assert [r.run_id for r in page1] == ["pg_004", "pg_003"]
    assert [r.run_id for r in page2] == ["pg_002", "pg_001"]

    assert await sqlite_backend.count_reports() == 5
    assert await sqlite_backend.count_reports(tag="ml") == 1
    assert await sqlite_backend.count_reports(path="deep") == 2

    deep = await sqlite_backend.list_reports(limit=10, path="deep")
    assert {r.run_id for r in deep} == {"pg_001", "pg_003"}
    tagged = await sqlite_backend.list_reports(limit=10, tag="ml")
    assert [r.run_id for r in tagged] == ["pg_000"]
    both = await sqlite_backend.list_reports(limit=10, tag="ml", path="quick")
    assert [r.run_id for r in both] == ["pg_000"]


async def test_search_reports(sqlite_backend):
    art = ArtifactRow(
        artifact_id="rep_art_sr",
        kind="pdf",
        source_type="research_report",
        title="Search Reports",
        bytes_path="reports/",
        bytes_size=500,
        first_seen_at="2026-07-01T00:00:00Z",
        last_touched_at="2026-07-01T00:00:00Z",
    )
    await sqlite_backend.upsert_artifact(art)

    await sqlite_backend.insert_report(
        ReportRow(
            run_id="sr_a",
            started_at="2026-07-01T10:00:00Z",
            original_query="transformer survey",
            path_taken="deep",
            markdown="# A\n\nAttention is all you need.",
            artifact_id="rep_art_sr",
        )
    )
    await sqlite_backend.insert_report(
        ReportRow(
            run_id="sr_b",
            started_at="2026-07-02T10:00:00Z",
            original_query="other topic",
            path_taken="quick",
            markdown="# B\n\nno match here",
            artifact_id="rep_art_sr",
        )
    )
    await sqlite_backend.insert_report(
        ReportRow(
            run_id="sr_c",
            started_at="2026-07-03T10:00:00Z",
            original_query="percent",
            path_taken="quick",
            markdown="# C\n\ncoverage is 100% complete",
            artifact_id="rep_art_sr",
        )
    )

    hits = await sqlite_backend.search_reports("attention", limit=10)
    assert [h.run_id for h in hits] == ["sr_a"]
    # Case-insensitive matching
    hits = await sqlite_backend.search_reports("TRANSFORMER", limit=10)
    assert [h.run_id for h in hits] == ["sr_a"]
    # LIKE wildcards in user input are escaped
    hits = await sqlite_backend.search_reports("100%", limit=10)
    assert [h.run_id for h in hits] == ["sr_c"]
    assert await sqlite_backend.search_reports("nomatch", limit=10) == []
    # Combined with tag/path filters
    await sqlite_backend.upsert_tag(
        TagRow(tag="ml", artifact_id="rep_art_sr", applied_in_run="sr_a")
    )
    hits = await sqlite_backend.search_reports("attention", limit=10, path="deep", tag="ml")
    assert [h.run_id for h in hits] == ["sr_a"]
    hits = await sqlite_backend.search_reports("attention", limit=10, path="quick")
    assert hits == []


async def test_count_artifacts_and_batch_get(sqlite_backend):
    a1 = ArtifactRow(
        artifact_id="ca1",
        kind="pdf",
        source_type="arxiv",
        title="One",
        bytes_path="artifacts/pdf/ca1.pdf",
        bytes_size=10,
        first_seen_at="2026-07-01T00:00:00Z",
        last_touched_at="2026-07-01T00:00:00Z",
    )
    a2 = ArtifactRow(
        artifact_id="ca2",
        kind="html",
        source_type="blog",
        title="Two",
        bytes_path="artifacts/html/ca2",
        bytes_size=20,
        first_seen_at="2026-07-01T00:00:00Z",
        last_touched_at="2026-07-01T00:00:00Z",
    )
    await sqlite_backend.upsert_artifact(a1)
    await sqlite_backend.upsert_artifact(a2)

    assert await sqlite_backend.count_artifacts() == 2
    fetched = await sqlite_backend.get_artifacts(["ca1", "ca2", "missing"])
    assert set(fetched) == {"ca1", "ca2"}
    assert fetched["ca2"].kind == "html"
    assert await sqlite_backend.get_artifacts([]) == {}


async def test_list_tags(sqlite_backend):
    for aid in ("tag_a", "tag_b"):
        art = ArtifactRow(
            artifact_id=aid,
            kind="pdf",
            source_type="arxiv",
            title=aid,
            bytes_path=f"artifacts/pdf/{aid}.pdf",
            bytes_size=10,
            first_seen_at="2026-07-01T00:00:00Z",
            last_touched_at="2026-07-01T00:00:00Z",
        )
        await sqlite_backend.upsert_artifact(art)
    await sqlite_backend.upsert_tag(TagRow(tag="ml", artifact_id="tag_a", applied_in_run=None))
    await sqlite_backend.upsert_tag(TagRow(tag="ml", artifact_id="tag_b", applied_in_run=None))
    await sqlite_backend.upsert_tag(TagRow(tag="survey", artifact_id="tag_a", applied_in_run=None))

    tags = await sqlite_backend.list_tags()
    assert ("ml", 2) in tags
    assert ("survey", 1) in tags
    assert tags[0] == ("ml", 2)  # ordered by count DESC
    limited = await sqlite_backend.list_tags(limit=1)
    assert limited == [("ml", 2)]
