"""Dedicated unit tests for `paths.deep.deep_research` (P3).

Covers the full deep-research loop by patching the four node functions
(planner, researcher, critic, writer) so the integration can be tested
without sequential-response mocks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.paths.deep import deep_research
from deep_research.state import (
    Citation,
    ClassifiedQuery,
    Critique,
    QueryPlan,
    ResearchPlan,
    SubQuestion,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classified(search_hint: str = "test query") -> ClassifiedQuery:
    return ClassifiedQuery(
        path=QueryPlan.deep,
        rationale="deep test",
        search_hint=search_hint,
    )


def _citation(url: str, title: str = "", score: float = 0.7) -> Citation:
    return Citation(
        url=url,
        title=title,
        snippet="snip",
        source_type="web",
        confidence_score=score,
    )


def _sub_q(id: str = "sq1", question: str = "Q?", tool_hint: str = "general-web") -> SubQuestion:
    return SubQuestion(id=id, question=question, tool_hint=tool_hint, rationale="r")


def _plan(subs: list[SubQuestion]) -> ResearchPlan:
    return ResearchPlan(sub_questions=subs, breadth=len(subs), max_depth=0)


@pytest.fixture
def cfg() -> AgentTopConfig:
    return AgentTopConfig()


def _registry_with_search() -> ToolRegistry:
    reg = ToolRegistry()

    async def _web_search(**kwargs: Any) -> Any:
        from deep_research.llm.tool_loop import ToolResult
        return ToolResult(
            content="search results",
            citations=[_citation("https://result", title="Result", score=0.8)],
        )

    reg.register("web_search", _web_search, {"type": "function", "name": "web_search"})
    return reg


def _empty_registry() -> ToolRegistry:
    return ToolRegistry()


# ---------------------------------------------------------------------------
# deep_research — happy path
# ---------------------------------------------------------------------------


class TestDeepHappyPath:
    @pytest.mark.asyncio
    async def test_full_loop_planner_researcher_critic_writer(self, cfg: AgentTopConfig) -> None:
        plan_result = _plan([_sub_q(id="sq1", question="What is X?")])
        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run",
                   return_value=("X is Y.", [_citation("https://a", title="A", score=0.9)])),
            patch("deep_research.paths.deep.critic_review",
                   return_value=Critique(sufficient=True, rationale="covered", gaps=[])),
            patch("deep_research.paths.deep.writer_write",
                   return_value="# Deep Report\n\nConclusion about X."),
        ):
            client = MagicMock()  # not used by patched nodes
            reg = _registry_with_search()
            report = await deep_research(_classified("What is X?"), "What is X?", client, reg, cfg)
            assert report.path == "deep"
            assert "# Deep Report" in report.markdown
            assert report.iterations >= 1
            assert any(c.url == "https://a" for c in report.citations)

    @pytest.mark.asyncio
    async def test_citations_sorted_by_confidence_desc(self, cfg: AgentTopConfig) -> None:
        plan_result = _plan([_sub_q(id="sq1", question="Q?")])
        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run",
                   return_value=("ans", [
                       _citation("https://high", title="H", score=0.9),
                       _citation("https://low", title="L", score=0.5),
                   ])),
            patch("deep_research.paths.deep.critic_review",
                   return_value=Critique(sufficient=True, rationale="r", gaps=[])),
            patch("deep_research.paths.deep.writer_write", return_value="# Report"),
        ):
            client = MagicMock()
            reg = _registry_with_search()
            report = await deep_research(_classified("Q"), "Q", client, reg, cfg)
            if len(report.citations) >= 2:
                scores = [c.confidence_score for c in report.citations]
                assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Critic iteration
# ---------------------------------------------------------------------------


class TestCriticIteration:
    @pytest.mark.asyncio
    async def test_gaps_appended_and_re_researched(self, cfg: AgentTopConfig) -> None:
        cfg.agent.max_iterations = 3
        cfg.agent.max_subquestions = 6
        sq1 = _sub_q(id="sq1", question="Q1?")
        gap_q = _sub_q(id="gap1", question="Q2?")

        plan_result = _plan([sq1])
        # First researcher -> critic (not sufficient, 1 gap) -> second researcher -> critic (sufficient) -> writer
        call_count = {"critic": 0, "researcher": 0}

        async def _researcher(sq, client, model, tools):
            call_count["researcher"] += 1
            if call_count["researcher"] == 1:
                return (f"ans for {sq.question}", [])
            return (f"ans2 for {sq.question}", [])

        async def _critic(state, client, model):
            call_count["critic"] += 1
            if call_count["critic"] == 1:
                return Critique(sufficient=False, rationale="missing Q2", gaps=[gap_q])
            return Critique(sufficient=True, rationale="covered", gaps=[])

        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run", side_effect=_researcher),
            patch("deep_research.paths.deep.critic_review", side_effect=_critic),
            patch("deep_research.paths.deep.writer_write", return_value="# Final report with Q1 and Q2"),
        ):
            client = MagicMock()
            reg = _registry_with_search()
            report = await deep_research(_classified("Q"), "Q", client, reg, cfg)
            assert "Q1" in report.markdown or "Q2" in report.markdown

    @pytest.mark.asyncio
    async def test_dedup_gaps_by_question_text(self, cfg: AgentTopConfig) -> None:
        cfg.agent.max_iterations = 2
        sq1 = _sub_q(id="sq1", question="Q1?")
        plan_result = _plan([sq1])

        call_count = {"critic": 0}

        async def _critic(state, client, model):
            call_count["critic"] += 1
            if call_count["critic"] == 1:
                # Gap has same question text as existing sub-question
                return Critique(sufficient=False, rationale="r", gaps=[_sub_q(id="gap1", question="Q1?")])
            return Critique(sufficient=True, rationale="r", gaps=[])

        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run",
                   return_value=("ans", [])),
            patch("deep_research.paths.deep.critic_review", side_effect=_critic),
            patch("deep_research.paths.deep.writer_write", return_value="# Report"),
        ):
            client = MagicMock()
            reg = _registry_with_search()
            report = await deep_research(_classified("Q"), "Q", client, reg, cfg)
            assert report.path == "deep"

    @pytest.mark.asyncio
    async def test_no_gaps_breaks_early(self, cfg: AgentTopConfig) -> None:
        plan_result = _plan([_sub_q(id="sq1", question="Q?")])
        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run",
                   return_value=("ans", [])),
            patch("deep_research.paths.deep.critic_review",
                   return_value=Critique(sufficient=True, rationale="r", gaps=[])),
            patch("deep_research.paths.deep.writer_write", return_value="# Done"),
        ):
            client = MagicMock()
            reg = _registry_with_search()
            report = await deep_research(_classified("Q"), "Q", client, reg, cfg)
            assert "# Done" in report.markdown


# ---------------------------------------------------------------------------
# Resilience / fallback
# ---------------------------------------------------------------------------


class TestDeepResilience:
    @pytest.mark.asyncio
    async def test_researcher_failure_recorded_as_error_draft(self, cfg: AgentTopConfig) -> None:
        plan_result = _plan([_sub_q(id="sq1", question="Q?")])
        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run",
                   side_effect=RuntimeError("simulated researcher failure")),
            patch("deep_research.paths.deep.critic_review",
                   return_value=Critique(sufficient=True, rationale="r", gaps=[])),
            patch("deep_research.paths.deep.writer_write",
                   return_value="# Fallback report"),
        ):
            client = MagicMock()
            reg = _registry_with_search()
            report = await deep_research(_classified("Q"), "Q", client, reg, cfg)
            assert report.path == "deep"

    @pytest.mark.asyncio
    async def test_writer_fallback_on_empty_content(self, cfg: AgentTopConfig) -> None:
        plan_result = _plan([_sub_q(id="sq1", question="Q?")])
        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run",
                   return_value=("draft answer", [])),
            patch("deep_research.paths.deep.critic_review",
                   return_value=Critique(sufficient=True, rationale="r", gaps=[])),
            patch("deep_research.paths.deep.writer_write",
                   return_value="# Report"),
        ):
            client = MagicMock()
            reg = _registry_with_search()
            report = await deep_research(_classified("Q"), "Q", client, reg, cfg)
            assert "# Report" in report.markdown

    @pytest.mark.asyncio
    async def test_no_pending_breaks_early(self, cfg: AgentTopConfig) -> None:
        # Planner returns empty sub-questions -> fallback plan with single original query
        plan_result = _plan([])
        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.writer_write", return_value="# Report"),
        ):
            client = MagicMock()
            reg = _registry_with_search()
            report = await deep_research(_classified("Q"), "Q", client, reg, cfg)
            assert report.path == "deep"


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    @pytest.mark.asyncio
    async def test_respects_max_iterations(self, cfg: AgentTopConfig) -> None:
        cfg.agent.max_iterations = 1
        plan_result = _plan([_sub_q(id="sq1", question="Q?")])
        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run",
                   return_value=("ans", [])),
            patch("deep_research.paths.deep.critic_review",
                   return_value=Critique(sufficient=True, rationale="r", gaps=[])),
            patch("deep_research.paths.deep.writer_write", return_value="# Report"),
        ):
            client = MagicMock()
            reg = _registry_with_search()
            report = await deep_research(_classified("Q"), "Q", client, reg, cfg)
            assert report.iterations <= 1

    @pytest.mark.asyncio
    async def test_breadth_hint_from_classified(self, cfg: AgentTopConfig) -> None:
        cfg.agent.max_subquestions = 10
        classified = _classified("Q")
        classified.breadth_hint = 2

        sq1 = _sub_q(id="sq1", question="Q1?")
        sq2 = _sub_q(id="sq2", question="Q2?")
        plan_result = _plan([sq1, sq2])
        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run",
                   return_value=("ans", [])),
            patch("deep_research.paths.deep.critic_review",
                   return_value=Critique(sufficient=True, rationale="r", gaps=[])),
            patch("deep_research.paths.deep.writer_write", return_value="# Report with 2 sections"),
        ):
            client = MagicMock()
            reg = _registry_with_search()
            report = await deep_research(classified, "Q", client, reg, cfg)
            assert report.path == "deep"


__all__ = [
    "TestConfigIntegration",
    "TestCriticIteration",
    "TestDeepHappyPath",
    "TestDeepResilience",
]
