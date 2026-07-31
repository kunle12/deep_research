"""Conformance tests: glossary upsert."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deep_research.library.storage.rows import GlossaryEntry


@pytest.mark.asyncio
async def test_upsert_and_get_glossary(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    entry = GlossaryEntry(
        term="RLHF",
        term_canonical="rlhf",
        kind="acronym",
        short_def="Reinforcement Learning from Human Feedback",
        acronym_expansion="Reinforcement Learning from Human Feedback",
        last_updated=now,
    )
    await sqlite_backend.upsert_glossary_entry(entry)

    fetched = await sqlite_backend.get_glossary_entry("rlhf")
    assert fetched is not None
    assert fetched.term == "RLHF"
    assert fetched.kind == "acronym"
    assert fetched.acronym_expansion == "Reinforcement Learning from Human Feedback"


@pytest.mark.asyncio
async def test_upsert_overwrites(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    e1 = GlossaryEntry(
        term="AI",
        term_canonical="ai",
        kind="concept",
        short_def="Original def",
        last_updated=now,
    )
    await sqlite_backend.upsert_glossary_entry(e1)

    e2 = GlossaryEntry(
        term="AI",
        term_canonical="ai",
        kind="concept",
        short_def="Updated def",
        last_updated=now,
    )
    await sqlite_backend.upsert_glossary_entry(e2)

    fetched = await sqlite_backend.get_glossary_entry("ai")
    assert fetched is not None
    assert fetched.short_def == "Updated def"


@pytest.mark.asyncio
async def test_list_glossary(sqlite_backend):
    now = datetime.now(UTC).isoformat()
    for term, canonical in [("A", "a"), ("B", "b"), ("C", "c")]:
        await sqlite_backend.upsert_glossary_entry(
            GlossaryEntry(
                term=term,
                term_canonical=canonical,
                kind="concept",
                short_def=f"Definition {term}",
                last_updated=now,
            )
        )

    entries = await sqlite_backend.list_glossary_entries()
    assert len(entries) >= 3


@pytest.mark.asyncio
async def test_get_missing_glossary(sqlite_backend):
    missing = await sqlite_backend.get_glossary_entry("nonexistent")
    assert missing is None


@pytest.mark.asyncio
async def test_upsert_preserves_first_seen_provenance(sqlite_backend):
    """Updating an existing term must NOT clobber first_seen_run_id /
    first_seen_artifact_id (they record the *first* time the term appeared)."""
    from deep_research.library.storage.rows import ArtifactRow, ReportRow

    now = datetime.now(UTC).isoformat()
    # FKs: first_seen_run_id -> reports(run_id), first_seen_artifact_id -> artifacts
    await sqlite_backend.insert_report(
        ReportRow(run_id="run_1", started_at=now, markdown="# run 1")
    )
    await sqlite_backend.insert_report(
        ReportRow(run_id="run_2", started_at=now, markdown="# run 2")
    )
    await sqlite_backend.upsert_artifact(
        ArtifactRow(
            artifact_id="art_1",
            kind="report",
            bytes_path="reports/x.md",
            first_seen_at=now,
            last_touched_at=now,
        )
    )
    await sqlite_backend.upsert_artifact(
        ArtifactRow(
            artifact_id="art_2",
            kind="report",
            bytes_path="reports/y.md",
            first_seen_at=now,
            last_touched_at=now,
        )
    )

    await sqlite_backend.upsert_glossary_entry(
        GlossaryEntry(
            term="GPT",
            term_canonical="gpt",
            kind="model",
            short_def="Original def",
            first_seen_run_id="run_1",
            first_seen_artifact_id="art_1",
            last_updated=now,
        )
    )
    await sqlite_backend.upsert_glossary_entry(
        GlossaryEntry(
            term="GPT",
            term_canonical="gpt",
            kind="model",
            short_def="Updated def",
            first_seen_run_id="run_2",
            first_seen_artifact_id="art_2",
            last_updated=now,
        )
    )

    fetched = await sqlite_backend.get_glossary_entry("gpt")
    assert fetched is not None
    assert fetched.short_def == "Updated def"
    assert fetched.first_seen_run_id == "run_1"
    assert fetched.first_seen_artifact_id == "art_1"


@pytest.mark.asyncio
async def test_upsert_preserves_term_id(sqlite_backend):
    """term_id must stay stable across updates so the FTS rowid linkage holds."""
    now = datetime.now(UTC).isoformat()
    await sqlite_backend.upsert_glossary_entry(
        GlossaryEntry(term="AI", term_canonical="ai", kind="concept", last_updated=now)
    )
    first = await sqlite_backend.get_glossary_entry("ai")
    assert first is not None

    await sqlite_backend.upsert_glossary_entry(
        GlossaryEntry(term="AI", term_canonical="ai", kind="concept", last_updated=now)
    )
    second = await sqlite_backend.get_glossary_entry("ai")
    assert second is not None
    assert first.term_id == second.term_id
