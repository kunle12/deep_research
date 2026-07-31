"""Conformance tests: artifact versions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deep_research.library.storage.rows import ArtifactRow


@pytest.mark.asyncio
async def test_insert_artifact_version(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    # Create parent artifacts first (FK constraint)
    art1 = ArtifactRow(
        artifact_id="old_art",
        kind="pdf",
        source_type="arxiv",
        title="Old Version",
        discovered_by="arxiv",
        bytes_path="old.pdf",
        bytes_size=100,
        first_seen_at=now,
        last_touched_at=now,
    )
    art2 = ArtifactRow(
        artifact_id="new_art",
        kind="pdf",
        source_type="arxiv",
        title="New Version",
        discovered_by="arxiv",
        bytes_path="new.pdf",
        bytes_size=200,
        first_seen_at=now,
        last_touched_at=now,
    )
    await sqlite_backend.upsert_artifact(art1)
    await sqlite_backend.upsert_artifact(art2)

    # Insert a report first (FK constraint for discovered_in_run)
    from deep_research.library.storage.rows import ReportRow

    report = ReportRow(
        run_id="run_001",
        started_at=now,
        original_query="test",
        path_taken="deep",
        markdown="# Test",
        artifact_id="old_art",
    )
    await sqlite_backend.insert_report(report)

    await sqlite_backend.insert_artifact_version(
        old_id="old_art",
        new_id="new_art",
        reason="content_changed",
        run_id="run_001",
    )


@pytest.mark.asyncio
async def test_insert_duplicate_version(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    art1 = ArtifactRow(
        artifact_id="old_art",
        kind="pdf",
        source_type="arxiv",
        title="Old",
        discovered_by="arxiv",
        bytes_path="old.pdf",
        bytes_size=100,
        first_seen_at=now,
        last_touched_at=now,
    )
    art2 = ArtifactRow(
        artifact_id="new_art",
        kind="pdf",
        source_type="arxiv",
        title="New",
        discovered_by="arxiv",
        bytes_path="new.pdf",
        bytes_size=200,
        first_seen_at=now,
        last_touched_at=now,
    )
    await sqlite_backend.upsert_artifact(art1)
    await sqlite_backend.upsert_artifact(art2)

    from deep_research.library.storage.rows import ReportRow

    for rid in ("run_001", "run_002"):
        r = ReportRow(
            run_id=rid,
            started_at=now,
            original_query="test",
            path_taken="deep",
            markdown="# Test",
            artifact_id="old_art",
        )
        await sqlite_backend.insert_report(r)

    await sqlite_backend.insert_artifact_version(
        old_id="old_art",
        new_id="new_art",
        reason="content_changed",
        run_id="run_001",
    )
    await sqlite_backend.insert_artifact_version(
        old_id="old_art",
        new_id="new_art",
        reason="url_moved",
        run_id="run_002",
    )
