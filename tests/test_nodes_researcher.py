"""Dedicated unit tests for `nodes.researcher.research` (P3).

Covers:
  - Happy path: run_with_tools loop, JSON answer+citations parsing
  - _hint_blurb: tool_hint routing logic (arxiv, reddit, browser-required, general-web)
  - _parse_final_assistant: JSON parse, fallback to raw markdown
  - ToolRegistry integration edge cases
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.llm.tool_loop import ToolRegistry
from deep_research.nodes.researcher import _hint_blurb, _parse_final_assistant, research
from deep_research.state import SubQuestion

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sub_q(question: str = "test question", tool_hint: str = "general-web") -> SubQuestion:
    return SubQuestion(id="sq1", question=question, tool_hint=tool_hint, rationale="test")


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


def _fake_client_always(content: str) -> MagicMock:
    """Return a client whose `create` always returns the same response."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_FakeResponse(content))
    return client


# ---------------------------------------------------------------------------
# _hint_blurb
# ---------------------------------------------------------------------------


class TestHintBlurb:
    def test_general_web_returns_empty(self) -> None:
        assert _hint_blurb("general-web", ["web_search"]) == ""

    def test_arxiv_with_tool_available(self) -> None:
        out = _hint_blurb("arxiv", ["arxiv_search", "web_search"])
        assert "arxiv_search" in out

    def test_arxiv_without_tool_returns_empty(self) -> None:
        assert _hint_blurb("arxiv", ["web_search"]) == ""

    def test_reddit_with_tool_available(self) -> None:
        out = _hint_blurb("reddit", ["reddit_search", "web_search"])
        assert "Reddit" in out

    def test_reddit_without_tool_returns_empty(self) -> None:
        assert _hint_blurb("reddit", ["web_search"]) == ""

    def test_browser_required_with_tool(self) -> None:
        out = _hint_blurb("browser-required", ["browser_navigate"])
        assert "browser_navigate" in out

    def test_browser_required_without_tool_returns_empty(self) -> None:
        assert _hint_blurb("browser-required", ["web_search"]) == ""

    def test_empty_hint_returns_empty(self) -> None:
        assert _hint_blurb("", ["web_search"]) == ""


# ---------------------------------------------------------------------------
# _parse_final_assistant
# ---------------------------------------------------------------------------


class TestParseFinalAssistant:
    def test_valid_json_with_citations(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "answer": "Paris is the capital.",
                        "citations": [
                            {
                                "url": "https://a",
                                "title": "A",
                                "snippet": "s",
                                "confidence_score": 0.9,
                            },
                            {"url": "https://b", "title": "B", "snippet": "s2"},
                        ],
                    }
                ),
            },
        ]
        answer, cites = _parse_final_assistant(messages)
        assert "Paris" in answer
        assert len(cites) == 2
        assert cites[0].url == "https://a"
        assert cites[0].confidence_score == 0.9

    def test_valid_json_no_citations(self) -> None:
        messages = [
            {"role": "assistant", "content": json.dumps({"answer": "just text", "citations": []})},
        ]
        answer, cites = _parse_final_assistant(messages)
        assert answer == "just text"
        assert cites == []

    def test_drops_citation_without_url(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "answer": "text",
                        "citations": [{"title": "no url"}],
                    }
                ),
            },
        ]
        _, cites = _parse_final_assistant(messages)
        assert cites == []

    def test_invalid_json_returns_raw_content(self) -> None:
        messages = [
            {"role": "assistant", "content": "plain markdown answer"},
        ]
        answer, cites = _parse_final_assistant(messages)
        assert answer == "plain markdown answer"
        assert cites == []

    def test_no_assistant_messages_returns_empty_answer(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        answer, cites = _parse_final_assistant(messages)
        assert "(no answer synthesized)" in answer
        assert cites == []

    def test_last_assistant_wins(self) -> None:
        messages = [
            {"role": "assistant", "content": "first"},
            {"role": "user", "content": "tool result"},
            {"role": "assistant", "content": json.dumps({"answer": "final", "citations": []})},
        ]
        answer, _ = _parse_final_assistant(messages)
        assert answer == "final"

    def test_empty_assistant_content_skipped(self) -> None:
        messages = [
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": json.dumps({"answer": "real", "citations": []})},
        ]
        answer, _ = _parse_final_assistant(messages)
        assert answer == "real"


# ---------------------------------------------------------------------------
# research() integration
# ---------------------------------------------------------------------------


class TestResearch:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        payload = json.dumps({"answer": "found results", "citations": []})
        client = _fake_client_always(payload)

        reg = ToolRegistry()
        answer, cites, refs = await research(_sub_q(), client, "m", reg)
        assert "found results" in answer
        assert cites == []
        assert refs == []

    @pytest.mark.asyncio
    async def test_parses_citations_from_final_message(self) -> None:
        payload = json.dumps(
            {
                "answer": "answer with refs",
                "citations": [
                    {
                        "url": "https://ref",
                        "title": "ref",
                        "snippet": "snip",
                        "confidence_score": 0.8,
                    }
                ],
            }
        )
        client = _fake_client_always(payload)

        reg = ToolRegistry()
        _, cites, _ = await research(_sub_q(), client, "m", reg)
        assert len(cites) == 1
        assert cites[0].url == "https://ref"

    @pytest.mark.asyncio
    async def test_non_json_fallback(self) -> None:
        client = _fake_client_always("plain text answer")
        reg = ToolRegistry()
        answer, cites, _ = await research(_sub_q(), client, "m", reg)
        assert "plain text answer" in answer
        assert cites == []

    @pytest.mark.asyncio
    async def test_hint_blurb_prepended_when_applicable(self) -> None:
        """Verify the hint blurb is included when tool_hint matches available tools."""
        captured: list[dict] = []

        async def _capture(**kwargs: Any) -> _FakeResponse:
            captured.append(kwargs)
            return _FakeResponse(json.dumps({"answer": "done", "citations": []}))

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_capture)

        reg = ToolRegistry()

        async def _dummy(**kw: Any) -> Any:
            from deep_research.llm.tool_loop import ToolResult

            return ToolResult(content="ok")

        reg.register("arxiv_search", _dummy, {"type": "function", "name": "arxiv_search"})
        sq = _sub_q(question="arxiv question", tool_hint="arxiv")
        await research(sq, client, "m", reg)

        msgs = captured[0]["messages"]
        user_content = msgs[1]["content"]
        assert "arxiv_search" in user_content  # hint blurb was prepended

    @pytest.mark.asyncio
    async def test_no_hint_for_general_web(self) -> None:
        captured: list[dict] = []

        async def _capture(**kwargs: Any) -> _FakeResponse:
            captured.append(kwargs)
            return _FakeResponse(json.dumps({"answer": "done", "citations": []}))

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_capture)

        reg = ToolRegistry()
        sq = _sub_q(question="general question")
        await research(sq, client, "m", reg)

        msgs = captured[0]["messages"]
        user_content = msgs[1]["content"]
        # No hint blurb for general-web
        assert "Hint:" not in user_content


__all__ = ["TestHintBlurb", "TestParseFinalAssistant", "TestResearch"]
