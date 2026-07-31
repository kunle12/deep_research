"""Dedicated unit tests for `deep_research.paths.quick.quick_search`.

The quick path is the simplest real-LLM path:
    web_search -> fetch_page top-k -> single LLM synthesis -> Report.

These tests mock both `AsyncOpenAI` and the `ToolRegistry` so they run
fully offline and deterministically. They complement `test_agent.py`
(which only exercises routing) by directly asserting `quick_search`
behavior: citation merging, LLM JSON parsing, fetch failures, missing
tools, and search errors.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.paths.quick import (
    MAX_PAGES_TO_FETCH,
    _merge_citations,
    _render_for_llm,
    quick_search,
)
from deep_research.state import Citation, ClassifiedQuery, QueryPlan, ToolName

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _classified(search_hint: str = "what is the capital of France") -> ClassifiedQuery:
    return ClassifiedQuery(
        path=QueryPlan.quick,
        rationale="simple factual query",
        search_hint=search_hint,
    )


def _citation(url: str, title: str = "", snippet: str = "", score: float = 0.7) -> Citation:
    return Citation(
        url=url,
        title=title,
        snippet=snippet,
        source_type="web",
        confidence_score=score,
        discovered_by=ToolName.web_search,
    )


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


class _FakeChatCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.create = AsyncMock(return_value=_FakeResponse(self._content))


class _FakeAsyncOpenAI:
    """Minimal AsyncOpenAI double exposing only `.chat.completions.create`."""

    def __init__(self, content: str) -> None:
        self.chat = MagicMock()
        self.chat.completions = _FakeChatCompletions(content)


@pytest.fixture
def cfg() -> AgentTopConfig:
    return AgentTopConfig()


def _registry_with_tools(
    search_citations: list[Citation] | None = None,
    search_error: str | None = None,
    fetch_results: dict[str, ToolResult] | None = None,
    fetch_error_urls: set[str] | None = None,
) -> ToolRegistry:
    """Build a ToolRegistry with stubbed `web_search` and `fetch_page`.

    `fetch_results` maps url -> ToolResult. URLs in `fetch_error_urls` raise
    an exception (to exercise the gather(return_exceptions=True) branch).
    """
    reg = ToolRegistry()
    search_citations = search_citations if search_citations is not None else []
    fetch_results = fetch_results or {}
    fetch_error_urls = fetch_error_urls or set()

    async def _web_search(**kwargs: Any) -> ToolResult:
        if search_error:
            return ToolResult(content="", error=search_error)
        return ToolResult(content="search ok", citations=list(search_citations))

    async def _fetch_page(**kwargs: Any) -> ToolResult:
        url = kwargs["url"]
        if url in fetch_error_urls:
            raise RuntimeError(f"simulated fetch failure for {url}")
        if url in fetch_results:
            return fetch_results[url]
        return ToolResult(content="", error="not mocked")

    reg.register("web_search", _web_search, {"type": "function", "name": "web_search"})
    reg.register("fetch_page", _fetch_page, {"type": "function", "name": "fetch_page"})
    return reg


def _registry_without_tools() -> ToolRegistry:
    """Registry with neither web_search nor fetch_page registered."""
    return ToolRegistry()


# ---------------------------------------------------------------------------
# _merge_citations
# ---------------------------------------------------------------------------


class TestMergeCitations:
    def test_dedup_by_url_keep_higher_confidence(self) -> None:
        base = [_citation("https://a", score=0.5)]
        additions = [_citation("https://a", title="better", score=0.9)]
        merged = _merge_citations(base, additions)
        assert len(merged) == 1
        assert merged[0].confidence_score == 0.9
        assert merged[0].title == "better"

    def test_disjoint_urls_kept_all(self) -> None:
        base = [_citation("https://a"), _citation("https://b", score=0.3)]
        additions = [_citation("https://c", score=0.5)]
        merged = _merge_citations(base, additions)
        assert {c.url for c in merged} == {"https://a", "https://b", "https://c"}

    def test_additions_do_not_override_lower_confidence(self) -> None:
        base = [_citation("https://a", score=0.9)]
        additions = [_citation("https://a", score=0.4)]
        merged = _merge_citations(base, additions)
        assert len(merged) == 1
        assert merged[0].confidence_score == 0.9


# ---------------------------------------------------------------------------
# _render_for_llm
# ---------------------------------------------------------------------------


class TestRenderForLLM:
    def test_renders_query_and_results(self) -> None:
        cits = [
            _citation("https://a", title="A", snippet="snip A"),
            _citation("https://b", title="B", snippet="snip B"),
        ]
        sr = ToolResult(content="search ok", citations=cits)
        out = _render_for_llm("q?", sr, [])
        assert "q?" in out
        assert "https://a" in out
        assert "https://b" in out
        assert "snip A" in out

    def test_renders_search_error_when_no_citations(self) -> None:
        sr = ToolResult(content="", citations=[], error="boom")
        out = _render_for_llm("q?", sr, [])
        assert "boom" in out
        assert "no fetched pages" in out

    def test_renders_fetched_pages_block(self) -> None:
        sr = ToolResult(content="ok", citations=[_citation("https://a")])
        pages = ["=== Source: https://a ===\nbody text here"]
        out = _render_for_llm("q?", sr, pages)
        assert "Fetched page contents" in out
        assert "body text here" in out


# ---------------------------------------------------------------------------
# quick_search end-to-end with mocks
# ---------------------------------------------------------------------------


class TestQuickSearch:
    @pytest.mark.asyncio
    async def test_happy_path_merges_search_and_llm_citations(self, cfg: AgentTopConfig) -> None:
        # web_search returns 2 hits, both fetched OK, LLM emits an answer + 1 citation
        search_cits = [
            _citation("https://a", title="A", snippet="snip A", score=0.9),
            _citation("https://b", title="B", snippet="snip B", score=0.6),
            _citation("https://c", title="C", snippet="snip C", score=0.4),
        ]
        fetch_results = {
            "https://a": ToolResult(
                content="Page A body.",
                citations=[_citation("https://a", title="A", score=1.0)],
            ),
            "https://b": ToolResult(
                content="Page B body.",
                citations=[_citation("https://b", title="B", score=0.8)],
            ),
            "https://c": ToolResult(
                content="Page C body.",
                citations=[],
            ),
        }
        reg = _registry_with_tools(search_citations=search_cits, fetch_results=fetch_results)
        llm_payload = {
            "answer": "Paris is the capital of France [https://a].",
            "citations": [
                {
                    "url": "https://a",
                    "title": "A",
                    "snippet": "snip A",
                    "confidence_score": 0.95,
                },
                {
                    "url": "https://d",
                    "title": "D-new",
                    "snippet": "extra",
                    "confidence_score": 0.7,
                },
            ],
        }
        client = _FakeAsyncOpenAI(json.dumps(llm_payload))

        report = await quick_search(
            _classified(), "What is the capital of France?", client, reg, cfg
        )

        assert report.path == "quick"
        assert report.classifier_rationale == "simple factual query"
        assert "Paris" in report.markdown
        # All 4 distinct URLs should be present (a, b, c from search/fetch + d from LLM)
        urls = {c.url for c in report.citations}
        assert urls == {"https://a", "https://b", "https://c", "https://d"}
        # The LLM-emitted 'a' (0.95) overrides the search 'a' (0.9) via merge.
        # Note: quick.py only appends *non-equal-URL* fetch citations back into the
        # main citations list, so the fetch_page 'a' (1.0) does not enter the merge
        # (it's deduplicated against the search URL it was fetched from).
        a_cit = next(c for c in report.citations if c.url == "https://a")
        assert a_cit.confidence_score == 0.95

    @pytest.mark.asyncio
    async def test_caps_to_max_pages_to_fetch(self, cfg: AgentTopConfig) -> None:
        # 5 search hits, only MAX_PAGES_TO_FETCH should be fetched
        search_cits = [_citation(f"https://h{i}", title=f"H{i}") for i in range(5)]
        fetched: dict[str, ToolResult] = {
            f"https://h{i}": ToolResult(content=f"body {i}") for i in range(5)
        }
        reg = _registry_with_tools(search_citations=search_cits, fetch_results=fetched)

        # Track which fetches actually happen
        fetched_urls: list[str] = []

        async def _spy_fetch(**kwargs: Any) -> ToolResult:
            fetched_urls.append(kwargs["url"])
            return fetched[kwargs["url"]]

        reg._tools["fetch_page"] = _spy_fetch

        client = _FakeAsyncOpenAI(json.dumps({"answer": "ok", "citations": []}))
        await quick_search(_classified(), "q", client, reg, cfg)

        # Only the top-MAX_PAGES_TO_FETCH URLs should be fetched
        assert len(fetched_urls) == MAX_PAGES_TO_FETCH
        assert fetched_urls == [f"https://h{i}" for i in range(MAX_PAGES_TO_FETCH)]

    @pytest.mark.asyncio
    async def test_fetch_failures_are_logged_not_fatal(self, cfg: AgentTopConfig) -> None:
        # top URL raises an exception; second is OK
        search_cits = [
            _citation("https://fail", title="Fail"),
            _citation("https://ok", title="OK"),
        ]
        reg = _registry_with_tools(
            search_citations=search_cits,
            fetch_results={"https://ok": ToolResult(content="ok body")},
            fetch_error_urls={"https://fail"},
        )
        client = _FakeAsyncOpenAI(json.dumps({"answer": "answer", "citations": []}))
        report = await quick_search(_classified(), "q", client, reg, cfg)
        assert report.path == "quick"
        assert "answer" in report.markdown
        # The non-failing URL citation should still be present
        assert any(c.url == "https://ok" for c in report.citations)

    @pytest.mark.asyncio
    async def test_invalid_llm_json_falls_back_to_raw_text(self, cfg: AgentTopConfig) -> None:
        reg = _registry_with_tools(search_citations=[_citation("https://a")])
        client = _FakeAsyncOpenAI("this is not json")
        report = await quick_search(_classified(), "q", client, reg, cfg)
        # Falls back to raw LLM content as the answer
        assert "this is not json" in report.markdown
        # No LLM citations, but search citation still present
        assert any(c.url == "https://a" for c in report.citations)

    @pytest.mark.asyncio
    async def test_llm_exception_returns_fallback_answer(self, cfg: AgentTopConfig) -> None:
        reg = _registry_with_tools(search_citations=[_citation("https://a")])
        # Configure the mock to raise on create
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("LLM down"))
        report = await quick_search(_classified(), "q", client, reg, cfg)
        assert "Could not synthesize" in report.markdown
        assert "RuntimeError" in report.markdown
        # Search citations still preserved
        assert any(c.url == "https://a" for c in report.citations)

    @pytest.mark.asyncio
    async def test_no_web_search_tool_registered(self, cfg: AgentTopConfig) -> None:
        reg = _registry_without_tools()
        client = _FakeAsyncOpenAI(json.dumps({"answer": "x", "citations": []}))
        report = await quick_search(_classified(), "q", client, reg, cfg)
        # Should not raise; no citations from search
        assert report.path == "quick"
        assert report.citations == []

    @pytest.mark.asyncio
    async def test_search_error_returns_no_citations(self, cfg: AgentTopConfig) -> None:
        reg = _registry_with_tools(search_citations=[], search_error="boom")
        client = _FakeAsyncOpenAI(json.dumps({"answer": "no results", "citations": []}))
        report = await quick_search(_classified(), "q", client, reg, cfg)
        assert report.path == "quick"
        assert report.citations == []
        assert "no results" in report.markdown

    @pytest.mark.asyncio
    async def test_no_fetch_page_tool_skips_fetch_step(self, cfg: AgentTopConfig) -> None:
        # web_search registered but fetch_page not registered
        reg = ToolRegistry()

        async def _web_search(**kwargs: Any) -> ToolResult:
            return ToolResult(
                content="ok",
                citations=[_citation("https://a", title="A", snippet="snip A")],
            )

        reg.register("web_search", _web_search, {"type": "function", "name": "web_search"})
        client = _FakeAsyncOpenAI(json.dumps({"answer": "ans", "citations": []}))
        report = await quick_search(_classified(), "q", client, reg, cfg)
        assert report.path == "quick"
        assert any(c.url == "https://a" for c in report.citations)
