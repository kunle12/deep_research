"""Dedicated unit tests for `nodes.planner.plan` (P3).

Covers the LLM-call wrapper fully offline:
  - Happy path: valid JSON with sub-questions, tool_hint validation
  - Invalid JSON: fallback to single sub-question = original query
  - LLM exception: fallback to single sub-question
  - Breadth parameter passed through to prompt
  - Tool_hint vocabulary enforcement (invalid hints defaulted to general-web)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.nodes.planner import plan
from deep_research.state import ResearchPlan

# ---------------------------------------------------------------------------
# AsyncOpenAI doubles
# ---------------------------------------------------------------------------


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
# plan() — happy path
# ---------------------------------------------------------------------------


class TestPlanHappyPath:
    @pytest.mark.asyncio
    async def test_parses_valid_json(self) -> None:
        payload = {
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "What are the latest RLHF methods?",
                    "tool_hint": "arxiv",
                    "rationale": "RLHF is a research topic.",
                },
                {
                    "id": "sq2",
                    "question": "What are the limitations?",
                    "tool_hint": "general-web",
                    "rationale": "Needs broader sources.",
                },
            ],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        out = await plan("Survey RLHF", client, "m", breadth=6)
        assert isinstance(out, ResearchPlan)
        assert out.breadth == 2
        assert len(out.sub_questions) == 2
        assert out.sub_questions[0].id == "sq1"
        assert out.sub_questions[0].tool_hint == "arxiv"
        assert out.sub_questions[1].tool_hint == "general-web"

    @pytest.mark.asyncio
    async def test_caps_sub_questions_to_breadth(self) -> None:
        payload = {
            "sub_questions": [
                {"id": f"sq{i}", "question": f"Q{i}", "tool_hint": "general-web", "rationale": "r"}
                for i in range(10)
            ],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        out = await plan("query", client, "m", breadth=3)
        assert len(out.sub_questions) == 3  # capped by breadth

    @pytest.mark.asyncio
    async def test_fills_missing_id_with_fallback(self) -> None:
        payload = {
            "sub_questions": [
                {"question": "Q1", "tool_hint": "general-web", "rationale": "r"},
            ],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        out = await plan("query", client, "m", breadth=6)
        assert out.sub_questions[0].id == "sq1"  # auto-numbered

    @pytest.mark.asyncio
    async def test_fills_missing_question_with_empty_string(self) -> None:
        payload = {
            "sub_questions": [
                {"id": "sq1", "tool_hint": "general-web", "rationale": "r"},
            ],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        out = await plan("query", client, "m", breadth=6)
        assert out.sub_questions[0].question == ""  # empty but present

    @pytest.mark.asyncio
    async def test_invalid_tool_hint_defaulted_to_general_web(self) -> None:
        payload = {
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "Q1",
                    "tool_hint": "nonexistent-tool",
                    "rationale": "r",
                },
            ],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        out = await plan("query", client, "m", breadth=6)
        assert out.sub_questions[0].tool_hint == "general-web"


# ---------------------------------------------------------------------------
# plan() — fallback on failure
# ---------------------------------------------------------------------------


class TestPlanFallback:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_single_fallback(self) -> None:
        client = _FakeAsyncOpenAI("not valid json {{{")
        out = await plan("What is RLHF?", client, "m", breadth=6)
        assert isinstance(out, ResearchPlan)
        assert out.breadth == 1
        assert len(out.sub_questions) == 1
        assert out.sub_questions[0].question == "What is RLHF?"
        assert out.sub_questions[0].tool_hint == "general-web"

    @pytest.mark.asyncio
    async def test_llm_exception_returns_single_fallback(self) -> None:
        client = _raising_client(RuntimeError("LLM down"))
        out = await plan("query", client, "m", breadth=6)
        assert isinstance(out, ResearchPlan)
        assert out.breadth == 1
        assert "planner failed" in out.sub_questions[0].rationale

    @pytest.mark.asyncio
    async def test_empty_sub_questions_falls_back(self) -> None:
        payload = {"sub_questions": []}
        client = _FakeAsyncOpenAI(json.dumps(payload))
        out = await plan("original query", client, "m", breadth=6)
        assert out.breadth == 1
        assert out.sub_questions[0].question == "original query"

    @pytest.mark.asyncio
    async def test_missing_sub_questions_key_falls_back(self) -> None:
        payload = {"other_key": "value"}
        client = _FakeAsyncOpenAI(json.dumps(payload))
        out = await plan("fallback query", client, "m", breadth=6)
        assert out.breadth == 1
        assert out.sub_questions[0].question == "fallback query"

    @pytest.mark.asyncio
    async def test_empty_content_returns_fallback(self) -> None:
        client = _FakeAsyncOpenAI("")
        out = await plan("q", client, "m", breadth=6)
        assert out.breadth == 1
        assert out.sub_questions[0].question == "q"


__all__ = ["TestPlanFallback", "TestPlanHappyPath"]
