"""Tests for merge_reports (library/merge.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    ReportRow,
    TagRow,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fake_llm(content: str):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    return mock_client


async def _seed_report(
    backend,
    *,
    run_id: str,
    query: str,
    markdown: str,
    artifact_id: str,
    citations: list | None = None,
    tags: list[str] | None = None,
) -> None:
    now = _now()
    await backend.upsert_artifact(
        ArtifactRow(
            artifact_id=artifact_id,
            kind="pdf",
            source_type="research_report",
            title=f"Report {run_id}",
            bytes_path=f"reports/{run_id}.md",
            bytes_size=len(markdown),
            first_seen_at=now,
            last_touched_at=now,
        )
    )
    await backend.insert_report(
        ReportRow(
            run_id=run_id,
            started_at=now,
            completed_at=now,
            original_query=query,
            path_taken="deep",
            markdown=markdown,
            artifact_id=artifact_id,
            citations_json=json.dumps(citations) if citations else None,
        )
    )
    if tags:
        for t in tags:
            await backend.upsert_tag(TagRow(tag=t, artifact_id=artifact_id, applied_in_run=run_id))


@pytest.mark.asyncio
async def test_merge_requires_two_reports(sqlite_backend, tmp_path):
    from deep_research.library.merge import merge_reports
    from deep_research.library.writer import LibraryWriter

    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    with pytest.raises(ValueError, match="at least two"):
        await merge_reports(sqlite_backend, writer, ["run_a"], None, "model")


@pytest.mark.asyncio
async def test_merge_rejects_missing_report(sqlite_backend, tmp_path):
    from deep_research.library.merge import merge_reports
    from deep_research.library.writer import LibraryWriter

    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    await _seed_report(
        sqlite_backend,
        run_id="run_a",
        query="topic a",
        markdown="# A",
        artifact_id="art_a",
    )
    with pytest.raises(ValueError, match="not found"):
        await merge_reports(sqlite_backend, writer, ["run_a", "missing"], None, "model")


@pytest.mark.asyncio
async def test_merge_llm_synthesis(sqlite_backend, tmp_path):
    from deep_research.library.merge import merge_reports
    from deep_research.library.writer import LibraryWriter

    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    await _seed_report(
        sqlite_backend,
        run_id="run_a",
        query="topic a",
        markdown="# A\n\ncontent a",
        artifact_id="art_a",
        tags=["tag-a"],
    )
    await _seed_report(
        sqlite_backend,
        run_id="run_b",
        query="topic b",
        markdown="# B\n\ncontent b",
        artifact_id="art_b",
        tags=["tag-b"],
    )

    llm = _fake_llm("# Merged Topic\n\nSynthesized content.")
    new_id = await merge_reports(
        sqlite_backend, writer, ["run_a", "run_b"], llm, "model", name="Merged Topic"
    )

    report = await sqlite_backend.get_report(new_id)
    assert report is not None
    assert report.original_query == "Merged Topic"
    assert report.path_taken == "merged"
    assert "Merged from" in report.markdown
    assert "Synthesized content." in report.markdown

    # Tags from both sources copied to merged artifact
    tags = await sqlite_backend.get_tags_for_artifact(report.artifact_id)
    assert {t.tag for t in tags} == {"tag-a", "tag-b"}

    # Sources kept by default + tagged merged
    for rid in ("run_a", "run_b"):
        src = await sqlite_backend.get_report(rid)
        assert src is not None
        src_tags = await sqlite_backend.get_tags_for_artifact(src.artifact_id)
        assert "merged" in {t.tag for t in src_tags}


@pytest.mark.asyncio
async def test_merge_auto_name_when_unspecified(sqlite_backend, tmp_path):
    from deep_research.library.merge import merge_reports
    from deep_research.library.writer import LibraryWriter

    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    await _seed_report(
        sqlite_backend, run_id="run_a", query="Foo", markdown="# A", artifact_id="art_a"
    )
    await _seed_report(
        sqlite_backend, run_id="run_b", query="Bar", markdown="# B", artifact_id="art_b"
    )

    new_id = await merge_reports(
        sqlite_backend, writer, ["run_a", "run_b"], _fake_llm("# Merged"), "model"
    )
    report = await sqlite_backend.get_report(new_id)
    assert report is not None
    assert "Foo" in report.original_query and "Bar" in report.original_query


@pytest.mark.asyncio
async def test_merge_stitch_fallback_on_llm_failure(sqlite_backend, tmp_path):
    from deep_research.library.merge import merge_reports
    from deep_research.library.writer import LibraryWriter

    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    await _seed_report(
        sqlite_backend,
        run_id="run_a",
        query="topic a",
        markdown="# A\n\ncontent a",
        artifact_id="art_a",
    )
    await _seed_report(
        sqlite_backend,
        run_id="run_b",
        query="topic b",
        markdown="# B\n\ncontent b",
        artifact_id="art_b",
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=Exception("boom"))
    new_id = await merge_reports(
        sqlite_backend, writer, ["run_a", "run_b"], mock_client, "model", name="Merged"
    )
    report = await sqlite_backend.get_report(new_id)
    assert report is not None
    # Fallback stitch includes both sources under sub-headers
    assert "topic a" in report.markdown
    assert "topic b" in report.markdown
    assert "content a" in report.markdown
    assert "content b" in report.markdown


@pytest.mark.asyncio
async def test_merge_dedups_citations(sqlite_backend, tmp_path):
    from deep_research.library.merge import merge_reports
    from deep_research.library.writer import LibraryWriter

    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    cit = [
        {
            "url": "https://example.com/paper",
            "title": "Paper",
            "source_type": "web",
            "accessed_at": "2026-01-01T00:00:00Z",
        }
    ]
    await _seed_report(
        sqlite_backend,
        run_id="run_a",
        query="topic a",
        markdown="# A",
        artifact_id="art_a",
        citations=cit,
    )
    await _seed_report(
        sqlite_backend,
        run_id="run_b",
        query="topic b",
        markdown="# B",
        artifact_id="art_b",
        citations=cit,  # same URL -> should dedupe
    )

    new_id = await merge_reports(
        sqlite_backend, writer, ["run_a", "run_b"], _fake_llm("# Merged"), "model"
    )
    report = await sqlite_backend.get_report(new_id)
    merged = json.loads(report.citations_json)
    assert len(merged) == 1
    assert merged[0]["url"] == "https://example.com/paper"


@pytest.mark.asyncio
async def test_merge_delete_sources_reassigns_and_deletes(sqlite_backend, tmp_path):
    from deep_research.library.merge import merge_reports
    from deep_research.library.writer import LibraryWriter

    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    await _seed_report(
        sqlite_backend, run_id="run_a", query="topic a", markdown="# A", artifact_id="art_a"
    )
    await _seed_report(
        sqlite_backend, run_id="run_b", query="topic b", markdown="# B", artifact_id="art_b"
    )
    # An analysis tied to run_a — should survive via reassignment.
    await sqlite_backend.upsert_artifact(
        ArtifactRow(
            artifact_id="src_pdf",
            kind="pdf",
            source_type="arxiv",
            title="paper",
            bytes_path="artifacts/pdf/src_pdf.pdf",
            bytes_size=100,
            first_seen_at=_now(),
            last_touched_at=_now(),
        )
    )
    await sqlite_backend.insert_analysis(
        AnalysisRow(
            analysis_id="an_a",
            artifact_id="src_pdf",
            run_id="run_a",
            analyzer="analyze_paper",
            summary="paper summary",
            analyzed_at=_now(),
        )
    )

    new_id = await merge_reports(
        sqlite_backend,
        writer,
        ["run_a", "run_b"],
        _fake_llm("# Merged"),
        "model",
        name="Merged",
        delete_sources=True,
    )

    # Sources gone
    assert await sqlite_backend.get_report("run_a") is None
    assert await sqlite_backend.get_report("run_b") is None
    # Analysis reassigned to the merged run
    analyses = await sqlite_backend.get_analyses_for_artifact("src_pdf")
    assert len(analyses) == 1
    assert analyses[0].run_id == new_id
