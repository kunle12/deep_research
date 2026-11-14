"""Tests for the dedicated evidence-extraction pass."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.nodes.evidence import _parse_evidence, extract_evidence
from deep_research.state import Citation, ResearchPlan, ResearchState, SubQuestion


def _state() -> ResearchState:
    state = ResearchState(
        query="Does RLHF improve safety?",
        plan=ResearchPlan(sub_questions=[SubQuestion(id="sq1", question="Q1?")]),
    )
    state.absorb_section("sq1", [], "RLHF improves safety on some benchmarks.")
    state.citations["https://a"] = Citation(url="https://a", title="A")
    return state


def _fake_client(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content=content, tool_calls=None))]
        )
    )
    return client


def test_parse_evidence_dedupes_claims_and_keeps_contradictions() -> None:
    raw = json.dumps(
        {
            "evidence": [
                {
                    "claim": "RLHF improves safety",
                    "evidence": "safety improves",
                    "source_url": "https://a",
                    "stance": "supports",
                    "confidence": 0.9,
                },
                {
                    "claim": "RLHF improves safety",
                    "evidence": "duplicate",
                    "source_url": "https://b",
                    "stance": "supports",
                },
                {
                    "claim": "RLHF can reduce helpfulness",
                    "evidence": "trade-off",
                    "source_url": "https://c",
                    "stance": "bad-stance",
                    "confidence": "not-a-number",
                },
            ],
            "contradictions": ["A says improves; C says reduces"],
        }
    )
    items, contradictions = _parse_evidence(raw, max_items=60)
    assert len(items) == 2
    assert items[0].claim == "RLHF improves safety"
    assert items[0].confidence == 0.9
    assert items[1].stance == "unclear"  # invalid stance normalized
    assert items[1].confidence == 0.5  # malformed confidence defaults
    assert contradictions == ["A says improves; C says reduces"]


def test_parse_evidence_invalid_json() -> None:
    assert _parse_evidence("not json", max_items=10) == ([], [])


@pytest.mark.asyncio
async def test_extract_evidence_non_fatal_on_llm_failure() -> None:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("down"))
    items, contradictions = await extract_evidence(_state(), client, "m")
    assert items == []
    assert contradictions == []


@pytest.mark.asyncio
async def test_extract_evidence_returns_items() -> None:
    client = _fake_client(
        json.dumps(
            {
                "evidence": [
                    {
                        "claim": "RLHF improves safety",
                        "evidence": "safety improves",
                        "source_url": "https://a",
                        "stance": "supports",
                        "confidence": 0.8,
                    }
                ],
                "contradictions": [],
            }
        )
    )
    items, contradictions = await extract_evidence(_state(), client, "m")
    assert len(items) == 1
    assert items[0].claim == "RLHF improves safety"
    assert contradictions == []
