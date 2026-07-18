"""Conformance tests: report CRUD."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deep_research.library.storage.rows import ArtifactRow, ReportRow


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
