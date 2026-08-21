"""Tests for nodes/recall.py — library-first prior knowledge injection."""

from __future__ import annotations

import pytest

from deep_research.library.storage.rows import SearchHit
from deep_research.nodes.recall import format_recall_context, recall


@pytest.mark.asyncio
async def test_recall_empty_when_storage_none() -> None:
    """When storage is None (PDL disabled), recall returns empty list."""
    result = await recall("transformer attention", None)
    assert result == []


@pytest.mark.asyncio
async def test_recall_empty_on_empty_query() -> None:
    """Empty query returns empty list."""
    result = await recall("", None)
    assert result == []


class _FakeStorage:
    """Minimal fake StorageBackend that returns canned hits."""

    def __init__(self, hits: list[SearchHit] | None = None):
        self._hits = hits or []

    async def full_text_search(self, query: str, *, kind: str, limit: int) -> list[SearchHit]:
        return self._hits


@pytest.mark.asyncio
async def test_recall_returns_hits() -> None:
    """When FTS5 returns hits, recall returns formatted dicts."""
    hits = [
        SearchHit(
            artifact_id="abc123",
            title="Attention Is All You Need",
            authors="Vaswani et al.",
            summary="A seminal paper on transformer attention mechanisms.",
            extracted_text="key finding: attention scales quadratically with sequence length",
            score=0.95,
        ),
        SearchHit(
            artifact_id="def456",
            title="Efficient Attention",
            authors="Smith et al.",
            summary="A survey of efficient attention mechanisms.",
            extracted_text="key finding: linear attention reduces O(n^2) to O(n)",
            score=0.85,
        ),
    ]
    storage = _FakeStorage(hits)
    result = await recall("transformer attention", storage, max_results=5)
    assert len(result) == 2
    assert result[0]["artifact_id"] == "abc123"
    assert result[0]["title"] == "Attention Is All You Need"
    assert "seminal paper" in result[0]["summary"]
    assert result[1]["artifact_id"] == "def456"


@pytest.mark.asyncio
async def test_recall_dedup_by_artifact_id() -> None:
    """Duplicate artifact_ids are deduped."""
    hits = [
        SearchHit(
            artifact_id="abc123",
            title="Attention Paper",
            authors="Author A",
            summary="Summary A",
            extracted_text="findings A",
            score=0.9,
        ),
        SearchHit(
            artifact_id="abc123",
            title="Attention Paper",
            authors="Author B",
            summary="Summary B",
            extracted_text="findings B",
            score=0.8,
        ),
    ]
    storage = _FakeStorage(hits)
    result = await recall("attention", storage, max_results=5)
    assert len(result) == 1  # deduped


@pytest.mark.asyncio
async def test_recall_no_matches() -> None:
    """No FTS5 matches returns empty list."""
    storage = _FakeStorage([])
    result = await recall("nonexistent topic", storage)
    assert result == []


def test_format_recall_context_empty() -> None:
    """Empty entries list returns empty string."""
    assert format_recall_context([]) == ""


def test_format_recall_context_single() -> None:
    """Single entry produces markdown section."""
    entries = [
        {
            "artifact_id": "abc123",
            "title": "Attention Is All You Need",
            "summary": "A seminal paper on attention.",
            "key_findings": "",
        }
    ]
    md = format_recall_context(entries)
    assert "Prior research from the library" in md
    assert "Attention Is All You Need" in md
    assert "seminal paper on attention" in md
    assert "delta" in md  # instruction about delta fetching


def test_format_recall_context_multiple() -> None:
    """Multiple entries produce numbered list."""
    entries = [
        {"artifact_id": "a1", "title": "Paper A", "summary": "Summary A", "key_findings": ""},
        {"artifact_id": "a2", "title": "Paper B", "summary": "", "key_findings": ""},
    ]
    md = format_recall_context(entries)
    assert "**1. Paper A**" in md
    assert "**2. Paper B**" in md
    assert "Summary A" in md


@pytest.mark.asyncio
async def test_recall_filters_low_relevance_hits() -> None:
    """Stored analyses with an explicitly-low relevance score are dropped from
    recall so an archived off-topic document can't pollute prior context."""
    hits = [
        SearchHit(
            artifact_id="on_topic",
            title="On Topic",
            summary="RLHF summary",
            extracted_text="findings",
            score=0.9,
            relevance_score=0.8,
            authors="",
        ),
        SearchHit(
            artifact_id="off_topic",
            title="Off Topic",
            summary="adversarial attacks summary",
            extracted_text="findings",
            score=0.8,
            relevance_score=0.1,
            authors="",
        ),
        SearchHit(
            artifact_id="pre_feature",
            title="No Score",
            summary="summary",
            extracted_text="findings",
            score=0.7,
            relevance_score=None,
            authors="",
        ),
    ]
    storage = _FakeStorage(hits)
    result = await recall("rlhf", storage, max_results=5)
    ids = {r["artifact_id"] for r in result}
    assert ids == {"on_topic", "pre_feature"}
    assert "off_topic" not in ids
    # The kept hit carries its relevance through.
    by_id = {r["artifact_id"]: r for r in result}
    assert by_id["on_topic"]["relevance_score"] == 0.8


@pytest.mark.asyncio
async def test_recall_min_relevance_zero_disables_filter() -> None:
    hits = [
        SearchHit(
            artifact_id="off_topic",
            title="Off Topic",
            summary="s",
            extracted_text="f",
            score=0.8,
            relevance_score=0.05,
            authors="",
        ),
    ]
    storage = _FakeStorage(hits)
    result = await recall("q", storage, max_results=5, min_relevance=0)
    assert len(result) == 1
