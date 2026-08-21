"""Tests for attach_source (library/attach.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    ReportRow,
    TagRow,
)
from deep_research.state import SourceAnalysis


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _seed_report(backend, *, run_id: str = "run_t", query: str = "target topic") -> None:
    now = _now()
    art = ArtifactRow(
        artifact_id="rep_art",
        kind="pdf",
        source_type="research_report",
        title="Report",
        bytes_path="reports/run_t.md",
        bytes_size=100,
        first_seen_at=now,
        last_touched_at=now,
    )
    await backend.upsert_artifact(art)
    await backend.insert_report(
        ReportRow(
            run_id=run_id,
            started_at=now,
            completed_at=now,
            original_query=query,
            path_taken="deep",
            markdown="# Target\n\nSome content.\n\n## Bibliography\n\n- nothing",
            artifact_id="rep_art",
            config_snapshot=json.dumps({"a": 1}),
        )
    )
    await backend.upsert_tag(TagRow(tag="keepme", artifact_id="rep_art", applied_in_run=run_id))


def _fake_source_analysis(title: str = "New Paper") -> SourceAnalysis:
    return SourceAnalysis(
        title=title,
        summary="summary of new source",
        key_claims=[{"claim": "c1", "evidence": "e1"}],
        limitations=["lim"],
        gaps=["gap"],
        relevance_to_query="very relevant",
    )


async def _run_attach(
    backend, writer, *, analysis=None, fetch_error="", no_artifact=False, force=False
):
    """Run attach_source with fetch/analyze/tools all stubbed out."""
    # The real fetch_source archives the artifact before analysis; mirror that.
    if not no_artifact:
        await backend.upsert_artifact(
            ArtifactRow(
                artifact_id="art_src",
                kind="html",
                source_type="html",
                title="New Paper",
                source_url="https://example.com/new",
                bytes_path="artifacts/html/art_src",
                bytes_size=10,
                first_seen_at=_now(),
                last_touched_at=_now(),
            )
        )

    fake_fetched = MagicMock()
    fake_fetched.url = "https://example.com/new"
    fake_fetched.url_type = MagicMock()
    fake_fetched.url_type.value = "html"
    fake_fetched.arxiv_id = None
    fake_fetched.content_text = "the article body text"
    fake_fetched.fetch_error = fetch_error
    fake_fetched.page_image_data_urls = []
    fake_fetched.citations = [
        {
            "url": "https://example.com/new",
            "title": "New Paper",
            "source_type": "html",
            "accessed_at": "2026-01-01T00:00:00Z",
        }
    ]
    fake_fetched.artifact_id = "art_src" if not no_artifact else ""
    # Citation list must hold Citation objects (attach merges via Citation(**raw)).
    from deep_research.state import Citation

    fake_fetched.citations = [
        Citation(url="https://example.com/new", title="New Paper", source_type="html")
    ]

    cfg = MagicMock()
    cfg.url_source.head_probe_timeout_s = 5
    cfg.url_source.attach_relevance_threshold = 0.4
    cfg.pdf_vision.enabled = False
    cfg.arxiv.pdf_cache_dir = "/tmp/cache"
    cfg.fetch_page.archive_org_fallback = True
    cfg.llm.vision_model = "vision"
    cfg.llm.text_model = "text"

    llm = MagicMock()

    router = MagicMock()
    router.resolve.return_value = MagicMock(client=llm, model="text", max_context_tokens=131072)

    with (
        patch("deep_research.library.attach.fetch_source", AsyncMock(return_value=fake_fetched)),
        (
            patch(
                "deep_research.library.attach.analyze_source_node",
                AsyncMock(return_value=analysis or _fake_source_analysis()),
            )
        ),
        patch("deep_research.library.attach._build_tools") as mock_build,
    ):
        mock_reg = MagicMock()
        mock_build.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
        mock_build.return_value.__aexit__ = AsyncMock(return_value=False)

        from deep_research.library.attach import attach_source

        return await attach_source(
            "https://example.com/new", "run_t", backend, writer, cfg, router, force=force
        )


@pytest.mark.asyncio
async def test_attach_requires_existing_report(sqlite_backend, tmp_path):
    from deep_research.library.attach import attach_source
    from deep_research.library.writer import LibraryWriter

    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    cfg = MagicMock()
    with pytest.raises(ValueError, match="not found"):
        await attach_source("https://x.test", "missing", sqlite_backend, writer, cfg, None)


@pytest.mark.asyncio
async def test_attach_records_analysis_and_updates_report(sqlite_backend, tmp_path):
    from deep_research.library.writer import LibraryWriter

    await _seed_report(sqlite_backend)
    writer = LibraryWriter(sqlite_backend, str(tmp_path))

    result = await _run_attach(sqlite_backend, writer)

    assert result["status"] == "attached"
    assert result["artifact_id"] == "art_src"

    # Analysis recorded against the target run with flattened key_findings
    analyses = await sqlite_backend.get_analyses_for_artifact("art_src")
    assert len(analyses) == 1
    assert analyses[0].run_id == "run_t"
    assert analyses[0].analyzer == "analyze_source"
    assert "summary of new source" in (analyses[0].summary or "")
    kf = json.loads(analyses[0].key_findings or "[]")
    assert any("c1" in str(x) for x in kf)

    # Report markdown gained the section BEFORE the bibliography
    report = await sqlite_backend.get_report("run_t")
    assert report is not None
    assert "## Added source: New Paper" in report.markdown
    bib_pos = report.markdown.index("## Bibliography")
    added_pos = report.markdown.index("## Added source")
    assert added_pos < bib_pos

    # Citations merged
    merged = json.loads(report.citations_json)
    assert any(c["url"] == "https://example.com/new" for c in merged)


@pytest.mark.asyncio
async def test_attach_keeps_report_files_on_same_day(sqlite_backend, tmp_path):
    """Regression: re-archive writes new files under today's dir; a post-archive
    stale-file cleanup with a same-day completed_at would delete them (the
    report's artifact would point at a missing file)."""
    from datetime import UTC, datetime
    from pathlib import Path

    from deep_research.library.writer import LibraryWriter

    # Seed a report whose completed_at is TODAY (same day as the attach runs).
    now = datetime.now(UTC)
    _now_iso = now.isoformat()
    await sqlite_backend.upsert_artifact(
        ArtifactRow(
            artifact_id="rep_art",
            kind="pdf",
            source_type="research_report",
            title="Report",
            bytes_path="reports/run_t.md",
            bytes_size=100,
            first_seen_at=_now_iso,
            last_touched_at=_now_iso,
        )
    )
    await sqlite_backend.insert_report(
        ReportRow(
            run_id="run_t",
            started_at=_now_iso,
            completed_at=_now_iso,
            original_query="target topic",
            path_taken="deep",
            markdown="# Target\n\n## Bibliography\n\n- nothing",
            artifact_id="rep_art",
        )
    )
    # Pre-existing on-disk files under today's dir (as a completed run would have).
    day_dir = Path(tmp_path) / "reports" / str(now.year) / f"{now.month:02d}" / f"{now.day:02d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "run_t.md").write_text("# Target", encoding="utf-8")
    (day_dir / "run_t.pdf").write_bytes(b"%PDF-fake")

    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    await _run_attach(sqlite_backend, writer)

    # The freshly re-archived report files must still exist on disk.
    assert (day_dir / "run_t.md").is_file()
    assert (day_dir / "run_t.pdf").is_file()
    # And the report row still points at an artifact whose bytes are present.
    report = await sqlite_backend.get_report("run_t")
    art = await sqlite_backend.get_artifact(report.artifact_id)
    assert art is not None
    assert (Path(tmp_path) / art.bytes_path).is_file()


@pytest.mark.asyncio
async def test_attach_migrates_tags_and_preserves_metadata(sqlite_backend, tmp_path):
    from deep_research.library.writer import LibraryWriter

    await _seed_report(sqlite_backend)
    writer = LibraryWriter(sqlite_backend, str(tmp_path))

    await _run_attach(sqlite_backend, writer)

    report = await sqlite_backend.get_report("run_t")
    assert report is not None
    # Metadata carried over (upsert overwrites these without the passthrough)
    assert report.path_taken == "deep"
    assert report.original_query == "target topic"
    assert report.config_snapshot is not None
    # New artifact has the old tag
    tags = await sqlite_backend.get_tags_for_artifact(report.artifact_id)
    assert {t.tag for t in tags} == {"keepme"}


@pytest.mark.asyncio
async def test_attach_skips_duplicate_url(sqlite_backend, tmp_path):
    from deep_research.library.writer import LibraryWriter

    await _seed_report(sqlite_backend)
    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    # Seed an existing analysis for the same URL on the target run
    await sqlite_backend.upsert_artifact(
        ArtifactRow(
            artifact_id="art_src",
            kind="html",
            source_type="html",
            title="New Paper",
            source_url="https://example.com/new",
            bytes_path="artifacts/html/art_src",
            bytes_size=10,
            first_seen_at=_now(),
            last_touched_at=_now(),
        )
    )
    await sqlite_backend.insert_analysis(
        AnalysisRow(
            analysis_id="an_existing",
            artifact_id="art_src",
            run_id="run_t",
            analyzer="analyze_source",
            summary="already analyzed",
            analyzed_at=_now(),
        )
    )

    result = await _run_attach(sqlite_backend, writer)
    assert result["status"] == "skipped"
    # Report untouched
    report = await sqlite_backend.get_report("run_t")
    assert "## Added source" not in report.markdown


@pytest.mark.asyncio
async def test_attach_leaves_report_untouched_on_fetch_failure(sqlite_backend, tmp_path):
    from deep_research.library.writer import LibraryWriter

    await _seed_report(sqlite_backend)
    writer = LibraryWriter(sqlite_backend, str(tmp_path))

    with pytest.raises(ValueError, match="could not fetch"):
        await _run_attach(
            sqlite_backend, writer, fetch_error="BLOCKED:bot_detection:cloudflare (403)"
        )

    report = await sqlite_backend.get_report("run_t")
    assert "## Added source" not in report.markdown
    assert report.path_taken == "deep"


@pytest.mark.asyncio
async def test_attach_without_artifact_skips_analysis_but_updates_report(sqlite_backend, tmp_path):
    from deep_research.library.writer import LibraryWriter

    await _seed_report(sqlite_backend)
    writer = LibraryWriter(sqlite_backend, str(tmp_path))

    result = await _run_attach(sqlite_backend, writer, no_artifact=True)
    assert result["status"] == "attached"
    # Section still appended even though no artifact/analysis recorded
    report = await sqlite_backend.get_report("run_t")
    assert "## Added source: New Paper" in report.markdown


@pytest.mark.asyncio
async def test_attach_refuses_off_topic_source_and_cleans_up_artifact(sqlite_backend, tmp_path):
    """An off-topic source is skipped with a reason, and the artifact archived
    during fetch (now orphaned) is deleted so it leaves no trace."""
    from deep_research.library.writer import LibraryWriter

    await _seed_report(sqlite_backend)
    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    off_topic = _fake_source_analysis(title="Off Topic Paper")
    off_topic.relevance_score = 0.15

    result = await _run_attach(sqlite_backend, writer, analysis=off_topic)

    assert result["status"] == "skipped"
    assert "off-topic" in result["reason"]
    assert "0.15" in result["reason"]
    # Report untouched
    report = await sqlite_backend.get_report("run_t")
    assert "## Added source" not in report.markdown
    # Orphaned artifact cleaned up (no analyses, no report references it)
    assert await sqlite_backend.get_artifact("art_src") is None
    assert await sqlite_backend.get_analyses_for_artifact("art_src") == []


@pytest.mark.asyncio
async def test_attach_keeps_shared_artifact_when_refused(sqlite_backend, tmp_path):
    """A refused attach must NOT delete an artifact that is already shared with
    another research (it has its own analyses)."""
    from deep_research.library.writer import LibraryWriter

    await _seed_report(sqlite_backend)
    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    # Pre-create the artifact (as a prior run would have) and its analysis, so
    # the artifact is shared and must survive the refused attach.
    await sqlite_backend.upsert_artifact(
        ArtifactRow(
            artifact_id="art_src",
            kind="html",
            source_type="html",
            title="New Paper",
            source_url="https://example.com/new",
            bytes_path="artifacts/html/art_src",
            bytes_size=10,
            first_seen_at=_now(),
            last_touched_at=_now(),
        )
    )
    await sqlite_backend.insert_analysis(
        AnalysisRow(
            analysis_id="an_prior",
            artifact_id="art_src",
            run_id="run_t",
            analyzer="analyze_paper",
            summary="prior analysis",
            analyzed_at=_now(),
        )
    )
    off_topic = _fake_source_analysis(title="Off Topic Paper")
    off_topic.relevance_score = 0.1

    result = await _run_attach(sqlite_backend, writer, analysis=off_topic)

    assert result["status"] == "skipped"
    # Shared artifact survives.
    assert await sqlite_backend.get_artifact("art_src") is not None
    # And its prior analysis is untouched.
    analyses = await sqlite_backend.get_analyses_for_artifact("art_src")
    assert any(a.analysis_id == "an_prior" for a in analyses)


@pytest.mark.asyncio
async def test_attach_force_overrides_relevance_gate(sqlite_backend, tmp_path):
    from deep_research.library.writer import LibraryWriter

    await _seed_report(sqlite_backend)
    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    off_topic = _fake_source_analysis(title="Off Topic Paper")
    off_topic.relevance_score = 0.1

    result = await _run_attach(sqlite_backend, writer, analysis=off_topic, force=True)

    assert result["status"] == "attached"
    report = await sqlite_backend.get_report("run_t")
    assert "## Added source" in report.markdown
    # The artifact is kept and the analysis recorded (with its score).
    analyses = await sqlite_backend.get_analyses_for_artifact("art_src")
    assert len(analyses) == 1
    assert analyses[0].relevance_score == 0.1


@pytest.mark.asyncio
async def test_attach_records_relevance_score(sqlite_backend, tmp_path):
    from deep_research.library.writer import LibraryWriter

    await _seed_report(sqlite_backend)
    writer = LibraryWriter(sqlite_backend, str(tmp_path))
    analysis = _fake_source_analysis(title="Relevant Paper")
    analysis.relevance_score = 0.85

    result = await _run_attach(sqlite_backend, writer, analysis=analysis)

    assert result["status"] == "attached"
    analyses = await sqlite_backend.get_analyses_for_artifact("art_src")
    assert analyses[0].relevance_score == 0.85
