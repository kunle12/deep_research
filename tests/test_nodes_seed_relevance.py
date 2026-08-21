"""Tests for the seed relevance pre-gate (nodes/seed_relevance.py)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.nodes.seed_relevance import filter_relevant_seeds
from deep_research.state import PaperNode


def _seed(arxiv_id: str, title: str = "", abstract: str = "") -> PaperNode:
    return PaperNode(arxiv_id=arxiv_id, title=title or arxiv_id, abstract=abstract)


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


def _client(scores: dict[str, float]) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_FakeResponse(json.dumps({"scores": scores}))
    )
    return client


def _raising_client() -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("LLM down"))
    return client


@pytest.mark.asyncio
async def test_drops_off_topic_seeds_below_threshold() -> None:
    seeds = [
        _seed("2401.1", title="On-topic RLHF", abstract="preference optimization for LLMs"),
        _seed("2401.2", title="Keyword overlap", abstract="adversarial attacks on networks"),
        _seed("2401.3", title="Clearly relevant", abstract="RLHF and alignment"),
    ]
    client = _client({"2401.1": 0.9, "2401.2": 0.2, "2401.3": 0.8})
    kept = await filter_relevant_seeds("RLHF survey", seeds, client, "m", threshold=0.7)
    ids = {s.arxiv_id for s in kept}
    assert ids == {"2401.1", "2401.3"}
    assert "2401.2" not in ids


@pytest.mark.asyncio
async def test_keeps_seeds_missing_from_response() -> None:
    """A seed the LLM omits is kept (lenient — only explicitly-low scores drop)."""
    seeds = [_seed("2401.1"), _seed("2401.2")]
    client = _client({"2401.1": 0.9})  # 2401.2 missing
    kept = await filter_relevant_seeds("q", seeds, client, "m", threshold=0.7)
    assert {s.arxiv_id for s in kept} == {"2401.1", "2401.2"}


@pytest.mark.asyncio
async def test_keeps_everything_when_disabled() -> None:
    seeds = [_seed("2401.1"), _seed("2401.2")]
    client = _raising_client()
    kept = await filter_relevant_seeds("q", seeds, client, "m", threshold=0.7, enabled=False)
    assert len(kept) == 2
    client.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_keeps_everything_when_llm_fails() -> None:
    seeds = [_seed("2401.1"), _seed("2401.2")]
    kept = await filter_relevant_seeds("q", seeds, _raising_client(), "m", threshold=0.7)
    assert len(kept) == 2


@pytest.mark.asyncio
async def test_empty_seeds_returns_empty() -> None:
    kept = await filter_relevant_seeds("q", [], MagicMock(), "m", threshold=0.7)
    assert kept == []


@pytest.mark.asyncio
async def test_empty_query_returns_all() -> None:
    seeds = [_seed("2401.1")]
    client = _raising_client()
    kept = await filter_relevant_seeds("  ", seeds, client, "m", threshold=0.7)
    assert len(kept) == 1
    client.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_clamps_scores_to_unit_interval() -> None:
    seeds = [_seed("2401.1")]
    client = _client({"2401.1": 5.0})  # out of range -> clamped to 1.0
    kept = await filter_relevant_seeds("q", seeds, client, "m", threshold=0.7)
    assert len(kept) == 1


@pytest.mark.asyncio
async def test_handles_non_dict_scores() -> None:
    seeds = [_seed("2401.1")]
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_FakeResponse(json.dumps({"scores": ["nope"]}))
    )
    kept = await filter_relevant_seeds("q", seeds, client, "m", threshold=0.7)
    # No usable scores -> keep all (lenient fallback)
    assert len(kept) == 1
