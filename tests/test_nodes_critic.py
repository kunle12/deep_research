"""Dedicated unit tests for `nodes.critic.review` (P3).

Covers:
  - Happy path: sufficient=True stops iteration, gaps appended when sufficient=False
  - _render_sections_for_prompt: renders drafts, citations, tool_hints
  - Invalid JSON: conservative fallback (sufficient if any drafts exist)
  - LLM exception: conservative fallback
  - Tool_hint vocabulary enforcement in gaps
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.nodes.critic import _render_sections_for_prompt, review
from deep_research.state import (
    Critique,
    ResearchPlan,
    ResearchState,
    SubQuestion,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(
    query: str = "test query",
    drafts: dict[str, str] | None = None,
    sections: dict[str, list] | None = None,
) -> ResearchState:
    plan = ResearchPlan(
        sub_questions=[
            SubQuestion(id="sq1", question="Q1?", tool_hint="general-web", rationale="r1"),
            SubQuestion(id="sq2", question="Q2?", tool_hint="arxiv", rationale="r2"),
        ],
        breadth=2,
        max_depth=0,
    )
    s = ResearchState(query=query, plan=plan)
    if drafts:
        for k, v in drafts.items():
            s.drafts[k] = v
    if sections:
        for k, v in sections.items():
            s.sections[k] = list(v)
    return s


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


class _FakeAsyncOpenAI:
    def __init__(self, content: str) -> None:
        self.chat = MagicMock()
        self.chat.completions = MagicMock()
        self.chat.completions.create = AsyncMock(return_value=_FakeResponse(content))


def _raising_client(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


# ---------------------------------------------------------------------------
# _render_sections_for_prompt
# ---------------------------------------------------------------------------


class TestRenderSectionsForPrompt:
    def test_renders_sub_questions_and_drafts(self) -> None:
        state = _state(
            query="q",
            drafts={"sq1": "Draft answer for Q1"},
        )
        out = _render_sections_for_prompt(state)
        assert "Q1?" in out
        assert "Draft answer" in out
        assert "Q2?" in out
        assert "(no draft produced)" in out

    def test_renders_citations(self) -> None:
        from deep_research.state import Citation

        state = _state(sections={"sq1": [Citation(url="https://a", title="A", snippet="s")]})
        out = _render_sections_for_prompt(state)
        assert "https://a" in out

    def test_no_drafts_renders_no_draft_labels(self) -> None:
        state = _state()
        out = _render_sections_for_prompt(state)
        assert "(no draft produced)" in out

    def test_draft_truncated_to_2000_chars(self) -> None:
        state = _state(drafts={"sq1": "A" * 5000})
        out = _render_sections_for_prompt(state)
        # The draft is truncated to 2000 chars
        assert len([ln for ln in out.splitlines() if "AAAA" in ln]) > 0


# ---------------------------------------------------------------------------
# review() — happy path
# ---------------------------------------------------------------------------


class TestReviewHappyPath:
    @pytest.mark.asyncio
    async def test_sufficient_stops_iteration(self) -> None:
        payload = {
            "sufficient": True,
            "rationale": "All aspects covered.",
            "gaps": [],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        state = _state(drafts={"sq1": "draft content"})
        out = await review(state, client, "m")
        assert isinstance(out, Critique)
        assert out.sufficient is True
        assert out.gaps == []

    @pytest.mark.asyncio
    async def test_not_sufficient_with_gaps(self) -> None:
        payload = {
            "sufficient": False,
            "rationale": "Missing depth on Q2.",
            "gaps": [
                {"id": "gap1", "question": "What are the implications?", "tool_hint": "general-web", "rationale": "need more"},
            ],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        state = _state(drafts={"sq1": "draft"})
        out = await review(state, client, "m")
        assert out.sufficient is False
        assert len(out.gaps) == 1
        assert out.gaps[0].question == "What are the implications?"

    @pytest.mark.asyncio
    async def test_fills_missing_gap_id_with_fallback(self) -> None:
        payload = {
            "sufficient": False,
            "rationale": "r",
            "gaps": [{"question": "Gap Q", "tool_hint": "general-web", "rationale": "r"}],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        state = _state(drafts={"sq1": "draft"})
        out = await review(state, client, "m")
        assert out.gaps[0].id.startswith("critic_gap_")

    @pytest.mark.asyncio
    async def test_invalid_gap_tool_hint_defaulted(self) -> None:
        payload = {
            "sufficient": False,
            "rationale": "r",
            "gaps": [{"id": "g1", "question": "Q", "tool_hint": "bad-hint", "rationale": "r"}],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        state = _state(drafts={"sq1": "draft"})
        out = await review(state, client, "m")
        assert out.gaps[0].tool_hint == "general-web"


# ---------------------------------------------------------------------------
# review() — fallback on failure
# ---------------------------------------------------------------------------


class TestReviewFallback:
    @pytest.mark.asyncio
    async def test_invalid_json_declares_sufficient_when_drafts_exist(self) -> None:
        client = _FakeAsyncOpenAI("not valid json {{{")
        state = _state(drafts={"sq1": "draft content"})
        out = await review(state, client, "m")
        assert out.sufficient is True  # conservative: drafts exist
        assert out.gaps == []

    @pytest.mark.asyncio
    async def test_invalid_json_not_sufficient_when_no_drafts(self) -> None:
        client = _FakeAsyncOpenAI("not valid json {{{")
        state = _state()  # no drafts
        out = await review(state, client, "m")
        assert out.sufficient is False
        assert len(out.gaps) == 1
        assert out.gaps[0].id == "critic_fallback_gap"

    @pytest.mark.asyncio
    async def test_llm_exception_declares_sufficient_when_drafts_exist(self) -> None:
        client = _raising_client(RuntimeError("critic down"))
        state = _state(drafts={"sq1": "draft"})
        out = await review(state, client, "m")
        assert out.sufficient is True
        assert "critic LLM call failed" in out.rationale

    @pytest.mark.asyncio
    async def test_llm_exception_not_sufficient_when_no_drafts(self) -> None:
        client = _raising_client(RuntimeError("critic down"))
        state = _state()  # no drafts
        out = await review(state, client, "m")
        assert out.sufficient is False
        assert len(out.gaps) == 1
        assert out.gaps[0].id == "critic_fallback_gap"


__all__ = ["TestRenderSectionsForPrompt", "TestReviewFallback", "TestReviewHappyPath"]
