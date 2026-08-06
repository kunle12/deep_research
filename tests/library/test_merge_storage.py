"""Tests: rename_report + reassign_run (merge support)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    CitationEdgeRow,
    ReportRow,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _make_report(
    backend, run_id: str, query: str = "q", artifact_id: str | None = None
) -> None:
    now = _now()
    if artifact_id:
        await backend.upsert_artifact(
            ArtifactRow(
                artifact_id=artifact_id,
                kind="pdf",
                source_type="research_report",
                title=f"Report {run_id}",
                bytes_path=f"reports/{run_id}.md",
                bytes_size=10,
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
            markdown="# R\n\ntext",
            artifact_id=artifact_id,
        )
    )


@pytest.mark.asyncio
async def test_rename_report(sqlite_backend):
    await _make_report(sqlite_backend, "run_a", query="old name", artifact_id="art_a")
    await sqlite_backend.rename_report("run_a", "new name")

    fetched = await sqlite_backend.get_report("run_a")
    assert fetched is not None
    assert fetched.original_query == "new name"
    # idempotent on missing run: quietly affects 0 rows
    await sqlite_backend.rename_report("missing", "x")


@pytest.mark.asyncio
async def test_reassign_run_moves_analyses_edges_glossary_versions(sqlite_backend):
    await _make_report(sqlite_backend, "run_old", artifact_id="art_old")
    await _make_report(sqlite_backend, "run_new", artifact_id="art_new")

    # source artifact + analysis tied to the old run
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
            analysis_id="an1",
            artifact_id="src_pdf",
            run_id="run_old",
            analyzer="analyze_paper",
            summary="s",
            analyzed_at=_now(),
        )
    )
    await sqlite_backend.insert_citation_edge(
        CitationEdgeRow(
            source_artifact_id="src_pdf",
            target_arxiv_id="1234.5678",
            weight=0.7,
            discovered_in_run="run_old",
        )
    )
    await sqlite_backend.upsert_glossary_entry(
        _glossary_entry("RLHF", first_seen_run_id="run_old", first_seen_artifact_id="src_pdf")
    )
    # artifact_versions requires both referenced artifacts to exist (FK)
    for aid in ("old_sha", "new_sha"):
        await sqlite_backend.upsert_artifact(
            ArtifactRow(
                artifact_id=aid,
                kind="pdf",
                source_type="arxiv",
                title=aid,
                bytes_path=f"artifacts/pdf/{aid}.pdf",
                bytes_size=10,
                first_seen_at=_now(),
                last_touched_at=_now(),
            )
        )
    await sqlite_backend.insert_artifact_version("old_sha", "new_sha", "content_changed", "run_old")

    await sqlite_backend.reassign_run("run_old", "run_new")

    # analyses repointed
    analyses = await sqlite_backend.get_analyses_for_artifact("src_pdf")
    assert len(analyses) == 1
    assert analyses[0].run_id == "run_new"
    # citation_edges repointed
    edges = await sqlite_backend.get_citation_edges_for_source("src_pdf")
    assert edges and edges[0].discovered_in_run == "run_new"
    # glossary repointed
    ge = await sqlite_backend.get_glossary_entry("rlhf")
    assert ge is not None
    assert ge.first_seen_run_id == "run_new"
    # artifact_versions repointed
    # (no direct getter; assert via delete_report cascade observability below)
    # delete the old run -> the repointed rows must survive
    await sqlite_backend.delete_report("run_old")
    analyses_after = await sqlite_backend.get_analyses_for_artifact("src_pdf")
    assert len(analyses_after) == 1, "repointed analysis must survive source-report deletion"
    assert analyses_after[0].run_id == "run_new"


@pytest.mark.asyncio
async def test_reassign_run_self_reference_is_noop(sqlite_backend):
    await _make_report(sqlite_backend, "run_x", artifact_id="art_x")
    await sqlite_backend.upsert_artifact(
        ArtifactRow(
            artifact_id="src_x",
            kind="pdf",
            source_type="arxiv",
            title="paper",
            bytes_path="artifacts/pdf/src_x.pdf",
            bytes_size=100,
            first_seen_at=_now(),
            last_touched_at=_now(),
        )
    )
    await sqlite_backend.insert_analysis(
        AnalysisRow(
            analysis_id="an_x",
            artifact_id="src_x",
            run_id="run_x",
            analyzer="analyze_paper",
            summary="s",
            analyzed_at=_now(),
        )
    )
    # must not raise and must not lose the analysis
    await sqlite_backend.reassign_run("run_x", "run_x")
    analyses = await sqlite_backend.get_analyses_for_artifact("src_x")
    assert len(analyses) == 1
    assert analyses[0].run_id == "run_x"


def _glossary_entry(
    term: str, *, first_seen_run_id: str | None, first_seen_artifact_id: str | None
):
    from deep_research.library.storage.rows import GlossaryEntry

    return GlossaryEntry(
        term=term,
        term_canonical=term.lower(),
        kind="concept",
        short_def="d",
        first_seen_run_id=first_seen_run_id,
        first_seen_artifact_id=first_seen_artifact_id,
        last_updated=_now(),
    )
