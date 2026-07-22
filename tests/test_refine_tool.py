"""Tests for the dynamic refinement (refine tool) feature.

Covers:
  1. drill_deeper refinement collected and returned
  2. chase_reference with URL → correct parent_arxiv_id
  3. revise_strategy → ack returned, no collector entry
  4. Depth cap prevents refinements beyond max_refinement_depth
  5. Multiple refine calls within same researcher → all collected
  6. drill_deeper with missing question → gracefully skipped
  7. Integration: refinements flushed into plan before critic
  8. Normalized dedup in absorb_refinements (case/whitespace)
  9. Concurrent isolation: parallel researchers get isolated refinements
 10. Per-researcher cap enforced
 11. Global iteration cap via flush_refinements(max_total)
 12. ScopedToolRegistry: parent tools callable, scoped tool not on parent
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ScopedToolRegistry, ToolRegistry, ToolResult
from deep_research.nodes.researcher import research
from deep_research.state import (
    Citation,
    ClassifiedQuery,
    Critique,
    QueryPlan,
    ResearchPlan,
    ResearchState,
    SubQuestion,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sub_q(
    question: str = "test question",
    tool_hint: str = "general-web",
    refinement_depth: int = 0,
) -> SubQuestion:
    return SubQuestion(
        id="sq1", question=question, tool_hint=tool_hint,
        rationale="test", refinement_depth=refinement_depth,
    )


class _FakeToolCall:
    def __init__(self, id: str, name: str, arguments: dict) -> None:
        self.id = id
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = json.dumps(arguments)


class _FakeMessage:
    def __init__(self, content: str, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, content: str, tool_calls: list | None = None) -> None:
        self.message = _FakeMessage(content, tool_calls)


class _FakeResponse:
    def __init__(self, content: str, tool_calls: list | None = None) -> None:
        self.choices = [_FakeChoice(content, tool_calls)]


def _answer_json(answer: str = "done") -> str:
    return json.dumps({"answer": answer, "citations": []})


def _client_with_refine_calls(refine_calls: list[dict]) -> MagicMock:
    """Client that emits refine tool calls on turn 1, then a final answer on turn 2."""
    call_idx = {"n": 0}

    async def _create(**kwargs: Any) -> _FakeResponse:
        call_idx["n"] += 1
        if call_idx["n"] == 1 and refine_calls:
            tool_calls = [
                _FakeToolCall(f"tc_{i}", "refine", args)
                for i, args in enumerate(refine_calls)
            ]
            return _FakeResponse("", tool_calls)
        return _FakeResponse(_answer_json())

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=_create)
    return client


# ---------------------------------------------------------------------------
# 1–6: research() refine tool unit tests
# ---------------------------------------------------------------------------


class TestRefineTool:
    @pytest.mark.asyncio
    async def test_drill_deeper_collected(self) -> None:
        client = _client_with_refine_calls([
            {"action": "drill_deeper", "question": "What about Y?", "rationale": "interesting"},
        ])
        reg = ToolRegistry()
        _, _, refinements = await research(_sub_q(), client, "m", reg)
        assert len(refinements) == 1
        assert refinements[0].question == "What about Y?"
        assert refinements[0].refinement_depth == 1
        assert "refine" in refinements[0].id

    @pytest.mark.asyncio
    async def test_chase_reference_with_arxiv_url(self) -> None:
        client = _client_with_refine_calls([
            {"action": "chase_reference", "reference_url": "https://arxiv.org/abs/2401.12345", "rationale": "key paper"},
        ])
        reg = ToolRegistry()
        _, _, refinements = await research(_sub_q(), client, "m", reg)
        assert len(refinements) == 1
        assert refinements[0].parent_arxiv_id == "https://arxiv.org/abs/2401.12345"
        assert "ref" in refinements[0].id

    @pytest.mark.asyncio
    async def test_chase_reference_non_arxiv(self) -> None:
        client = _client_with_refine_calls([
            {"action": "chase_reference", "reference_url": "https://example.com/paper", "rationale": "ref"},
        ])
        reg = ToolRegistry()
        _, _, refinements = await research(_sub_q(), client, "m", reg)
        assert len(refinements) == 1
        assert refinements[0].parent_arxiv_id is None

    @pytest.mark.asyncio
    async def test_revise_strategy_not_collected(self) -> None:
        client = _client_with_refine_calls([
            {"action": "revise_strategy", "rationale": "try a different approach"},
        ])
        reg = ToolRegistry()
        _, _, refinements = await research(_sub_q(), client, "m", reg)
        assert refinements == []

    @pytest.mark.asyncio
    async def test_depth_cap_prevents_refinement(self) -> None:
        client = _client_with_refine_calls([
            {"action": "drill_deeper", "question": "deeper?", "rationale": "r"},
        ])
        reg = ToolRegistry()
        sq = _sub_q(refinement_depth=2)
        _, _, refinements = await research(
            sq, client, "m", reg, max_refinement_depth=2,
        )
        assert refinements == []

    @pytest.mark.asyncio
    async def test_multiple_refine_calls_all_collected(self) -> None:
        client = _client_with_refine_calls([
            {"action": "drill_deeper", "question": "Q1?", "rationale": "r1"},
            {"action": "drill_deeper", "question": "Q2?", "rationale": "r2"},
            {"action": "chase_reference", "reference_url": "https://arxiv.org/abs/1", "rationale": "r3"},
        ])
        reg = ToolRegistry()
        _, _, refinements = await research(_sub_q(), client, "m", reg)
        assert len(refinements) == 3

    @pytest.mark.asyncio
    async def test_drill_deeper_missing_question_skipped(self) -> None:
        client = _client_with_refine_calls([
            {"action": "drill_deeper", "rationale": "no question provided"},
        ])
        reg = ToolRegistry()
        _, _, refinements = await research(_sub_q(), client, "m", reg)
        assert refinements == []

    @pytest.mark.asyncio
    async def test_per_researcher_cap(self) -> None:
        client = _client_with_refine_calls([
            {"action": "drill_deeper", "question": f"Q{i}?", "rationale": "r"}
            for i in range(5)
        ])
        reg = ToolRegistry()
        _, _, refinements = await research(
            _sub_q(), client, "m", reg, max_refinement_per_researcher=2,
        )
        assert len(refinements) == 2


# ---------------------------------------------------------------------------
# 7: Integration — refinements flushed before critic
# ---------------------------------------------------------------------------


class TestRefineIntegration:
    @pytest.mark.asyncio
    async def test_refinements_flushed_into_plan_before_critic(self) -> None:
        cfg = AgentTopConfig()
        cfg.agent.max_iterations = 2
        sq1 = SubQuestion(id="sq1", question="Q1?", rationale="r")
        plan_result = ResearchPlan(sub_questions=[sq1], breadth=1, max_depth=0)
        refined_sq = SubQuestion(id="sq1.refine1", question="Deeper Q?", rationale="r", refinement_depth=1)

        critic_saw_refinement = {"value": False}

        async def _researcher(sq, client, model, tools, **kwargs):
            return ("ans", [], [refined_sq])

        async def _critic(state, client, model):
            questions = {s.question for s in state.plan.sub_questions}
            critic_saw_refinement["value"] = "Deeper Q?" in questions
            return Critique(sufficient=True, rationale="r", gaps=[])

        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run", side_effect=_researcher),
            patch("deep_research.paths.deep.critic_review", side_effect=_critic),
            patch("deep_research.paths.deep.writer_write", return_value="# Report"),
        ):
            from deep_research.paths.deep import deep_research
            client = MagicMock()
            reg = ToolRegistry()
            classified = ClassifiedQuery(path=QueryPlan.deep, rationale="test")
            await deep_research(classified, "Q", client, reg, cfg)
            assert critic_saw_refinement["value"]


# ---------------------------------------------------------------------------
# 8: Normalized dedup in absorb_refinements
# ---------------------------------------------------------------------------


class TestAbsorbRefinements:
    def test_dedup_case_insensitive(self) -> None:
        state = ResearchState(query="q")
        state.plan.sub_questions = [
            SubQuestion(id="sq1", question="What is X?", rationale="r"),
        ]
        state.absorb_refinements([
            SubQuestion(id="r1", question="what is x?", rationale="r"),
            SubQuestion(id="r2", question="  WHAT IS X?  ", rationale="r"),
            SubQuestion(id="r3", question="What is Y?", rationale="r"),
        ])
        assert len(state.pending_refinements) == 1
        assert state.pending_refinements[0].question == "What is Y?"

    def test_dedup_against_pending(self) -> None:
        state = ResearchState(query="q")
        state.absorb_refinements([
            SubQuestion(id="r1", question="New Q?", rationale="r"),
        ])
        state.absorb_refinements([
            SubQuestion(id="r2", question="new q?", rationale="r"),
        ])
        assert len(state.pending_refinements) == 1


# ---------------------------------------------------------------------------
# 9: Concurrent isolation
# ---------------------------------------------------------------------------


class TestConcurrentIsolation:
    @pytest.mark.asyncio
    async def test_parallel_researchers_isolated_refinements(self) -> None:
        def _make_client(question: str) -> MagicMock:
            call_idx = {"n": 0}

            async def _create(**kwargs: Any) -> _FakeResponse:
                call_idx["n"] += 1
                if call_idx["n"] == 1:
                    return _FakeResponse("", [
                        _FakeToolCall("tc_0", "refine", {
                            "action": "drill_deeper",
                            "question": question,
                            "rationale": "r",
                        }),
                    ])
                return _FakeResponse(_answer_json())

            client = MagicMock()
            client.chat.completions.create = AsyncMock(side_effect=_create)
            return client

        reg = ToolRegistry()
        sq_a = SubQuestion(id="a", question="QA?", rationale="r")
        sq_b = SubQuestion(id="b", question="QB?", rationale="r")

        result_a, result_b = await asyncio.gather(
            research(sq_a, _make_client("Refine from A"), "m", reg),
            research(sq_b, _make_client("Refine from B"), "m", reg),
        )

        refs_a = result_a[2]
        refs_b = result_b[2]
        assert len(refs_a) == 1
        assert len(refs_b) == 1
        assert refs_a[0].question == "Refine from A"
        assert refs_b[0].question == "Refine from B"


# ---------------------------------------------------------------------------
# 10–11: Flush caps
# ---------------------------------------------------------------------------


class TestFlushRefinements:
    def test_flush_with_max_total(self) -> None:
        state = ResearchState(query="q")
        for i in range(10):
            state.pending_refinements.append(
                SubQuestion(id=f"r{i}", question=f"Q{i}?", rationale="r"),
            )
        flushed = state.flush_refinements(max_total=3)
        assert len(flushed) == 3
        assert len(state.pending_refinements) == 0
        assert len(state.plan.sub_questions) == 3

    def test_flush_without_cap(self) -> None:
        state = ResearchState(query="q")
        for i in range(5):
            state.pending_refinements.append(
                SubQuestion(id=f"r{i}", question=f"Q{i}?", rationale="r"),
            )
        flushed = state.flush_refinements()
        assert len(flushed) == 5


# ---------------------------------------------------------------------------
# 12: ScopedToolRegistry
# ---------------------------------------------------------------------------


class TestScopedToolRegistry:
    @pytest.mark.asyncio
    async def test_parent_tools_callable_through_scope(self) -> None:
        parent = ToolRegistry()

        async def _parent_tool(**kwargs: Any) -> ToolResult:
            return ToolResult(content="parent result")

        parent.register("parent_tool", _parent_tool, {"description": "p", "parameters": {}})
        scope = ScopedToolRegistry(parent)

        result = await scope.call("parent_tool", {})
        assert result.content == "parent result"

    @pytest.mark.asyncio
    async def test_scoped_tool_not_on_parent(self) -> None:
        parent = ToolRegistry()
        scope = ScopedToolRegistry(parent)

        async def _scoped_tool(**kwargs: Any) -> ToolResult:
            return ToolResult(content="scoped result")

        scope.register("scoped_only", _scoped_tool, {"description": "s", "parameters": {}})

        assert "scoped_only" in scope.names()
        assert "scoped_only" not in parent.names()

        result = await scope.call("scoped_only", {})
        assert result.content == "scoped result"

        parent_result = await parent.call("scoped_only", {})
        assert parent_result.error is not None

    @pytest.mark.asyncio
    async def test_scoped_schemas_include_parent_and_extra(self) -> None:
        parent = ToolRegistry()

        async def _t(**kw: Any) -> ToolResult:
            return ToolResult(content="ok")

        parent.register("t1", _t, {"description": "d1", "parameters": {}})
        scope = ScopedToolRegistry(parent)
        scope.register("t2", _t, {"description": "d2", "parameters": {}})

        schemas = scope.schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "t1" in names
        assert "t2" in names


__all__ = [
    "TestAbsorbRefinements",
    "TestConcurrentIsolation",
    "TestFlushRefinements",
    "TestRefineIntegration",
    "TestRefineTool",
    "TestScopedToolRegistry",
]
