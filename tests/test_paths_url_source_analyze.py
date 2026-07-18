"""Dedicated unit tests for `paths.url_source` ANALYZE mode (P2.5).

Covers the analyze path with mocked AsyncOpenAI + ToolRegistry so tests run
fully offline:
  - `analyze_source.analyze()`: JSON parse, invalid JSON, vision blocks, LLM exception
  - `_render_analysis_markdown`: each section / empty sections
  - `url_source()`: arxiv / pdf / html fetch dispatch, unsupported URL type,
    fetch failure, follow-up gating (no follow-up vs explicit follow-up handoff)

Existing `test_paths_url_source.py` only covers the follow-up heuristic; this
file complements it with full-path coverage.
"""

from __future__ import annotations

import json
import sys as _sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.nodes.analyze_source import analyze as analyze_source_node
from deep_research.paths.url_source import (
    _fetch_arxiv_source,
    _fetch_html_source,
    _fetch_pdf_source,
    _render_analysis_markdown,
    url_source,
)
from deep_research.tools.pdf_utils import parse_pdf_path as _parse_pdf_path
from deep_research.state import Citation, SourceAnalysis, ToolName

_us_module = _sys.modules["deep_research.paths.url_source"]


# ---------------------------------------------------------------------------
# AsyncOpenAI doubles (same shape as test_paths_quick.py)
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


def _cfg() -> AgentTopConfig:
    return AgentTopConfig()


def _cit(url: str, title: str = "", source_type: str = "html") -> Citation:
    return Citation(
        url=url,
        title=title,
        snippet="",
        source_type=source_type,  # type: ignore[arg-type]
        confidence_score=0.7,
        discovered_by=ToolName.fetch_page,
    )


def _registry(
    tools: dict[str, Any] | None = None,
) -> ToolRegistry:
    """Build a ToolRegistry from a {name: async_callable} dict."""
    reg = ToolRegistry()
    for name, fn in (tools or {}).items():
        reg.register(name, fn, {"type": "function", "name": name})
    return reg


def _analysis(**overrides: Any) -> SourceAnalysis:
    defaults: dict[str, Any] = {
        "title": "Sample Source",
        "summary": "A short summary of the source.",
        "key_claims": [
            {"claim": "Claim 1", "evidence": "Ev 1", "page_or_section": "Sec. 2"},
        ],
        "methodology": None,
        "limitations": None,
        "relevance_to_query": None,
        "follow_ups": [],
        "gaps": [],
    }
    defaults.update(overrides)
    return SourceAnalysis.model_validate(defaults)


# ---------------------------------------------------------------------------
# analyze_source.analyze()
# ---------------------------------------------------------------------------


class TestAnalyzeSourceNode:
    @pytest.mark.asyncio
    async def test_parses_valid_json(self) -> None:
        payload = {
            "title": "T",
            "summary": "S",
            "key_claims": [{"claim": "C", "evidence": "E", "page_or_section": "p.1"}],
            "methodology": None,
            "limitations": None,
            "relevance_to_query": None,
            "follow_ups": [],
            "gaps": [],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        result = await analyze_source_node("https://x", "html", "content", "q", client, "m")
        assert isinstance(result, SourceAnalysis)
        assert result.title == "T"
        assert result.summary == "S"
        assert len(result.key_claims) == 1
        assert result.key_claims[0]["claim"] == "C"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_unparseable_marker(self) -> None:
        client = _FakeAsyncOpenAI("not valid json {{{")
        result = await analyze_source_node("https://x", "html", "c", "q", client, "m")
        assert result.title.startswith("[unparseable]")
        assert "not valid json" in result.summary

    @pytest.mark.asyncio
    async def test_llm_exception_returns_error_marker(self) -> None:
        client = _raising_client(RuntimeError("LLM down"))
        result = await analyze_source_node("https://x", "html", "c", "q", client, "m")
        assert result.title.startswith("[error]")
        assert "LLM analysis failed" in result.summary
        assert "RuntimeError" in result.summary

    @pytest.mark.asyncio
    async def test_vision_blocks_present_in_messages_when_images_supplied(self) -> None:
        # The node builds multi-content user message when image data URLs are given.
        # We capture the messages by intercepting create().
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> Any:
            captured["messages"] = kwargs.get("messages")
            return _FakeResponse(json.dumps({"title": "t", "summary": "s"}))

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_capture)

        await analyze_source_node(
            "https://x",
            "pdf",
            "text content",
            "q",
            client,
            "m",
            page_image_data_urls=["data:image/jpeg;base64,AAAA"],
        )
        user_msg = captured["messages"][1]
        assert user_msg["role"] == "user"
        # Multi-content form: list of blocks
        assert isinstance(user_msg["content"], list)
        types = [b.get("type") for b in user_msg["content"]]
        assert "text" in types
        assert "image_url" in types

    @pytest.mark.asyncio
    async def test_text_only_form_when_no_images(self) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> Any:
            captured["messages"] = kwargs.get("messages")
            return _FakeResponse(json.dumps({"title": "t", "summary": "s"}))

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_capture)
        await analyze_source_node("https://x", "html", "text", "", client, "m")
        user_msg = captured["messages"][1]
        assert user_msg["role"] == "user"
        # Text-only form: content is a plain string
        assert isinstance(user_msg["content"], str)


# ---------------------------------------------------------------------------
# _parse_pdf_path
# ---------------------------------------------------------------------------


class TestParsePdfPath:
    def test_absolute_path_returned(self) -> None:
        assert _parse_pdf_path("/tmp/foo/bar.pdf") == "/tmp/foo/bar.pdf"

    def test_multiline_returns_first_line(self) -> None:
        assert _parse_pdf_path("/a/b.pdf\nsecond line") == "/a/b.pdf"

    def test_relative_path_returns_none(self) -> None:
        assert _parse_pdf_path("relative/foo.pdf") is None

    def test_empty_returns_none(self) -> None:
        assert _parse_pdf_path("") is None
        assert _parse_pdf_path("   ") is None


# ---------------------------------------------------------------------------
# _render_analysis_markdown
# ---------------------------------------------------------------------------


class TestRenderAnalysisMarkdown:
    def test_full_render_includes_all_sections(self) -> None:
        a = _analysis(
            methodology="We did X",
            limitations=["lim 1", "lim 2"],
            relevance_to_query="Relevant to the user's question.",
            follow_ups=[{"topic": "Topic A", "why": "Because"}],
            gaps=["gap 1"],
        )
        md = _render_analysis_markdown("https://src", "arxiv", a, "the query")
        assert "Source Analysis" in md
        assert "https://src" in md
        assert "`arxiv`" in md
        assert "Sample Source" in md
        assert "the query" in md
        assert "## Summary" in md
        assert "## Key Claims" in md
        assert "Claim 1" in md
        assert "Sec. 2" in md
        assert "## Methodology" in md
        assert "## Limitations" in md
        assert "lim 1" in md
        assert "## Relevance to Query" in md
        assert "## Identified Gaps" in md
        assert "gap 1" in md
        assert "## Follow-up Research Suggestions" in md
        assert "Topic A" in md

    def test_empty_optional_sections_omitted(self) -> None:
        a = _analysis()  # all optional fields empty/None
        md = _render_analysis_markdown("https://src", "html", a, None)
        assert "## Methodology" not in md
        assert "## Limitations" not in md
        assert "## Relevance to Query" not in md
        assert "## Identified Gaps" not in md
        assert "## Follow-up Research Suggestions" not in md
        assert "## Key Claims" in md  # claim list is non-empty here

    def test_missing_query_omits_query_context(self) -> None:
        a = _analysis()
        md = _render_analysis_markdown("https://src", "html", a, None)
        assert "Query context" not in md


# ---------------------------------------------------------------------------
# _fetch_arxiv_source / _fetch_pdf_source / _fetch_html_source
# ---------------------------------------------------------------------------


class TestFetchHelpers:
    @pytest.mark.asyncio
    async def test_arxiv_resolve_missing_returns_tool_not_registered_msg(self) -> None:
        reg = _registry()  # no arxiv tools
        text, title, cits, page_urls = await _fetch_arxiv_source("2401.12345", reg)
        assert "arxiv tool not registered" in text
        assert title == ""
        assert cits == []
        assert page_urls == []

    @pytest.mark.asyncio
    async def test_arxiv_resolve_returns_metadata_citation(self) -> None:
        async def _resolve(**kw: Any) -> ToolResult:
            return ToolResult(
                content="paper meta",
                citations=[
                    _cit(
                        "https://arxiv.org/abs/2401.12345",
                        title="My Paper",
                        source_type="arxiv",
                    )
                ],
            )

        async def _download(**kw: Any) -> ToolResult:
            return ToolResult(content="/tmp/x.pdf")

        async def _extract(**kw: Any) -> ToolResult:
            return ToolResult(content="full paper text body")

        reg = _registry(
            {
                "arxiv_resolve": _resolve,
                "arxiv_download_pdf": _download,
                "pdf_extract_text": _extract,
            }
        )
        # render_pages defaults to False, so no pdf_render_pages call
        text, title, cits, page_urls = await _fetch_arxiv_source("2401.12345", reg)
        assert text == "full paper text body"
        assert title == "My Paper"
        assert len(cits) == 1
        assert cits[0].title == "My Paper"
        assert page_urls == []

    @pytest.mark.asyncio
    async def test_arxiv_resolve_with_render_returns_page_urls(self) -> None:
        async def _resolve(**kw: Any) -> ToolResult:
            return ToolResult(
                content="",
                citations=[
                    _cit(
                        "https://arxiv.org/abs/2401.12345",
                        title="My Paper",
                        source_type="arxiv",
                    )
                ],
            )

        async def _download(**kw: Any) -> ToolResult:
            return ToolResult(content="/tmp/x.pdf")

        async def _extract(**kw: Any) -> ToolResult:
            return ToolResult(content="extracted text")

        async def _render(**kw: Any) -> ToolResult:
            import json
            return ToolResult(
                content=json.dumps({
                    "pages": ["data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"],
                    "count": 2,
                })
            )

        reg = _registry(
            {
                "arxiv_resolve": _resolve,
                "arxiv_download_pdf": _download,
                "pdf_extract_text": _extract,
                "pdf_render_pages": _render,
            }
        )
        text, _title, _cits, page_urls = await _fetch_arxiv_source(
            "2401.12345", reg, render_pages=True, max_pages=10
        )
        assert text == "extracted text"
        assert len(page_urls) == 2
        assert all(p.startswith("data:image/jpeg;base64,") for p in page_urls)

    @pytest.mark.asyncio
    async def test_arxiv_download_failure_falls_back_to_meta_content(self) -> None:
        async def _resolve(**kw: Any) -> ToolResult:
            return ToolResult(content="metadata only fallback", citations=[])

        async def _download(**kw: Any) -> ToolResult:
            return ToolResult(content="", error="404")

        reg = _registry({"arxiv_resolve": _resolve, "arxiv_download_pdf": _download})
        text, _title, cits, page_urls = await _fetch_arxiv_source("2401.12345", reg)
        # Falls back to meta_res.content (no extract happened)
        assert "metadata only fallback" in text
        assert cits == []
        assert page_urls == []

    @pytest.mark.asyncio
    async def test_html_fetch_uses_fetch_page(self) -> None:
        async def _fetch_page(**kw: Any) -> ToolResult:
            return ToolResult(
                content="extracted article body",
                citations=[_cit("https://blog.example/post")],
            )

        cfg = _cfg()
        cfg.fetch_page.min_content_chars_for_browser_fallback = 10**9  # disable browser fallback
        reg = _registry({"fetch_page": _fetch_page})
        text, cits = await _fetch_html_source("https://blog.example/post", reg, cfg)
        assert text == "extracted article body"
        assert len(cits) == 1

    @pytest.mark.asyncio
    async def test_html_fetch_no_fallback_logic_in_url_source(self) -> None:
        # P4: the browser fallback now lives inside `fetch_page` itself.
        # `_fetch_html_source` should just call fetch_page and return its output
        # verbatim — no extra fallback selection logic here anymore.
        async def _fetch_page(**kw: Any) -> ToolResult:
            # Pretend fetch_page already tried browser and returned its content.
            return ToolResult(
                content="rendered via browser (handled inside fetch_page)",
                citations=[_cit("https://x.test")],
            )

        cfg = _cfg()
        cfg.browser.enabled = True
        cfg.fetch_page.min_content_chars_for_browser_fallback = 500
        reg = _registry({"fetch_page": _fetch_page})
        text, cits = await _fetch_html_source("https://x.test", reg, cfg)
        assert "rendered via browser" in text
        assert "trafilatura extraction low-yield" not in text  # no raw-HTML-excerpt retry here
        assert len(cits) == 1

    @pytest.mark.asyncio
    async def test_html_fetch_no_fetch_page_returns_warning(self) -> None:
        cfg = _cfg()
        reg = _registry()  # nothing registered
        text, cits = await _fetch_html_source("https://x.test", reg, cfg)
        assert "fetch_page tool not registered" in text
        assert cits == []

    @pytest.mark.asyncio
    async def test_pdf_download_failure_returns_error_text(self) -> None:
        # Direct PDF fetch hits a non-existent host -> returns an error string
        text, cits, page_urls = await _fetch_pdf_source(
            "https://nonexistent.invalid/x.pdf", _registry()
        )
        assert "failed to download PDF" in text
        assert cits == []
        assert page_urls == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_pdf_source_with_render_returns_page_urls(self, tmp_path) -> None:
        # Live httpx-mocked download + stubbed pdf tools
        async def _extract(**kw: Any) -> ToolResult:
            return ToolResult(content="extracted text")

        async def _render(**kw: Any) -> ToolResult:
            import json
            return ToolResult(content=json.dumps({"pages": ["data:image/jpeg;base64,AAAA"], "count": 1}))

        reg = _registry({"pdf_extract_text": _extract, "pdf_render_pages": _render})

        pdf_bytes = b"%PDF-1.5 dummy"
        respx.get("https://cdn.test/x.pdf").mock(
            return_value=httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"})
        )

        text, _cits, page_urls = await _fetch_pdf_source(
            "https://cdn.test/x.pdf", reg, render_pages=True, max_pages=10
        )
        assert text == "extracted text"
        assert len(page_urls) == 1
        assert page_urls[0].startswith("data:image/jpeg;base64,")


# ---------------------------------------------------------------------------
# url_source() — top-level dispatcher
# ---------------------------------------------------------------------------


class TestUrlSourceDispatcher:
    @pytest.mark.asyncio
    async def test_arxiv_url_calls_analyze_and_renders(self, monkeypatch) -> None:
        # Arrange: stub the helpers to avoid hitting the network
        async def _fake_arxiv(_aid: str, _tools: ToolRegistry, **_kw: Any):
            return ("paper body text", "My Title", [_cit("https://arxiv.org/abs/2401.12345", source_type="arxiv")], [])

        monkeypatch.setattr(_us_module, "_fetch_arxiv_source", _fake_arxiv)

        # Stub the analyze node to return a known SourceAnalysis
        async def _fake_analyze(**kw: Any) -> SourceAnalysis:
            return _analysis(title="My Title", summary="arxiv summary")

        monkeypatch.setattr(_us_module, "analyze_source_node", _fake_analyze, raising=True)
        # Note: url_source() references analyze_source_node as a module-global,
        # so we patch the module's bound name directly.

        cfg = _cfg()
        reg = _registry()
        client = _FakeAsyncOpenAI("")  # won't be used since analyze is stubbed
        report = await url_source(
            "https://arxiv.org/abs/2401.12345", "summarize this", client, reg, cfg
        )
        assert report.path == "url_source"  # no follow-up trigger phrase
        assert "Source Analysis" in report.markdown
        assert "arxiv" in report.markdown
        assert "arxiv summary" in report.markdown
        assert any(c.url == "https://arxiv.org/abs/2401.12345" for c in report.citations)

    @pytest.mark.asyncio
    async def test_html_url_uses_fetch_page_then_analyze(self, monkeypatch) -> None:
        async def _fake_html(_url: str, _tools: ToolRegistry, _cfg: AgentTopConfig, **kw: Any):
            return ("extracted blog text", [_cit("https://blog.example/post")])

        monkeypatch.setattr(_us_module, "_fetch_html_source", _fake_html)

        async def _fake_analyze(**kw: Any) -> SourceAnalysis:
            return _analysis(title="Blog Post", summary="blog summary here")

        monkeypatch.setattr(_us_module, "analyze_source_node", _fake_analyze)

        cfg = _cfg()
        reg = _registry()
        client = _FakeAsyncOpenAI("")
        report = await url_source(
            "https://blog.example/post", "what does it say", client, reg, cfg
        )
        assert report.path == "url_source"
        assert "blog summary here" in report.markdown
        assert "html" in report.markdown

    @pytest.mark.asyncio
    async def test_pdf_url_dispatches(self, monkeypatch) -> None:
        async def _fake_pdf(_url: str, _tools: ToolRegistry, **_kw: Any):
            return ("pdf extracted text", [_cit("https://x.test/p.pdf", source_type="pdf")], [])

        monkeypatch.setattr(_us_module, "_fetch_pdf_source", _fake_pdf)

        async def _fake_analyze(**kw: Any) -> SourceAnalysis:
            return _analysis(title="PDF doc", summary="pdf summary")

        monkeypatch.setattr(_us_module, "analyze_source_node", _fake_analyze)

        cfg = _cfg()
        reg = _registry()
        client = _FakeAsyncOpenAI("")
        report = await url_source(
            "https://x.test/p.pdf", "", client, reg, cfg
        )
        assert report.path == "url_source"
        assert "pdf summary" in report.markdown
        assert "pdf" in report.markdown.lower()

    @pytest.mark.asyncio
    async def test_unsupported_url_type_returns_unclear(self) -> None:
        # classify_url_sync returns UrlType.html for unknown; force an unexpected
        # branch by monkeypatching classify_url (the async version url_source calls)
        # to a sentinel value.
        cfg = _cfg()
        reg = _registry()
        client = _FakeAsyncOpenAI("")
        original = _us_module.classify_url

        class _SentinelType:
            value = "weird"

            def __eq__(self, other: Any) -> bool:  # type: ignore[override]
                return False  # never equals any UrlType

        async def _fake_classify(_url: str, **_kw: Any) -> Any:
            return _SentinelType()

        _us_module.classify_url = _fake_classify  # type: ignore[assignment]
        try:
            report = await url_source("https://weird.test", "q", client, reg, cfg)
            assert report.path == "unclear"
            assert "Unsupported URL type" in report.markdown
        finally:
            _us_module.classify_url = original  # type: ignore[assignment]

    @pytest.mark.asyncio
    async def test_fetch_failure_short_circuits_without_analyze(self, monkeypatch) -> None:
        # Content starting with "HTTP" triggers the fetch-failed branch
        async def _fake_html(_url: str, _tools: ToolRegistry, _cfg: AgentTopConfig, **kw: Any):
            return ("HTTP 503 from upstream", [])

        monkeypatch.setattr(_us_module, "_fetch_html_source", _fake_html)

        # This should NOT be called
        async def _no_analyze(**kw: Any) -> SourceAnalysis:
            raise AssertionError("analyze should not run on fetch failure")

        monkeypatch.setattr(_us_module, "analyze_source_node", _no_analyze)

        cfg = _cfg()
        reg = _registry()
        client = _FakeAsyncOpenAI("")
        report = await url_source(
            "https://broken.test", "summarize", client, reg, cfg
        )
        assert report.path == "url_source"
        assert "Source Fetch Failed" in report.markdown
        assert "HTTP 503" in report.markdown

    @pytest.mark.asyncio
    async def test_no_follow_up_when_query_neutral(self, monkeypatch) -> None:
        async def _fake_arxiv(_aid: str, _tools: ToolRegistry, **_kw: Any):
            return ("text", "T", [_cit("https://arxiv.org/abs/2401.12345", source_type="arxiv")], [])

        monkeypatch.setattr(_us_module, "_fetch_arxiv_source", _fake_arxiv)

        async def _fake_analyze(**kw: Any) -> SourceAnalysis:
            # Return gaps/follow_ups that COULD trigger follow-up — but query won't ask.
            return _analysis(
                title="T",
                summary="S",
                gaps=["gap one"],
                follow_ups=[{"topic": "Topic", "why": "because"}],
            )

        monkeypatch.setattr(_us_module, "analyze_source_node", _fake_analyze)

        cfg = _cfg()
        # Disable auto-follow-up explicitly
        cfg.url_source.auto_follow_up = False
        cfg.url_source.follow_up_trigger_phrases = []
        reg = _registry()
        client = _FakeAsyncOpenAI("")
        report = await url_source(
            "https://arxiv.org/abs/2401.12345", "summarize this paper", client, reg, cfg
        )
        assert report.path == "url_source"  # NOT url_source_with_followup
        # Gaps are surfaced in the rendered analysis section, but no follow-up research
        assert "Identified Gaps" in report.markdown
        # The follow-up research SECTION (appended by _maybe_run_follow_up) is distinct
        # from the renderer's "Follow-up Research Suggestions" heading; we check that
        # the actual deep-path handoff section was NOT appended.
        assert "## Follow-up Research\n\n" not in report.markdown

    @pytest.mark.asyncio
    async def test_follow_up_triggered_when_query_asks_for_gaps(self, monkeypatch) -> None:
        async def _fake_arxiv(_aid: str, _tools: ToolRegistry, **_kw: Any):
            return ("text", "T", [_cit("https://arxiv.org/abs/2401.12345", source_type="arxiv")], [])

        monkeypatch.setattr(_us_module, "_fetch_arxiv_source", _fake_arxiv)

        async def _fake_analyze(**kw: Any) -> SourceAnalysis:
            return _analysis(
                title="T",
                summary="S",
                gaps=["gap one", "gap two"],
            )

        monkeypatch.setattr(_us_module, "analyze_source_node", _fake_analyze)

        # Stub the deep path handoff so we don't run the real deep loop
        async def _fake_followup(classified, original_query, client, tools, config, **kwargs):
            from deep_research.state import Report
            return Report(markdown="## Follow-up Research\n\ndeep result body", path="deep")

        # _maybe_run_follow_up imports deep_research.paths.deep.deep_research lazily,
        # so patch it on the deep module's attribute.
        import deep_research.paths.deep as deep_mod

        original_deep = deep_mod.deep_research
        deep_mod.deep_research = _fake_followup  # type: ignore[assignment]
        try:
            cfg = _cfg()
            cfg.url_source.auto_follow_up = False
            cfg.url_source.follow_up_trigger_phrases = []
            reg = _registry()
            client = _FakeAsyncOpenAI("")
            report = await url_source(
                "https://arxiv.org/abs/2401.12345",
                "what are the gaps in this paper?",
                client,
                reg,
                cfg,
            )
            assert report.path == "url_source_with_followup"
            assert "deep result body" in report.markdown
        finally:
            deep_mod.deep_research = original_deep  # type: ignore[assignment]

    @pytest.mark.asyncio
    async def test_follow_up_not_run_when_no_gaps_or_follow_ups(self, monkeypatch) -> None:
        # Even with a trigger-phrase query, no gaps/follow_ups -> no follow-up section
        async def _fake_arxiv(_aid: str, _tools: ToolRegistry, **_kw: Any):
            return ("text", "T", [_cit("https://arxiv.org/abs/2401.12345", source_type="arxiv")], [])

        monkeypatch.setattr(_us_module, "_fetch_arxiv_source", _fake_arxiv)

        async def _fake_analyze(**kw: Any) -> SourceAnalysis:
            return _analysis(title="T", summary="S")  # no gaps, no follow_ups

        monkeypatch.setattr(_us_module, "analyze_source_node", _fake_analyze)

        cfg = _cfg()
        reg = _registry()
        client = _FakeAsyncOpenAI("")
        report = await url_source(
            "https://arxiv.org/abs/2401.12345",
            "what are the gaps?",  # triggers, but analysis has no gaps
            client,
            reg,
            cfg,
        )
        # path reflects "wants_follow_up" (trigger phrase present) regardless of
        # whether the deep handoff actually produced content; only the markdown
        # distinguishes whether a follow-up SECTION was appended.
        assert report.path == "url_source_with_followup"
        assert "## Follow-up Research\n\n" not in report.markdown
