"""Dedicated unit tests for `nodes.writer.write` (P3).

Covers:
  - Happy path: LLM returns markdown, fence stripping
  - _render_sections_for_prompt: renders drafts per sub-question
  - _render_citations_for_prompt: renders citations
  - _concatenate_drafts: deterministic fallback on LLM failure
  - LLM exception: fallback to concatenated drafts
  - Empty drafts: fallback to placeholder
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.nodes.writer import (
    _concatenate_drafts,
    _render_citations_for_prompt,
    _render_sections_for_prompt,
    write,
)
from deep_research.state import Citation, ResearchPlan, ResearchState, SubQuestion

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(
    query: str = "test query",
    drafts: dict[str, str] | None = None,
    citations: list[Citation] | None = None,
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
    if citations:
        for c in citations:
            s.citations[c.url] = c
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
    def test_renders_drafts_as_sections(self) -> None:
        state = _state(drafts={"sq1": "Draft body for Q1", "sq2": "Draft body for Q2"})
        out = _render_sections_for_prompt(state)
        assert "## Q1?" in out
        assert "Draft body for Q1" in out
        assert "## Q2?" in out
        assert "Draft body for Q2" in out

    def test_skips_missing_drafts(self) -> None:
        state = _state(drafts={"sq1": "only sq1 has a draft"})
        out = _render_sections_for_prompt(state)
        assert "## Q1?" in out
        assert "## Q2?" not in out  # no draft, section omitted

    def test_no_drafts_returns_placeholder(self) -> None:
        state = _state()
        out = _render_sections_for_prompt(state)
        assert "(no drafts available)" in out


# ---------------------------------------------------------------------------
# _render_citations_for_prompt
# ---------------------------------------------------------------------------


class TestRenderCitationsForPrompt:
    def test_renders_citation_list(self) -> None:
        state = _state(
            citations=[
                Citation(url="https://a", title="A", snippet="snip a"),
                Citation(url="https://b", title="B", snippet="snip b"),
            ],
        )
        out = _render_citations_for_prompt(state)
        assert "https://a" in out
        assert "https://b" in out
        assert "snip a" in out
        assert "snip b" in out

    def test_empty_citations_returns_placeholder(self) -> None:
        state = _state()
        out = _render_citations_for_prompt(state)
        assert "(no citations available)" in out


# ---------------------------------------------------------------------------
# _concatenate_drafts
# ---------------------------------------------------------------------------


class TestConcatenateDrafts:
    def test_renders_all_drafts_in_order(self) -> None:
        state = _state(drafts={"sq1": "Draft A", "sq2": "Draft B"})
        out = _concatenate_drafts(state)
        assert "# Report" in out
        assert "test query" in out
        assert "## Q1?" in out
        assert "Draft A" in out
        assert "## Q2?" in out
        assert "Draft B" in out

    def test_no_drafts_returns_query_only(self) -> None:
        state = _state()
        out = _concatenate_drafts(state)
        assert "# Report" in out
        assert "test query" in out
        assert "## " not in out  # no sub-question sections

    def test_skips_missing_drafts(self) -> None:
        state = _state(drafts={"sq1": "only draft"})
        out = _concatenate_drafts(state)
        assert "## Q1?" in out
        assert "## Q2?" not in out


# ---------------------------------------------------------------------------
# write() — happy path
# ---------------------------------------------------------------------------


class TestWriteHappyPath:
    @pytest.mark.asyncio
    async def test_returns_llm_markdown(self) -> None:
        client = _FakeAsyncOpenAI("# Final Report\n\nThis is the synthesized report.")
        state = _state(drafts={"sq1": "draft"})
        out = await write(state, client, "m")
        assert "# Final Report" in out
        assert "synthesized report" in out

    @pytest.mark.asyncio
    async def test_strips_code_fences(self) -> None:
        client = _FakeAsyncOpenAI("```markdown\n# Report\ninside\n```")
        state = _state(drafts={"sq1": "draft"})
        out = await write(state, client, "m")
        assert "```" not in out
        assert "# Report" in out
        assert "inside" in out


# ---------------------------------------------------------------------------
# write() — fallback
# ---------------------------------------------------------------------------


class TestWriteFallback:
    @pytest.mark.asyncio
    async def test_llm_exception_falls_back_to_concatenated_drafts(self) -> None:
        client = _raising_client(RuntimeError("writer down"))
        state = _state(drafts={"sq1": "Draft A", "sq2": "Draft B"})
        out = await write(state, client, "m")
        assert "# Report" in out
        assert "test query" in out
        assert "Draft A" in out
        assert "Draft B" in out

    @pytest.mark.asyncio
    async def test_empty_llm_response_returns_empty_string(self) -> None:
        client = _FakeAsyncOpenAI("")
        state = _state(drafts={"sq1": "draft content"})
        out = await write(state, client, "m")
        assert out == ""  # empty response passes through, fallback only on exception

    @pytest.mark.asyncio
    async def test_no_drafts_fallback_renders_query(self) -> None:
        client = _raising_client(RuntimeError("down"))
        state = _state()  # no drafts
        out = await write(state, client, "m")
        assert "# Report" in out
        assert "test query" in out


__all__ = [
    "TestConcatenateDrafts",
    "TestRenderCitationsForPrompt",
    "TestRenderSectionsForPrompt",
    "TestWriteFallback",
    "TestWriteHappyPath",
]
