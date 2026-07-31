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
