"""Bot-detection / blocked-source handling tests.

Covers:
- `classify_blocked_response` + `detect_challenge_vendor` unit behaviour
- fetch_page e2e: 403 Cloudflare, 200 challenge body (no browser fallback),
  negative-cache (no re-fetch within TTL), browser-blocked propagation
- "Unavailable Sources" rendering + `ResearchState.absorb_blocked_sources`
- deep path: researcher 4-tuple -> Report.blocked_sources + markdown section
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.paths.deep import deep_research
from deep_research.report.markdown import (
    render_blocked_sources_markdown,
    render_report_markdown,
)
from deep_research.state import (
    BlockedSource,
    ClassifiedQuery,
    Critique,
    QueryPlan,
    Report,
    ResearchPlan,
    ResearchState,
    SubQuestion,
)
from deep_research.tools import build_tool_registry
from deep_research.tools.fetch_page import (
    BlockedVerdict,
    classify_blocked_response,
    detect_challenge_vendor,
)

CLOUDFLARE_403 = (
    "<html><title>Attention Required! | Cloudflare</title>"
    "<body>Just a moment... <script>cf-chl-123</script></body></html>"
)
CHALLENGE_200 = (
    "<html><body><script>challenge-platform</script>"
    "<h1>Checking your browser before accessing</h1></body></html>"
)
DATADOME_200 = "<html><body>DDoS Protection by DataDome blocks this request</body></html>"
CLEAN_200 = (
    "<html><head><title>Article</title></head><body>"
    "<p>This is a real article with plenty of content.</p></body></html>"
)
ARCHIVED_HTML = (
    "<html><head><title>Archived Article</title></head><body><h1>Archived</h1><p>"
    + "The archived article body text. " * 120
    + "</p></body></html>"
)


def _cfg(tmp_path) -> AgentTopConfig:
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    return cfg


# ---------------------------------------------------------------------------
# Classifier unit tests
# ---------------------------------------------------------------------------


class TestClassifier:
    def test_403_cloudflare_body_is_bot_detection(self) -> None:
        v = classify_blocked_response(403, {}, CLOUDFLARE_403)
        assert v is not None
        assert v.error == "BLOCKED:bot_detection:cloudflare (403)"

    def test_200_challenge_page_is_bot_detection(self) -> None:
        v = classify_blocked_response(200, {}, CHALLENGE_200)
        assert v is not None
        assert v.error == "BLOCKED:bot_detection:cloudflare (200)"

    def test_200_datadome_page_is_bot_detection(self) -> None:
        v = classify_blocked_response(200, {}, DATADOME_200)
        assert v is not None
        assert v.error == "BLOCKED:bot_detection:datadome (200)"

    def test_429_is_rate_limited(self) -> None:
        v = classify_blocked_response(429, {}, "")
        assert v is not None
        assert v.error == "BLOCKED:rate_limited (429)"

    def test_404_is_not_found(self) -> None:
        v = classify_blocked_response(404, {}, "")
        assert v is not None
        assert v.error == "BLOCKED:not_found (404)"

    def test_500_is_generic_http_error(self) -> None:
        v = classify_blocked_response(500, {}, "")
        assert v is not None
        assert v.error == "BLOCKED:http_error (500)"

    def test_403_without_challenge_markers_is_http_error(self) -> None:
        v = classify_blocked_response(403, {}, "<html>access denied</html>")
        assert v is not None
        assert v.error == "BLOCKED:http_error (403)"

    def test_clean_200_is_none(self) -> None:
        assert classify_blocked_response(200, {}, CLEAN_200) is None

    def test_header_challenge_signal(self) -> None:
        v = classify_blocked_response(200, {"cf-mitigated": "challenge"}, CLEAN_200)
        assert v is not None
        assert v.error == "BLOCKED:bot_detection:cloudflare (200)"

    def test_detect_challenge_vendor_markers(self) -> None:
        assert detect_challenge_vendor("Just a moment...") == "cloudflare"
        assert detect_challenge_vendor("g-recaptcha widget") == "recaptcha"
        assert detect_challenge_vendor("REAL ARTICLE TEXT") is None
        assert detect_challenge_vendor("") is None

    def test_verdict_without_status(self) -> None:
        assert BlockedVerdict("bot_detection", "cloudflare", None).error == (
            "BLOCKED:bot_detection:cloudflare"
        )


# ---------------------------------------------------------------------------
# fetch_page e2e — blocked responses
# ---------------------------------------------------------------------------


class TestFetchPageBlocked:
    @pytest.mark.asyncio
    @respx.mock
    async def test_403_cloudflare_returns_blocked_error(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        cfg.fetch_page.archive_org_fallback = False
        respx.get("https://cf.test/article").mock(
            return_value=httpx.Response(403, text=CLOUDFLARE_403)
        )
        reg = await build_tool_registry(cfg)
        res = await reg.call("fetch_page", {"url": "https://cf.test/article"})
        assert res.error == "BLOCKED:bot_detection:cloudflare (403)"
        assert res.content == ""
        assert res.citations == []
        await reg.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_200_challenge_skips_browser_fallback(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        cfg.fetch_page.min_content_chars_for_browser_fallback = 10
        cfg.browser.enabled = True
        cfg.fetch_page.archive_org_fallback = False
        respx.get("https://cf.test/challenge").mock(
            return_value=httpx.Response(200, text=CHALLENGE_200)
        )
        reg = await build_tool_registry(cfg)
        browser_called = {"n": 0}

        async def _stub_navigate(**kw: Any) -> ToolResult:
            browser_called["n"] += 1
            return ToolResult(content="rendered article")

        reg._tools["browser_navigate"] = _stub_navigate  # type: ignore[attr-defined]

        res = await reg.call("fetch_page", {"url": "https://cf.test/challenge"})
        assert res.error == "BLOCKED:bot_detection:cloudflare (200)"
        assert browser_called["n"] == 0
        await reg.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_blocked_verdict_is_negatively_cached(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        cfg.fetch_page.archive_org_fallback = False
        route = respx.get("https://cf.test/article2").mock(
            return_value=httpx.Response(403, text=CLOUDFLARE_403)
        )
        reg = await build_tool_registry(cfg)
        r1 = await reg.call("fetch_page", {"url": "https://cf.test/article2"})
        r2 = await reg.call("fetch_page", {"url": "https://cf.test/article2"})
        assert r1.error == r2.error == "BLOCKED:bot_detection:cloudflare (403)"
        assert route.call_count == 1, "blocked URL must not be re-fetched"
        await reg.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_browser_blocked_error_is_propagated(self, tmp_path) -> None:
        """If the browser fallback itself reports a challenge, fetch_page
        surfaces the BLOCKED error instead of raw challenge HTML."""
        cfg = _cfg(tmp_path)
        cfg.fetch_page.min_content_chars_for_browser_fallback = 10
        cfg.browser.enabled = True
        respx.get("https://render.test/post").mock(
            return_value=httpx.Response(200, text="<html><body><p>tiny</p></body></html>")
        )
        reg = await build_tool_registry(cfg)

        async def _stub_navigate(**kw: Any) -> ToolResult:
            return ToolResult(
                content="",
                error="BLOCKED:bot_detection:cloudflare (browser challenge)",
            )

        reg._tools["browser_navigate"] = _stub_navigate  # type: ignore[attr-defined]

        res = await reg.call("fetch_page", {"url": "https://render.test/post"})
        assert res.error == "BLOCKED:bot_detection:cloudflare (browser challenge)"
        assert res.content == ""
        await reg.close()


# ---------------------------------------------------------------------------
# Wayback Machine auto-fallback
# ---------------------------------------------------------------------------


_WAYBACK_PREFIX = re.compile(r"^https://web\.archive\.org/web/2/")


class TestArchiveFallback:
    @pytest.mark.asyncio
    @respx.mock
    async def test_blocked_article_rescued_via_wayback(self, tmp_path) -> None:
        respx.get("https://cf.test/article").mock(
            return_value=httpx.Response(403, text=CLOUDFLARE_403)
        )
        respx.get(_WAYBACK_PREFIX).mock(return_value=httpx.Response(200, text=ARCHIVED_HTML))
        reg = await build_tool_registry(_cfg(tmp_path))
        res = await reg.call("fetch_page", {"url": "https://cf.test/article"})
        assert res.error is None
        assert "Wayback Machine" in res.content
        assert "https://cf.test/article" in res.content  # provenance notes original
        assert "archived article body" in res.content
        assert len(res.citations) == 1
        cit = res.citations[0]
        assert cit.url.startswith("https://web.archive.org/web/")
        assert cit.confidence_score == 0.5
        await reg.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_wayback_snapshot_keeps_blocked_error(self, tmp_path) -> None:
        respx.get("https://cf.test/uncaptured").mock(
            return_value=httpx.Response(403, text=CLOUDFLARE_403)
        )
        respx.get(_WAYBACK_PREFIX).mock(return_value=httpx.Response(404))
        reg = await build_tool_registry(_cfg(tmp_path))
        res = await reg.call("fetch_page", {"url": "https://cf.test/uncaptured"})
        assert res.error == "BLOCKED:bot_detection:cloudflare (403)"
        assert res.content == ""
        await reg.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_archive_fallback_disabled_skips_wayback(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        cfg.fetch_page.archive_org_fallback = False
        respx.get("https://cf.test/nofallback").mock(
            return_value=httpx.Response(403, text=CLOUDFLARE_403)
        )
        wayback_route = respx.get(_WAYBACK_PREFIX).mock(
            return_value=httpx.Response(200, text=ARCHIVED_HTML)
        )
        reg = await build_tool_registry(cfg)
        res = await reg.call("fetch_page", {"url": "https://cf.test/nofallback"})
        assert res.error == "BLOCKED:bot_detection:cloudflare (403)"
        assert wayback_route.call_count == 0
        await reg.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_archive_rescue_is_cached_under_original_url(self, tmp_path) -> None:
        original_route = respx.get("https://cf.test/cached").mock(
            return_value=httpx.Response(403, text=CLOUDFLARE_403)
        )
        wayback_route = respx.get(_WAYBACK_PREFIX).mock(
            return_value=httpx.Response(200, text=ARCHIVED_HTML)
        )
        reg = await build_tool_registry(_cfg(tmp_path))
        r1 = await reg.call("fetch_page", {"url": "https://cf.test/cached"})
        r2 = await reg.call("fetch_page", {"url": "https://cf.test/cached"})
        assert r1.error is None and r2.error is None
        assert "Wayback Machine" in r1.content and "Wayback Machine" in r2.content
        # Both the original and the wayback snapshot are fetched exactly once.
        assert original_route.call_count == 1
        assert wayback_route.call_count == 1
        await reg.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_wayback_fallback_for_archive_urls(self, tmp_path) -> None:
        respx.get("https://web.archive.org/web/2020/https://example.com").mock(
            return_value=httpx.Response(403, text=CLOUDFLARE_403)
        )
        wayback_route = respx.get(_WAYBACK_PREFIX).mock(
            return_value=httpx.Response(200, text=ARCHIVED_HTML)
        )
        reg = await build_tool_registry(_cfg(tmp_path))
        res = await reg.call(
            "fetch_page", {"url": "https://web.archive.org/web/2020/https://example.com"}
        )
        assert res.error == "BLOCKED:bot_detection:cloudflare (403)"
        assert wayback_route.call_count == 0
        await reg.close()


# ---------------------------------------------------------------------------
# Reporting + state
# ---------------------------------------------------------------------------


class TestBlockedReporting:
    def test_render_blocked_sources_markdown(self) -> None:
        md = render_blocked_sources_markdown(
            [BlockedSource(url="https://a.test/x", reason="BLOCKED:bot_detection:cloudflare (403)")]
        )
        assert "## Unavailable Sources" in md
        assert "https://a.test/x" in md
        assert "bot_detection" in md
        assert render_blocked_sources_markdown([]) == ""

    def test_render_report_markdown_appends_section_when_missing(self) -> None:
        report = Report(
            markdown="# Report\n\nBody",
            blocked_sources=[
                BlockedSource(url="https://b.test/y", reason="BLOCKED:rate_limited (429)")
            ],
        )
        rendered = render_report_markdown(report, AgentTopConfig().output)
        assert "## Unavailable Sources" in rendered
        assert "https://b.test/y" in rendered
        # Section appears exactly once (deep/url_source already embed it).
        assert rendered.count("## Unavailable Sources") == 1

    def test_render_report_markdown_does_not_duplicate_section(self) -> None:
        report = Report(
            markdown="# Report\n\n## Unavailable Sources\n\n- [x](x)",
            blocked_sources=[
                BlockedSource(url="https://b.test/y", reason="BLOCKED:not_found (404)")
            ],
        )
        rendered = render_report_markdown(report, AgentTopConfig().output)
        assert rendered.count("## Unavailable Sources") == 1

    def test_state_absorb_blocked_sources_dedups_and_annotates(self) -> None:
        state = ResearchState(query="q")
        state.absorb_blocked_sources(
            [BlockedSource(url="https://a.test/1", reason="BLOCKED:http_error (500)")],
            sq_id="sq1",
        )
        state.absorb_blocked_sources(
            [BlockedSource(url="https://a.test/1", reason="BLOCKED:http_error (500)")],
            sq_id="sq2",
        )
        state.absorb_blocked_sources(
            [BlockedSource(url="https://a.test/2", reason="BLOCKED:not_found (404)")],
            sq_id="sq2",
        )
        assert len(state.blocked_sources) == 2
        assert state.blocked_sources[0].sub_question == "sq1"
        assert state.blocked_sources[1].sub_question == "sq2"


# ---------------------------------------------------------------------------
# Deep path — researcher 4-tuple wiring
# ---------------------------------------------------------------------------


class TestDeepPathBlockedSources:
    @pytest.mark.asyncio
    async def test_blocked_sources_reach_report_and_markdown(self) -> None:
        cfg = AgentTopConfig()
        plan_result = ResearchPlan(
            sub_questions=[SubQuestion(id="sq1", question="What is X?", rationale="r")],
            breadth=1,
            max_depth=0,
        )
        blocked = BlockedSource(
            url="https://blocked.test/article",
            reason="BLOCKED:bot_detection:cloudflare (403)",
        )

        async def _researcher(sq, client, model, tools, **kwargs):
            return ("X is Y. Source: https://ok.test", [], [], [blocked])

        with (
            patch("deep_research.paths.deep.planner_plan", return_value=plan_result),
            patch("deep_research.paths.deep.researcher_run", side_effect=_researcher),
            patch(
                "deep_research.paths.deep.critic_review",
                return_value=Critique(sufficient=True, rationale="covered", gaps=[]),
            ),
            patch("deep_research.paths.deep.writer_write", return_value="# Deep Report\n\nX is Y."),
        ):
            client = MagicMock()
            reg = ToolRegistry()
            report = await deep_research(
                ClassifiedQuery(path=QueryPlan.deep, rationale="test"),
                "What is X?",
                client,
                reg,
                cfg,
            )

        assert len(report.blocked_sources) == 1
        assert report.blocked_sources[0].url == "https://blocked.test/article"
        assert report.blocked_sources[0].sub_question == "sq1"
        assert "## Unavailable Sources" in report.markdown
        assert "https://blocked.test/article" in report.markdown
        assert report.markdown.count("## Unavailable Sources") == 1


# ---------------------------------------------------------------------------
# url_source path — blocked PDF / blocked HTML short-circuit
# ---------------------------------------------------------------------------


async def _fake_classify_pdf(url: str, **kw: Any) -> Any:
    from deep_research.tools.url_classifier import UrlType

    return UrlType.pdf


class TestUrlSourceBlocked:
    @pytest.mark.asyncio
    @respx.mock
    async def test_download_pdf_403_returns_blocked_error(self, tmp_path) -> None:
        from deep_research.paths.url_source import _download_pdf_to_cache

        respx.get("https://cdn.test/x.pdf").mock(
            return_value=httpx.Response(
                403,
                text=CLOUDFLARE_403,
                headers={"content-type": "text/html"},
            )
        )
        result, archive_url = await _download_pdf_to_cache(
            "https://cdn.test/x.pdf",
            str(tmp_path / "pdfs"),
            archive_fallback=False,
        )
        assert result == "BLOCKED:bot_detection:cloudflare (403)"
        assert archive_url is None

    @pytest.mark.asyncio
    async def test_blocked_pdf_error_short_circuits_to_blocked_report(
        self, monkeypatch, tmp_path
    ) -> None:
        import sys as _sys

        us_module = _sys.modules["deep_research.paths.url_source"]

        async def _fake_pdf(_url: str, _tools: ToolRegistry, **_kw: Any):
            return ("BLOCKED:http_error (403)", [], [])

        monkeypatch.setattr(us_module, "_fetch_pdf_source", _fake_pdf)

        async def _no_analyze(**kw: Any) -> Any:
            raise AssertionError("analyze must not run on a blocked source")

        monkeypatch.setattr(us_module, "analyze_source_node", _no_analyze)
        monkeypatch.setattr(us_module, "classify_url", _fake_classify_pdf)

        cfg = _cfg(tmp_path)
        reg = ToolRegistry()
        report = await us_module.url_source(
            "https://x.test/p.pdf",
            "summarize",
            MagicMock(),
            reg,
            cfg,
        )
        assert report.path == "url_source"
        assert "# Source Blocked" in report.markdown
        assert "BLOCKED:http_error (403)" in report.markdown
        assert len(report.blocked_sources) == 1
        assert report.blocked_sources[0].url == "https://x.test/p.pdf"


# ---------------------------------------------------------------------------
# PDF downloads — Wayback Machine auto-fallback
# ---------------------------------------------------------------------------


PDF_BYTES = b"%PDF-1.5\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF\n"


class TestPdfWaybackFallback:
    @pytest.mark.asyncio
    @respx.mock
    async def test_pdf_blocked_rescued_via_wayback(self, tmp_path) -> None:
        from deep_research.paths.url_source import _download_pdf_to_cache

        respx.get("https://cdn.test/paper.pdf").mock(
            return_value=httpx.Response(
                403,
                text=CLOUDFLARE_403,
                headers={"content-type": "text/html"},
            )
        )
        respx.get(_WAYBACK_PREFIX).mock(
            return_value=httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"content-type": "application/pdf"},
            )
        )
        result, archive_url = await _download_pdf_to_cache(
            "https://cdn.test/paper.pdf", str(tmp_path / "pdfs")
        )
        assert isinstance(result, Path)
        assert result.read_bytes().startswith(b"%PDF-")
        assert archive_url is not None
        assert archive_url.startswith("https://web.archive.org/web/")

    @pytest.mark.asyncio
    @respx.mock
    async def test_pdf_wayback_html_capture_not_saved(self, tmp_path) -> None:
        from deep_research.paths.url_source import _download_pdf_to_cache

        respx.get("https://cdn.test/notpdf.pdf").mock(
            return_value=httpx.Response(
                403,
                text=CLOUDFLARE_403,
                headers={"content-type": "text/html"},
            )
        )
        respx.get(_WAYBACK_PREFIX).mock(return_value=httpx.Response(200, text=ARCHIVED_HTML))
        result, archive_url = await _download_pdf_to_cache(
            "https://cdn.test/notpdf.pdf", str(tmp_path / "pdfs")
        )
        assert isinstance(result, str)
        assert result == "BLOCKED:bot_detection:cloudflare (403)"
        assert archive_url is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_pdf_wayback_disabled_skips_archive(self, tmp_path) -> None:
        from deep_research.paths.url_source import _download_pdf_to_cache

        respx.get("https://cdn.test/nofallback.pdf").mock(
            return_value=httpx.Response(
                403,
                text=CLOUDFLARE_403,
                headers={"content-type": "text/html"},
            )
        )
        wayback_route = respx.get(_WAYBACK_PREFIX).mock(
            return_value=httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"content-type": "application/pdf"},
            )
        )
        result, archive_url = await _download_pdf_to_cache(
            "https://cdn.test/nofallback.pdf",
            str(tmp_path / "pdfs"),
            archive_fallback=False,
        )
        assert result == "BLOCKED:bot_detection:cloudflare (403)"
        assert archive_url is None
        assert wayback_route.call_count == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_pdf_fetch_source_cites_archive_url(self, tmp_path) -> None:
        from deep_research.paths.url_source import _fetch_pdf_source

        respx.get("https://cdn.test/full.pdf").mock(
            return_value=httpx.Response(
                403,
                text=CLOUDFLARE_403,
                headers={"content-type": "text/html"},
            )
        )
        respx.get(_WAYBACK_PREFIX).mock(
            return_value=httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"content-type": "application/pdf"},
            )
        )

        async def _extract(**kw: Any) -> ToolResult:
            return ToolResult(content="extracted archived pdf text")

        reg = ToolRegistry()
        reg.register("pdf_extract_text", _extract, {"type": "function"})

        text, cits, page_urls = await _fetch_pdf_source(
            "https://cdn.test/full.pdf",
            reg,
            pdf_cache_dir=str(tmp_path / "pdfs"),
        )
        assert "Wayback Machine" in text
        assert "extracted archived pdf text" in text
        assert len(cits) == 1
        assert cits[0].url.startswith("https://web.archive.org/web/")
        assert cits[0].confidence_score == 0.5
        assert page_urls == []
