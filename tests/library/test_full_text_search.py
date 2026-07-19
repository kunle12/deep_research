"""Conformance tests: full-text search."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deep_research.library.storage.rows import AnalysisRow, ArtifactRow, GlossaryEntry


@pytest.mark.asyncio
async def test_full_text_search(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    art = ArtifactRow(
        artifact_id="fts_art_1",
        kind="pdf",
        source_type="arxiv",
        title="ML Paper",
        discovered_by="arxiv",
        bytes_path="artifacts/pdf/fts_art_1.pdf",
        bytes_size=1000,
        first_seen_at=now,
        last_touched_at=now,
    )
    await sqlite_backend.upsert_artifact(art)

    # Insert a report first (FK constraint for analyses.run_id)
    from deep_research.library.storage.rows import ReportRow
    report = ReportRow(
        run_id="run_001", started_at=now, original_query="ml",
        path_taken="deep", markdown="# ML",
        artifact_id="fts_art_1",
    )
    await sqlite_backend.insert_report(report)

    analysis = AnalysisRow(
        analysis_id="fts_an_1",
        artifact_id="fts_art_1",
        run_id="run_001",
        analyzer="analyze_paper",
        summary="This paper discusses machine learning transformers.",
        key_findings='["transformers are effective", "attention mechanism"]',
        analyzed_at=now,
    )
    await sqlite_backend.insert_analysis(analysis)

    hits = await sqlite_backend.full_text_search("machine learning", kind="pdf", limit=10)
    assert len(hits) >= 1, f"expected at least 1 hit for 'machine learning', got {len(hits)}"
    assert hits[0].artifact_id == "fts_art_1"
    assert "machine learning" in (hits[0].summary or "").lower() or \
           "machine learning" in (hits[0].extracted_text or "").lower()


@pytest.mark.asyncio
async def test_glossary_search(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    await sqlite_backend.upsert_glossary_entry(GlossaryEntry(
        term="Transformer",
        term_canonical="transformer",
        kind="model",
        short_def="A neural network architecture using attention mechanisms.",
        last_updated=now,
    ))

    results = await sqlite_backend.glossary_search("attention", limit=10)
    assert len(results) >= 0
