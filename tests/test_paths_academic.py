"""Dedicated unit tests for `paths.academic` (P7).

Covers the recursive-mining loop fully offline by mocking:
  - the analyze_paper node (returns synthetic PaperAnalysis objects)
  - the arxiv_search / arxiv_resolve / arxiv_download_pdf / pdf_extract_text
    tools via a custom ToolRegistry built from {name: async_callable} dict
  - the AsyncOpenAI client for the final synthesis step

Helpers tested individually:
  - _gather_seeds: empty search_hint, missing arxiv_search tool, search-error
  - download_pdf_once / extract_text: download + extract happy paths and
    failure modes (dl failure -> None, missing tool -> empty)
  - fetch_paper_text_fallback: resolve-only fallback, missing tool -> ""
  - render_pages: missing tool, render error, non-JSON content,
    malformed JSON, happy path with valid data URLs list
  - _synthesize_markdown: empty analyses -> boilerplate, happy path LLM
    synthesis, LLM failure -> deterministic fallback, fallback formatting,
    and blog/web fallback when no arxiv papers are analyzable
  - academic_research end-to-end: respects max_papers cap, dedup via arxiv_id,
    recursion falls below max_depth, citation-graph edges recorded,
    pdf_vision toggle off skips rendering, classifier-provided search_hint
    preferred over original_query
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.nodes.paper_analysis import (
    download_pdf_once,
    extract_text,
    fetch_paper_text_fallback,
    render_pages,
)
from deep_research.paths import academic
from deep_research.paths.academic import (
    _fallback_blog_synthesis,
    _fallback_synthesis,
    _gather_seeds,
    _synthesize_markdown,
    academic_research,
)
from deep_research.state import (
    Citation,
    ClassifiedQuery,
    PaperAnalysis,
    PaperNode,
    QueryPlan,
    ToolName,
)

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


def _fake_router(client, model: str = "text") -> MagicMock:
    """Wrap a fake OpenAI client in a fake LLMRouter exposing `resolve`."""
    router = MagicMock()
    router.resolve.return_value = MagicMock(client=client, model=model, max_context_tokens=131072)
    return router


def _raising_client(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


def _cfg(**overrides: Any) -> AgentTopConfig:
    cfg = AgentTopConfig()
    cfg.academic.max_depth = overrides.pop("max_depth", cfg.academic.max_depth)
    cfg.academic.max_papers = overrides.pop("max_papers", cfg.academic.max_papers)
    cfg.academic.concurrency = overrides.pop("concurrency", cfg.academic.concurrency)
    cfg.academic.seed_count = overrides.pop("seed_count", cfg.academic.seed_count)
    cfg.academic.max_key_references_to_recurse = overrides.pop(
        "max_key_references_to_recurse", cfg.academic.max_key_references_to_recurse
    )
    cfg.pdf_vision.enabled = overrides.pop("pdf_vision_enabled", cfg.pdf_vision.enabled)
    if overrides:
        raise TypeError(f"unknown overrides: {list(overrides)}")
    return cfg


def _classified(**overrides: Any) -> ClassifiedQuery:
    defaults: dict[str, Any] = {
        "path": QueryPlan.academic,
        "rationale": "test classifier",
        "search_hint": "",
    }
    defaults.update(overrides)
    return ClassifiedQuery.model_validate(defaults)


def _citation(arxiv_id: str, title: str = "", authors: list[str] | None = None) -> Citation:
    return Citation(
        url=f"https://arxiv.org/abs/{arxiv_id}",
        title=title or arxiv_id,
        snippet="abstract snippet",
        source_type="arxiv",
        arxiv_id=arxiv_id,
        authors=authors or [],
        confidence_score=0.9,
        discovered_by=ToolName.arxiv,
    )


def _registry(tools: dict[str, Any] | None = None) -> ToolRegistry:
    """Build a ToolRegistry from a {name: async_callable} dict.

    Each callable takes kwargs and returns a ToolResult.
    """
    reg = ToolRegistry()
    for name, fn in (tools or {}).items():
        reg.register(name, fn, {"type": "function", "name": name})
    return reg


def _analysis(
    arxiv_id: str,
    title: str | None = None,
    key_refs: list[str] | None = None,
) -> PaperAnalysis:
    """Build a synthetic PaperAnalysis the mock analyze node returns."""
    return PaperAnalysis(
        title=title or f"Paper {arxiv_id}",
        summary=f"Summary of {arxiv_id}.",
        key_findings=[f"finding for {arxiv_id}"],
        methodology="Mock methodology.",
        limitations=["Mock limitation."],
        key_references=[
            PaperNode(arxiv_id=ref_id, title=f"Ref {ref_id}", rationale="key")
            for ref_id in (key_refs or [])
        ],
    )


# ---------------------------------------------------------------------------
# _gather_seeds
# ---------------------------------------------------------------------------


class TestGatherSeeds:
    @pytest.mark.asyncio
    async def test_empty_search_hint_returns_empty(self) -> None:
        cfg = _cfg()
        classified = _classified(search_hint="")
        reg = _registry({"arxiv_search": _noop_tool})
        out = await _gather_seeds(classified, "ignored", reg, cfg, [])
        assert out == []

    @pytest.mark.asyncio
    async def test_uses_classifier_search_hint_when_set(self) -> None:
        cfg = _cfg(seed_count=3)
        classified = _classified(search_hint="rlhf survey")
        captured: dict[str, Any] = {}

        async def _search(**kwargs: Any) -> ToolResult:
            captured.update(kwargs)
            return ToolResult(content="hit", citations=[_citation("2401.1", "T")])

        reg = _registry({"arxiv_search": _search})
        out = await _gather_seeds(classified, "ignored", reg, cfg, [])
        assert captured["query"] == "rlhf survey"
        assert captured["max_results"] == 3
        assert len(out) == 1
        assert out[0].arxiv_id == "2401.1"

    @pytest.mark.asyncio
    async def test_falls_back_to_original_query_when_no_hint(self) -> None:
        """When classified.search_hint is empty, original_query is used."""
        cfg = _cfg()
        classified = _classified(search_hint="")
        captured: dict[str, Any] = {}

        async def _search(**kwargs: Any) -> ToolResult:
            captured.update(kwargs)
            return ToolResult(content="", citations=[])

        reg = _registry({"arxiv_search": _search})
        # search_hint is empty -> `classified.search_hint or original_query` = original_query
        out = await _gather_seeds(classified, "anyquery", reg, cfg, [])
        assert out == []
        # _search IS called with original_query because search_hint was falsy
        assert captured["query"] == "anyquery"

    @pytest.mark.asyncio
    async def test_falls_back_to_original_query_for_search(self) -> None:
        cfg = _cfg()
        # ClassifiedQuery with search_hint="" but original_query="rlhf"
        classified = _classified(search_hint="rlhf-specific")
        captured: dict[str, Any] = {}

        async def _search(**kwargs: Any) -> ToolResult:
            captured.update(kwargs)
            return ToolResult(content="", citations=[])

        reg = _registry({"arxiv_search": _search})
        await _gather_seeds(classified, "ignored", reg, cfg, [])
        assert captured["query"] == "rlhf-specific"

    @pytest.mark.asyncio
    async def test_missing_arxiv_search_tool_returns_empty(self) -> None:
        cfg = _cfg()
        classified = _classified(search_hint="x")
        reg = _registry()  # nothing registered
        out = await _gather_seeds(classified, "ignored", reg, cfg, [])
        assert out == []

    @pytest.mark.asyncio
    async def test_arxiv_search_error_returns_empty(self) -> None:
        cfg = _cfg()
        classified = _classified(search_hint="x")

        async def _search(**kwargs: Any) -> ToolResult:
            return ToolResult(content="", error="503")

        reg = _registry({"arxiv_search": _search})
        out = await _gather_seeds(classified, "ignored", reg, cfg, [])
        assert out == []

    @pytest.mark.asyncio
    async def test_drops_citations_without_arxiv_id(self) -> None:
        cfg = _cfg()
        classified = _classified(search_hint="x")
        cit_with = _citation("2401.1")
        cit_without = Citation(
            url="https://example.com/blog",
            title="no arxiv id",
            source_type="html",
            confidence_score=0.5,
        )

        async def _search(**kwargs: Any) -> ToolResult:
            return ToolResult(content="x", citations=[cit_with, cit_without])

        reg = _registry({"arxiv_search": _search})
        seeds_citations: list[Citation] = []
        out = await _gather_seeds(classified, "ignored", reg, cfg, seeds_citations)
        assert len(out) == 1
        assert out[0].arxiv_id == "2401.1"
        # seeds_citations only keeps the arxiv one
        assert len(seeds_citations) == 1
        assert seeds_citations[0].arxiv_id == "2401.1"


# ---------------------------------------------------------------------------
# download_pdf_once / extract_text / fetch_paper_text_fallback
# ---------------------------------------------------------------------------


class TestDownloadAndExtract:
    @pytest.mark.asyncio
    async def test_download_returns_path(self, tmp_path) -> None:
        pdf_path = tmp_path / "x.pdf"
        pdf_path.write_bytes(b"%PDF")

        async def _download(**_: Any) -> ToolResult:
            return ToolResult(content=str(pdf_path))

        reg = _registry({"arxiv_download_pdf": _download})
        out = await download_pdf_once("2401.1", reg)
        assert out == str(pdf_path)

    @pytest.mark.asyncio
    async def test_download_error_returns_none(self) -> None:
        async def _download(**_: Any) -> ToolResult:
            return ToolResult(content="", error="HTTP 503")

        reg = _registry({"arxiv_download_pdf": _download})
        out = await download_pdf_once("2401.1", reg)
        assert out is None

    @pytest.mark.asyncio
    async def test_download_non_path_returns_none(self) -> None:
        """If arxiv_download_pdf returns non-path content, we return None so
        the caller falls back to arxiv_resolve instead of crashing."""

        async def _download(**_: Any) -> ToolResult:
            return ToolResult(content="error: not a path")

        reg = _registry({"arxiv_download_pdf": _download})
        out = await download_pdf_once("2401.1", reg)
        assert out is None

    @pytest.mark.asyncio
    async def test_missing_download_tool_returns_none(self) -> None:
        reg = _registry({})
        out = await download_pdf_once("2401.1", reg)
        assert out is None

    @pytest.mark.asyncio
    async def test_extract_text_happy_path(self, tmp_path) -> None:
        pdf_path = tmp_path / "x.pdf"
        pdf_path.write_bytes(b"%PDF")

        async def _extract(**_: Any) -> ToolResult:
            return ToolResult(content="extracted body")

        reg = _registry({"pdf_extract_text": _extract})
        out = await extract_text(str(pdf_path), reg)
        assert out == "extracted body"

    @pytest.mark.asyncio
    async def test_extract_text_missing_tool_returns_empty(self) -> None:
        reg = _registry({})
        out = await extract_text("/tmp/x.pdf", reg)
        assert out == ""

    @pytest.mark.asyncio
    async def test_resolve_fallback_happy_path(self) -> None:
        async def _resolve(**_: Any) -> ToolResult:
            return ToolResult(content="metadata-only content")

        reg = _registry({"arxiv_resolve": _resolve})
        out, _pdf_path = await fetch_paper_text_fallback("2401.1", reg)
        assert out == "metadata-only content"

    @pytest.mark.asyncio
    async def test_resolve_fallback_missing_tool_returns_empty(self) -> None:
        reg = _registry({})
        out, _pdf_path = await fetch_paper_text_fallback("2401.1", reg)
        assert out == ""


# ---------------------------------------------------------------------------
# render_pages
# ---------------------------------------------------------------------------


class TestRenderPages:
    @pytest.mark.asyncio
    async def test_missing_render_tool_returns_empty(self) -> None:
        reg = _registry({})
        out = await render_pages("/tmp/x.pdf", reg)
        assert out == []

    @pytest.mark.asyncio
    async def test_render_error_returns_empty(self) -> None:
        async def _render(**_: Any) -> ToolResult:
            return ToolResult(content="", error="poppler missing")

        reg = _registry({"pdf_render_pages": _render})
        out = await render_pages("/tmp/x.pdf", reg)
        assert out == []

    @pytest.mark.asyncio
    async def test_render_returns_non_json_returns_empty(self) -> None:
        async def _render(**_: Any) -> ToolResult:
            return ToolResult(content="not json")

        reg = _registry({"pdf_render_pages": _render})
        out = await render_pages("/tmp/x.pdf", reg)
        assert out == []

    @pytest.mark.asyncio
    async def test_render_returns_pages_list_of_data_urls(self, tmp_path) -> None:
        pdf_path = tmp_path / "x.pdf"
        pdf_path.write_bytes(b"%PDF")

        async def _render(**_: Any) -> ToolResult:
            payload = {
                "pages": ["data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"],
                "count": 2,
            }
            return ToolResult(content=json.dumps(payload))

        reg = _registry({"pdf_render_pages": _render})
        out = await render_pages(str(pdf_path), reg, max_pages=10)
        assert out == ["data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"]

    @pytest.mark.asyncio
    async def test_render_filters_non_data_url_strings(self, tmp_path) -> None:
        pdf_path = tmp_path / "x.pdf"
        pdf_path.write_bytes(b"%PDF")

        async def _render(**_: Any) -> ToolResult:
            payload = {"pages": ["data:image/jpeg;base64,OK", "not a data url", ""], "count": 3}
            return ToolResult(content=json.dumps(payload))

        reg = _registry({"pdf_render_pages": _render})
        out = await render_pages(str(pdf_path), reg)
        assert out == ["data:image/jpeg;base64,OK"]


# ---------------------------------------------------------------------------
# _synthesize_markdown
# ---------------------------------------------------------------------------


class TestSynthesizeMarkdown:
    @pytest.mark.asyncio
    async def test_empty_analyses_returns_boilerplate(self) -> None:
        client = _raising_client(RuntimeError("should not be called"))
        out = await _synthesize_markdown("query", {}, client, "m")
        assert "No arxiv papers" in out
        assert "POPPLER" in out

    @pytest.mark.asyncio
    async def test_llm_happy_path_returns_synthesized_markdown(self) -> None:
        client = _FakeAsyncOpenAI("# Synthesized\n\nReport body.")
        analyses = {"2401.1": _analysis("2401.1")}
        out = await _synthesize_markdown("query", analyses, client, "m")
        assert out == "# Synthesized\n\nReport body."

    @pytest.mark.asyncio
    async def test_llm_failure_returns_fallback_synthesis(self) -> None:
        client = _raising_client(RuntimeError("LLM down"))
        analyses = {
            "2401.1": _analysis("2401.1", title="First Paper"),
            "2401.2": _analysis("2401.2", title="Second Paper"),
        }
        out = await _synthesize_markdown("query", analyses, client, "m")
        # Fallback synthesis contains the title query + paper count + each paper
        assert "Academic Research Report" in out
        assert "query" in out
        assert "Papers analyzed" in out
        assert "First Paper" in out
        assert "Second Paper" in out
        # arxiv links rendered
        assert "https://arxiv.org/abs/2401.1" in out

    @pytest.mark.asyncio
    async def test_llm_empty_response_falls_back(self) -> None:
        # When the LLM returns empty content, we resort to fallback synthesis
        client = _FakeAsyncOpenAI("")
        analyses = {"2401.1": _analysis("2401.1")}
        out = await _synthesize_markdown("query", analyses, client, "m")
        assert "Academic Research Report" in out

    @pytest.mark.asyncio
    async def test_empty_analyses_with_blogs_synthesizes_report(self) -> None:
        # Academic mode is not limited to arxiv: when no arxiv papers are
        # analyzed but blog/web content was found, a report is still generated
        # from the blogs.
        blog_cit = Citation(
            url="https://blog.example/post",
            title="Blog Post",
            snippet="snippet",
            source_type="blog",
            confidence_score=0.7,
        )

        async def _fetch_page(**kw: Any) -> ToolResult:
            return ToolResult(content="article body text", citations=[])

        reg = _registry({"fetch_page": _fetch_page})
        client = _FakeAsyncOpenAI("# Blog Synthesis\n\nReport body.")
        out = await _synthesize_markdown(
            "query", {}, client, "m", blog_citations=[blog_cit], tools=reg
        )
        assert out == "# Blog Synthesis\n\nReport body."

    @pytest.mark.asyncio
    async def test_empty_analyses_with_blogs_llm_failure_returns_fallback(self) -> None:
        blog_cit = Citation(
            url="https://blog.example/post",
            title="Blog Post",
            snippet="snippet",
            source_type="blog",
            confidence_score=0.7,
        )
        client = _raising_client(RuntimeError("LLM down"))
        out = await _synthesize_markdown(
            "query", {}, client, "m", blog_citations=[blog_cit]
        )
        assert "Web/blog sources found" in out
        assert "Blog Post" in out
        assert "No peer-reviewed arxiv" in out

    @pytest.mark.asyncio
    async def test_empty_analyses_with_blogs_no_fetch_tool_uses_snippets(self) -> None:
        blog_cit = Citation(
            url="https://blog.example/post",
            title="Blog Post",
            snippet="snippet",
            source_type="blog",
            confidence_score=0.7,
        )
        # No fetch_page registered: the fallback still runs off snippets.
        client = _FakeAsyncOpenAI("# Blog Synthesis\n")
        out = await _synthesize_markdown(
            "query", {}, client, "m", blog_citations=[blog_cit]
        )
        assert out == "# Blog Synthesis"

    @pytest.mark.asyncio
    async def test_no_extractable_text_with_blogs_falls_back(self) -> None:
        # All arxiv analyses lack extractable text (None), but blogs exist:
        # synthesize from the blogs rather than emit the no-text boilerplate.
        blog_cit = Citation(
            url="https://blog.example/post",
            title="Blog Post",
            snippet="snippet",
            source_type="blog",
            confidence_score=0.7,
        )
        client = _FakeAsyncOpenAI("# Blog Synthesis\n")
        out = await _synthesize_markdown(
            "query", {"2401.1": None}, client, "m", blog_citations=[blog_cit]
        )
        assert out == "# Blog Synthesis"


class TestFallbackSynthesis:
    def test_renders_each_paper_section(self) -> None:
        analyses = {
            "2401.1": _analysis("2401.1", title="Paper One"),
            "2401.2": _analysis("2401.2", title="Paper Two"),
        }
        out = _fallback_synthesis("my query", analyses)
        assert "my query" in out
        assert "Papers analyzed" in out
        assert "Paper One" in out
        assert "Paper Two" in out
        assert "arxiv.org/abs/2401.1" in out
        assert "arxiv.org/abs/2401.2" in out
        # Key findings render
        assert "finding for 2401.1" in out
        # Methodology
        assert "Mock methodology" in out
        # Limitations
        assert "Mock limitation" in out

    def test_renders_each_blog_source(self) -> None:
        blog_cits = [
            Citation(
                url="https://blog.example/a",
                title="Post A",
                snippet="snippet A",
                source_type="blog",
                confidence_score=0.7,
            ),
            Citation(
                url="https://blog.example/b",
                title="Post B",
                snippet="snippet B",
                source_type="blog",
                confidence_score=0.6,
            ),
        ]
        out = _fallback_blog_synthesis("my query", blog_cits)
        assert "my query" in out
        assert "Web/blog sources found" in out
        assert "No peer-reviewed arxiv" in out
        assert "Post A" in out
        assert "Post B" in out
        assert "snippet A" in out
        assert "snippet B" in out
        assert "https://blog.example/a" in out


# ---------------------------------------------------------------------------
# academic_research end-to-end (integration via mocked analyze node + tools)
# ---------------------------------------------------------------------------


def _patch_analyze(monkeypatch, analyses_by_id: dict[str, PaperAnalysis]) -> dict[str, int]:
    """Replace the academic path's analyze_paper_node with a function that
    returns the analyses_by_id entry for each arxiv_id (or a fresh _analysis
    if missing). Returns a counter dict so tests can assert call counts.
    """
    calls: dict[str, int] = {"n": 0}

    async def _fake_analyze(
        arxiv_id: str,
        paper_text: str,
        query: str,
        client: Any,
        model: str,
        page_image_data_urls: list[str] | None = None,
        text_source: str = "pdf",
        max_context_tokens: int = 131072,
        **kwargs: Any,
    ) -> PaperAnalysis:
        calls["n"] += 1
        if arxiv_id in analyses_by_id:
            return analyses_by_id[arxiv_id]
        return _analysis(arxiv_id)

    monkeypatch.setattr(academic, "analyze_paper_node", _fake_analyze)
    return calls


def _tools_for(
    monkeypatch,
    papers: list[str],
) -> ToolRegistry:
    """Build a tool registry where arxiv_search returns `papers` as citations,
    arxiv_download_pdf returns a dummy path, pdf_extract_text returns text, and
    pdf_render_pages returns two synthetic data URLs.
    """
    citations = [_citation(aid, title=f"Paper {aid}") for aid in papers]

    async def _arxiv_search(**kwargs: Any) -> ToolResult:
        return ToolResult(content="searched", citations=citations)

    async def _arxiv_resolve(**kwargs: Any) -> ToolResult:
        return ToolResult(content="resolved")

    async def _arxiv_download(**kwargs: Any) -> ToolResult:
        return ToolResult(content="/tmp/fake.pdf")

    async def _pdf_extract(**kwargs: Any) -> ToolResult:
        return ToolResult(content="paper body text")

    async def _pdf_render(**kwargs: Any) -> ToolResult:
        return ToolResult(
            content=json.dumps({"pages": ["data:image/jpeg;base64,AAAA"], "count": 1})
        )

    return _registry(
        {
            "arxiv_search": _arxiv_search,
            "arxiv_resolve": _arxiv_resolve,
            "arxiv_download_pdf": _arxiv_download,
            "pdf_extract_text": _pdf_extract,
            "pdf_render_pages": _pdf_render,
        }
    )


class TestAcademicResearchE2E:
    @pytest.mark.asyncio
    async def test_no_seeds_returns_boilerplate_report(self, monkeypatch) -> None:
        cfg = _cfg()
        classified = _classified(search_hint="nothing")

        async def _empty_search(**_: Any) -> ToolResult:
            return ToolResult(content="", citations=[])

        reg = _registry({"arxiv_search": _empty_search})
        client = _FakeAsyncOpenAI("# Synthesized\n")
        _patch_analyze(monkeypatch, {})

        report = await academic_research(classified, "nothing", _fake_router(client), reg, cfg)
        assert report.path == "academic"
        assert "No arxiv papers" in report.markdown
        assert report.citation_graph is not None
        assert len(report.citation_graph.nodes) == 0
        assert report.iterations == 0

    @pytest.mark.asyncio
    async def test_no_arxiv_seeds_but_blogs_generate_report(self, monkeypatch) -> None:
        """Academic mode is not limited to arxiv: when arxiv returns no seeds
        but blog/web content was found, the report is synthesized from blogs."""
        cfg = _cfg()
        classified = _classified(search_hint="nothing")

        async def _empty_search(**_: Any) -> ToolResult:
            return ToolResult(content="", citations=[])

        blog_cit = Citation(
            url="https://blog.example/post",
            title="Blog Post",
            snippet="snippet",
            source_type="blog",
            confidence_score=0.7,
        )

        async def _blog_search(**kw: Any) -> ToolResult:
            return ToolResult(content="", citations=[blog_cit])

        async def _fetch_page(**kw: Any) -> ToolResult:
            return ToolResult(content="article body", citations=[])

        reg = _registry(
            {
                "arxiv_search": _empty_search,
                "blog_search": _blog_search,
                "fetch_page": _fetch_page,
            }
        )
        client = _FakeAsyncOpenAI("# Blog Synthesis\n\nReport body.")
        _patch_analyze(monkeypatch, {})

        report = await academic_research(classified, "nothing", _fake_router(client), reg, cfg)
        assert report.path == "academic"
        assert report.markdown == "# Blog Synthesis\n\nReport body."
        # Blog citation flows into the bibliography even though no arxiv papers
        assert any(c.url == "https://blog.example/post" for c in report.citations)
        assert report.citation_graph is not None
        assert len(report.citation_graph.nodes) == 0
        assert report.iterations == 0

    @pytest.mark.asyncio
    async def test_flat_no_recursion_max_depth_zero(self, monkeypatch) -> None:
        """max_depth=0 means no children are enqueued, only seed papers analyzed."""
        cfg = _cfg(max_depth=0, max_papers=10, seed_count=3, concurrency=2)
        classified = _classified(search_hint="rlhf")

        # Each analysis advertises children, but max_depth=0 must drop them
        analyses = {
            "2401.1": _analysis("2401.1", key_refs=["2401.10", "2401.11"]),
            "2401.2": _analysis("2401.2", key_refs=["2401.20"]),
            "2401.3": _analysis("2401.3"),
        }
        calls = _patch_analyze(monkeypatch, analyses)
        reg = _tools_for(monkeypatch, list(analyses.keys()))
        # The synthesis client isn't reachable from the analyze path (we patched it out),
        # but the final synthesis call still goes through the real client.
        client = _FakeAsyncOpenAI("# Synthesis OK\n")
        report = await academic_research(classified, "rlhf", _fake_router(client), reg, cfg)
        assert report.path == "academic"
        # All 3 seeds analyzed, no children enqueued
        assert calls["n"] == 3
        assert report.iterations == 3
        assert len(report.citation_graph.nodes) == 3
        # No edges because no recursion
        for _parent, children in report.citation_graph.edges.items():
            assert children == []

    @pytest.mark.asyncio
    async def test_recursion_max_one_level(self, monkeypatch) -> None:
        """max_depth=1 means seed papers are analyzed, their key refs enqueued,
        and analyzed — but the depth-1 refs do NOT recurse further."""
        cfg = _cfg(max_depth=1, max_papers=10, seed_count=2, concurrency=2)
        classified = _classified(search_hint="rlhf")

        # Seeds: 2401.1 references 2401.10 (child of seed) which references 2401.100 (grandchild)
        # max_depth=1 must analyze 2401.10 but NOT 2401.100
        analyses = {
            "2401.1": _analysis("2401.1", key_refs=["2401.10", "2401.11"]),
            "2401.2": _analysis("2401.2", key_refs=["2401.20"]),
            "2401.10": _analysis("2401.10", key_refs=["2401.100"]),
            "2401.11": _analysis("2401.11"),
            "2401.20": _analysis("2401.20"),
        }
        calls = _patch_analyze(monkeypatch, analyses)
        reg = _tools_for(monkeypatch, ["2401.1", "2401.2"])
        client = _FakeAsyncOpenAI("# Synthesis OK\n")
        report = await academic_research(classified, "rlhf", _fake_router(client), reg, cfg)
        # 5 analyses: 2 seeds + 3 depth-1 children (2401.10, 2401.11, 2401.20).
        # 2401.100 is a depth-2 grandchild and must NOT be analyzed.
        assert calls["n"] == 5
        assert report.iterations == 5
        assert len(report.citation_graph.nodes) >= 5
        # The grandchild must not be in the graph
        assert "2401.100" not in report.citation_graph.nodes
        # Edges: 2401.1 -> 2401.10 and 2401.11; 2401.2 -> 2401.20
        assert set(report.citation_graph.edges.get("2401.1", [])) == {"2401.10", "2401.11"}
        assert set(report.citation_graph.edges.get("2401.2", [])) == {"2401.20"}

    @pytest.mark.asyncio
    async def test_max_papers_cap_enforced(self, monkeypatch) -> None:
        """Even if recursion would enqueue more papers, max_papers is a hard cap."""
        cfg = _cfg(max_depth=2, max_papers=3, seed_count=5, concurrency=1)
        classified = _classified(search_hint="rlhf")

        # 5 seeds, all referencing more papers — but max_papers=3 must stop us
        seed_ids = ["A.1", "A.2", "A.3", "A.4", "A.5"]
        analyses = {aid: _analysis(aid, key_refs=[f"{aid}.child"]) for aid in seed_ids}
        # Note: arxiv ids must match the regex in the search results; we use
        # a fixed fake registry so the regex isn't checked here.
        calls = _patch_analyze(monkeypatch, analyses)
        reg = _tools_for(monkeypatch, seed_ids)
        client = _FakeAsyncOpenAI("# Synthesis OK\n")
        report = await academic_research(classified, "rlhf", _fake_router(client), reg, cfg)
        # We stop analyzing once processed_count == max_papers=3
        assert calls["n"] <= 3
        assert report.iterations <= 3
        # The graph may have more nodes (enqueued children) but analyzed
        # nodes must be <= max_papers
        analyzed_count = sum(
            1 for n in report.citation_graph.nodes.values() if n.arxiv_id in analyses
        )
        assert analyzed_count <= 3

    @pytest.mark.asyncio
    async def test_pdf_vision_disabled_skips_render(self, monkeypatch) -> None:
        """When pdf_vision.enabled=False, _render_paper_pages is not called.
        We assert by removing pdf_render_pages from the registry version of
        the vision-gate check; the path short-circuits via the
        `"pdf_render_pages" not in tools.names()` branch."""
        cfg = _cfg(pdf_vision_enabled=False, max_depth=0, max_papers=5, concurrency=1)
        classified = _classified(search_hint="x")

        render_calls: list[Any] = []

        async def _arxiv_search(**_: Any) -> ToolResult:
            return ToolResult(content="", citations=[_citation("2401.1")])

        async def _arxiv_download(**_: Any) -> ToolResult:
            return ToolResult(content="/tmp/p.pdf")

        async def _pdf_extract(**_: Any) -> ToolResult:
            return ToolResult(content="body")

        async def _pdf_render(**_: Any) -> ToolResult:
            render_calls.append(True)
            return ToolResult(content="{}")

        reg = _registry(
            {
                "arxiv_search": _arxiv_search,
                "arxiv_download_pdf": _arxiv_download,
                "pdf_extract_text": _pdf_extract,
                "pdf_render_pages": _pdf_render,  # present but must be skipped
            }
        )
        _patch_analyze(monkeypatch, {"2401.1": _analysis("2401.1")})
        client = _FakeAsyncOpenAI("# x\n")
        await academic_research(classified, "x", _fake_router(client), reg, cfg)
        # pdf_render_pages was NOT called because pdf_vision.enabled=False
        # gated the path-internal `if config.pdf_vision.enabled and ...` check.
        assert render_calls == []

    @pytest.mark.asyncio
    async def test_classifier_search_hint_preferred_over_query(self, monkeypatch) -> None:
        """When ClassifiedQuery.search_hint is set, _gather_seeds uses it for
        arxiv_search rather than the original_query."""
        cfg = _cfg(seed_count=2, max_depth=0, concurrency=1)
        classified = _classified(search_hint="explicit hint")
        captured: dict[str, Any] = {}

        async def _search(**kwargs: Any) -> ToolResult:
            captured.update(kwargs)
            return ToolResult(content="", citations=[_citation("2401.1")])

        reg = _registry(
            {
                "arxiv_search": _search,
                "arxiv_download_pdf": _noop_tool,
                "pdf_extract_text": _noop_tool,
            }
        )
        _patch_analyze(monkeypatch, {"2401.1": _analysis("2401.1")})
        client = _FakeAsyncOpenAI("# x\n")
        await academic_research(classified, "ignored original", _fake_router(client), reg, cfg)
        assert captured["query"] == "explicit hint"
        assert captured["query"] != "ignored original"

    @pytest.mark.asyncio
    async def test_dedup_via_version_stripped_arxiv_id(self, monkeypatch) -> None:
        """The same paper appearing under versioned and unversioned ids must
        be processed only once (by version-stripped base id)."""
        cfg = _cfg(max_depth=1, max_papers=10, seed_count=2, concurrency=1)
        classified = _classified(search_hint="x")
        # Seed with versioned id; its ref points to the same base id unversioned
        analyses = {
            "2401.10v2": _analysis("2401.10v2", key_refs=["2401.10"]),  # same base
            "2401.20": _analysis("2401.20"),
        }
        # _strip_version("2401.10v2") == "2401.10" so child should dedup
        calls = _patch_analyze(monkeypatch, analyses)
        reg = _tools_for(monkeypatch, ["2401.10v2", "2401.20"])
        client = _FakeAsyncOpenAI("# Synthesis OK\n")
        await academic_research(classified, "x", _fake_router(client), reg, cfg)
        # The versioned seed 2401.10v2 gets analyzed AND its key ref 2401.10
        # would normally be enqueued, but stripped both == 2401.10 -> dedup
        # So we analyze exactly 2 papers (2401.10v2 and 2401.20).
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_seed_relevance_gate_drops_off_topic_seeds(self, monkeypatch) -> None:
        """An off-topic seed is dropped by the batch pre-gate before any PDF
        download / analysis, so it never consumes a max_papers slot."""
        cfg = _cfg(max_depth=0, max_papers=10, seed_count=5, concurrency=1)
        cfg.academic.seed_relevance_gate = True
        cfg.academic.seed_relevance_threshold = 0.7
        classified = _classified(search_hint="rlhf")
        # 3 seeds; the batch gate keeps 2 and drops 1 (2401.3).
        seeds = ["2401.1", "2401.2", "2401.3"]
        analyses = {aid: _analysis(aid) for aid in seeds}
        calls = _patch_analyze(monkeypatch, analyses)
        reg = _tools_for(monkeypatch, seeds)

        gate_client = _FakeAsyncOpenAI('{"scores": {"2401.1": 0.9, "2401.2": 0.8, "2401.3": 0.1}}')
        # The same fake router serves both the gate (ANALYSIS role) and the
        # synthesis writer. The router.resolve returns one client — the gate
        # uses it too. _patch_analyze has already swapped the analyze node.
        router = _fake_router(gate_client)
        report = await academic_research(classified, "rlhf", router, reg, cfg)
        # Only the 2 on-topic seeds were analyzed.
        assert calls["n"] == 2
        assert "2401.3" not in report.citation_graph.nodes
        urls = [c.url for c in report.citations]
        assert "https://arxiv.org/abs/2401.3" not in urls

    @pytest.mark.asyncio
    async def test_seed_gate_drops_scholar_only_seed_and_its_citation(self, monkeypatch) -> None:
        """An off-topic scholar-only seed (synthetic `scholar:` id, citation with
        no arxiv_id) must be dropped AND its citation removed from the report."""
        cfg = _cfg(max_depth=0, max_papers=10, seed_count=5, concurrency=1)
        cfg.academic.seed_relevance_gate = True
        cfg.academic.seed_relevance_threshold = 0.7
        cfg.academic.seed_backends = ["arxiv", "scholar"]
        cfg.scholar.enabled = True
        classified = _classified(search_hint="rlhf")
        analyses = {"2401.1": _analysis("2401.1")}
        calls = _patch_analyze(monkeypatch, analyses)

        arxiv_cit = _citation("2401.1", title="On Topic")
        scholar_cit = Citation(
            url="https://nature.com/articles/offtopic",
            title="Off Topic Scholar",
            snippet="abstract",
            source_type="scholar",
            confidence_score=0.7,
        )

        async def _arxiv_search(**kwargs: Any) -> ToolResult:
            return ToolResult(content="arxiv", citations=[arxiv_cit])

        async def _scholar_search(**kwargs: Any) -> ToolResult:
            return ToolResult(content="scholar", citations=[scholar_cit])

        async def _arxiv_download(**kwargs: Any) -> ToolResult:
            return ToolResult(content="/tmp/fake.pdf")

        async def _pdf_extract(**kwargs: Any) -> ToolResult:
            return ToolResult(content="paper body text")

        reg = _registry(
            {
                "arxiv_search": _arxiv_search,
                "arxiv_download_pdf": _arxiv_download,
                "pdf_extract_text": _pdf_extract,
                "scholar_search": _scholar_search,
            }
        )
        # Gate keeps the arxiv seed, drops the scholar-only seed. The synthetic
        # id is "scholar:" + sha256(url)[:12] (see _gather_seeds).
        import hashlib

        scholar_id = "scholar:" + hashlib.sha256(scholar_cit.url.encode()).hexdigest()[:12]
        gate_client = _FakeAsyncOpenAI(json.dumps({"scores": {"2401.1": 0.9, scholar_id: 0.1}}))
        router = _fake_router(gate_client)
        report = await academic_research(classified, "rlhf", router, reg, cfg)
        assert calls["n"] == 1
        urls = [c.url for c in report.citations]
        assert "https://arxiv.org/abs/2401.1" in urls
        assert "https://nature.com/articles/offtopic" not in urls

    @pytest.mark.asyncio
    async def test_seed_relevance_gate_disabled_keeps_all_seeds(self, monkeypatch) -> None:
        """When seed_relevance_gate=false, the gate client is never called and
        all seeds are analyzed."""
        cfg = _cfg(max_depth=0, max_papers=10, seed_count=3, concurrency=1)
        cfg.academic.seed_relevance_gate = False
        classified = _classified(search_hint="rlhf")
        seeds = ["2401.1", "2401.2", "2401.3"]
        analyses = {aid: _analysis(aid) for aid in seeds}
        calls = _patch_analyze(monkeypatch, analyses)
        reg = _tools_for(monkeypatch, seeds)
        gate_client = MagicMock()
        gate_client.chat.completions.create = AsyncMock()
        # Two separate clients: gate + writer. Use a router that resolves a
        # fresh client per resolve call.
        router = MagicMock()
        router.resolve.return_value = MagicMock(
            client=gate_client, model="m", max_context_tokens=131072
        )
        with patch.object(
            academic, "_synthesize_markdown", new=AsyncMock(return_value="# Synth\n")
        ):
            await academic_research(classified, "rlhf", router, reg, cfg)
        gate_client.chat.completions.create.assert_not_awaited()
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_citations_deduped_by_url_keeps_highest_confidence(self, monkeypatch) -> None:
        cfg = _cfg(max_depth=0, max_papers=5, concurrency=1, seed_count=2)
        classified = _classified(search_hint="x")
        _patch_analyze(
            monkeypatch,
            {"2401.1": _analysis("2401.1"), "2401.2": _analysis("2401.2")},
        )
        reg = _tools_for(monkeypatch, ["2401.1", "2401.2"])
        client = _FakeAsyncOpenAI("# x\n")
        report = await academic_research(classified, "x", _fake_router(client), reg, cfg)
        urls = [c.url for c in report.citations]
        # Each arxiv URL should appear exactly once (dedup by url)
        assert len(urls) == len(set(urls))
        # Both URLs present
        assert "https://arxiv.org/abs/2401.1" in urls
        assert "https://arxiv.org/abs/2401.2" in urls

    @pytest.mark.asyncio
    async def test_same_paper_seeded_twice_analyzed_once(self, monkeypatch) -> None:
        """Two seeds resolving to the same version-stripped arxiv id must be
        analyzed exactly once even when dispatched concurrently. The claim
        under the lock re-checks membership, closing the pre-check TOCTOU."""
        cfg = _cfg(max_depth=0, max_papers=10, seed_count=2, concurrency=2)
        classified = _classified(search_hint="x")
        # arxiv_search returns both the versioned and unversioned forms of the
        # same paper; _gather_seeds keeps both (dedup there is by raw id).
        analyses = {"2401.1": _analysis("2401.1"), "2401.1v2": _analysis("2401.1v2")}
        calls = _patch_analyze(monkeypatch, analyses)
        reg = _tools_for(monkeypatch, ["2401.1", "2401.1v2"])
        client = _FakeAsyncOpenAI("# Synthesis OK\n")
        report = await academic_research(classified, "x", _fake_router(client), reg, cfg)
        assert calls["n"] == 1, "same paper analyzed more than once (TOCTOU)"
        assert report.iterations == 1

    @pytest.mark.asyncio
    async def test_archives_blog_posts_when_writer_configured(self, monkeypatch) -> None:
        """Academic-mode blog citations are fetched + archived as artifacts."""
        from deep_research.library.writer import LibraryWriter

        cfg = _cfg(max_depth=0, max_papers=1, seed_count=1, concurrency=1)
        classified = _classified(search_hint="rlhf")

        blog_cit = Citation(
            url="https://blog.example/post",
            title="Blog Post",
            snippet="snippet",
            source_type="blog",
            confidence_score=0.7,
        )

        async def _blog_search(**kw: Any) -> ToolResult:
            return ToolResult(content="", citations=[blog_cit])

        async def _fetch_page(**kw: Any) -> ToolResult:
            return ToolResult(content="article body", citations=[])

        async def _arxiv_search(**kw: Any) -> ToolResult:
            return ToolResult(content="", citations=[_citation("2401.1", "Paper 2401.1")])

        async def _arxiv_download(**kw: Any) -> ToolResult:
            return ToolResult(content="/tmp/fake.pdf")

        async def _pdf_extract(**kw: Any) -> ToolResult:
            return ToolResult(content="paper body text")

        reg = _registry(
            {
                "arxiv_search": _arxiv_search,
                "arxiv_download_pdf": _arxiv_download,
                "pdf_extract_text": _pdf_extract,
                "blog_search": _blog_search,
                "fetch_page": _fetch_page,
            }
        )
        calls = _patch_analyze(monkeypatch, {"2401.1": _analysis("2401.1")})
        client = _FakeAsyncOpenAI("# Synthesis OK\n")

        writer = LibraryWriter(MagicMock(), "/tmp/dr_test_academic_blog")
        with patch(
            "deep_research.paths.academic.archive_html_source", new=AsyncMock()
        ) as archive:
            await academic_research(
                classified, "rlhf", _fake_router(client), reg, cfg,
                writer=writer, run_id="run1",
            )
        archive.assert_awaited()
        url, html = archive.await_args.args[:2]
        assert url == "https://blog.example/post"
        assert html == "article body"
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_skips_blog_archiving_when_fetched_html_flag_off(self, monkeypatch) -> None:
        """`pdl.archive_fetched_html=False` opts out of academic blog archiving."""
        from deep_research.library.writer import LibraryWriter

        cfg = _cfg(max_depth=0, max_papers=1, seed_count=1, concurrency=1)
        cfg.pdl.archive_fetched_html = False
        classified = _classified(search_hint="rlhf")

        blog_cit = Citation(
            url="https://blog.example/post",
            title="Blog Post",
            snippet="snippet",
            source_type="blog",
            confidence_score=0.7,
        )

        async def _blog_search(**kw: Any) -> ToolResult:
            return ToolResult(content="", citations=[blog_cit])

        async def _arxiv_search(**kw: Any) -> ToolResult:
            return ToolResult(content="", citations=[_citation("2401.1", "Paper 2401.1")])

        async def _arxiv_download(**kw: Any) -> ToolResult:
            return ToolResult(content="/tmp/fake.pdf")

        async def _pdf_extract(**kw: Any) -> ToolResult:
            return ToolResult(content="paper body text")

        reg = _registry(
            {
                "arxiv_search": _arxiv_search,
                "arxiv_download_pdf": _arxiv_download,
                "pdf_extract_text": _pdf_extract,
                "blog_search": _blog_search,
            }
        )
        _patch_analyze(monkeypatch, {"2401.1": _analysis("2401.1")})
        client = _FakeAsyncOpenAI("# Synthesis OK\n")

        writer = LibraryWriter(MagicMock(), "/tmp/dr_test_academic_blog")
        with patch(
            "deep_research.paths.academic.archive_html_source", new=AsyncMock()
        ) as archive:
            await academic_research(
                classified, "rlhf", _fake_router(client), reg, cfg,
                writer=writer, run_id="run1",
            )
        archive.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


async def _noop_tool(**_: Any) -> ToolResult:
    """A tool that returns nothing. Used as a placeholder so the
    registry-level `not in tools.names()` check fails (we want the tool
    present but returning empty)."""
    return ToolResult(content="", citations=[])


__all__ = [
    "TestAcademicResearchE2E",
    "TestDownloadAndExtract",
    "TestFallbackSynthesis",
    "TestGatherSeeds",
    "TestRenderPages",
    "TestSynthesizeMarkdown",
]
