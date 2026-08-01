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

from deep_research.nodes.critic import (
    _parse_paper_requests,
    _render_paper_candidates,
    _render_sections_for_prompt,
    review,
)
from deep_research.state import (
    Citation,
    Critique,
    PaperAnalysis,
    PaperNode,
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
                {
                    "id": "gap1",
                    "question": "What are the implications?",
                    "tool_hint": "general-web",
                    "rationale": "need more",
                },
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

    @pytest.mark.asyncio
    async def test_non_dict_gaps_are_skipped(self) -> None:
        """LLM returns a list-of-lists instead of list-of-dicts for gaps."""
        payload = {
            "sufficient": False,
            "rationale": "r",
            "gaps": [
                ["id1", "question1", "general-web", "rationale1"],  # list, not dict
                {"id": "g2", "question": "Valid gap", "tool_hint": "general-web", "rationale": "r"},
            ],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        state = _state(drafts={"sq1": "draft"})
        out = await review(state, client, "m")
        # Only the dict gap should be kept
        assert len(out.gaps) == 1
        assert out.gaps[0].id == "g2"
        assert out.gaps[0].question == "Valid gap"


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


# ---------------------------------------------------------------------------
# Paper-analysis candidates + proposals
# ---------------------------------------------------------------------------


class TestPaperCandidates:
    def _state_with_paper(self, *, arxiv_id: str = "2401.00001", draft: str = "") -> ResearchState:
        state = _state(drafts={"sq1": draft or f"See https://arxiv.org/abs/{arxiv_id}"})
        state.absorb_citations(
            [
                Citation(
                    url=f"https://arxiv.org/abs/{arxiv_id}",
                    title="Paper A",
                    snippet="This is the abstract of a paper.",
                    arxiv_id=arxiv_id,
                    confidence_score=0.9,
                )
            ]
        )
        return state

    def test_renders_candidate_with_reference_count(self) -> None:
        state = self._state_with_paper(
            draft="Mention https://arxiv.org/abs/2401.00001 twice in one draft"
        )
        out = _render_paper_candidates(state)
        assert "arxiv:2401.00001" in out
        assert "referenced_in_drafts=1" in out
        assert "abstract of a paper" in out

    def test_counts_references_across_distinct_drafts(self) -> None:
        state = _state(
            drafts={
                "sq1": "Cites https://arxiv.org/abs/2401.00001",
                "sq2": "Also cites 2401.00001",
            }
        )
        state.absorb_citations(
            [Citation(url="https://arxiv.org/abs/2401.00001", title="A", arxiv_id="2401.00001")]
        )
        out = _render_paper_candidates(state)
        assert "referenced_in_drafts=2" in out

    def test_excludes_analyzed_and_requested(self) -> None:
        state = self._state_with_paper()
        state.deep_analyses["2401.00001"] = PaperAnalysis(title="A", summary="s")
        state.deep_analysis_requested.append("2401.00001")
        out = _render_paper_candidates(state)
        assert "arxiv:2401.00001" not in out

    def test_renders_deep_analysis_digest(self) -> None:
        state = _state(drafts={"sq1": "draft"})
        state.deep_analyses["2401.00001"] = PaperAnalysis(
            title="Deep Paper", summary="digest summary"
        )
        out = _render_sections_for_prompt(state)
        assert "Deep paper analyses" in out
        assert "arxiv:2401.00001" in out
        assert "digest summary" in out

    def test_key_references_become_candidates(self) -> None:
        state = _state(drafts={"sq1": "draft"})
        state.deep_analyses["2401.00001"] = PaperAnalysis(
            title="Analyzed",
            summary="s",
            key_references=[PaperNode(arxiv_id="2401.00002", title="Ref Paper")],
        )
        out = _render_paper_candidates(state)
        assert "arxiv:2401.00002" in out
        assert "key reference" in out

    def test_ignores_non_arxiv_citations(self) -> None:
        state = _state(drafts={"sq1": "web only"})
        state.absorb_citations([Citation(url="https://example.com/x", title="Web", snippet="s")])
        out = _render_paper_candidates(state)
        assert "(no arxiv paper candidates)" in out


class TestParsePaperRequests:
    def test_parses_and_clamps(self) -> None:
        raw = [
            {"arxiv_id": "2401.00001", "rationale": "r", "priority_score": 1.5},
            {"arxiv_id": "2401.00002", "reason": "foundational", "priority_score": 0.4},
            {"arxiv_id": "not-an-arxiv-id", "priority_score": 0.9},
            {"arxiv_id": "scholar:abc", "priority_score": 0.9},
            "garbage",
        ]
        out = _parse_paper_requests(raw)
        assert [p.arxiv_id for p in out] == ["2401.00001", "2401.00002"]
        assert out[0].priority_score == 1.0  # clamped
        assert out[1].reason == "foundational"

    def test_dedupe_keeps_highest_priority(self) -> None:
        raw = [
            {"arxiv_id": "2401.00001", "priority_score": 0.5},
            {"arxiv_id": "2401.00001", "priority_score": 0.9},
        ]
        out = _parse_paper_requests(raw)
        assert len(out) == 1
        assert out[0].priority_score == 0.9

    def test_non_list_returns_empty(self) -> None:
        assert _parse_paper_requests(None) == []
        assert _parse_paper_requests({"arxiv_id": "2401.00001"}) == []


class TestReviewPaperProposals:
    @pytest.mark.asyncio
    async def test_parses_papers_to_analyze_from_llm(self) -> None:
        payload = {
            "sufficient": True,
            "rationale": "covered",
            "gaps": [],
            "papers_to_analyze": [
                {
                    "arxiv_id": "2401.00001",
                    "rationale": "seminal",
                    "reason": "foundational",
                    "priority_score": 0.95,
                    "expected_title": "Seminal Paper",
                }
            ],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        state = _state(drafts={"sq1": "draft"})
        out = await review(state, client, "m")
        assert len(out.papers_to_analyze) == 1
        req = out.papers_to_analyze[0]
        assert req.arxiv_id == "2401.00001"
        assert req.reason == "foundational"
        assert req.priority_score == 0.95


__all__ = ["TestRenderSectionsForPrompt", "TestReviewFallback", "TestReviewHappyPath"]
