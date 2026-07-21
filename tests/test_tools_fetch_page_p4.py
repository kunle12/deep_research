"""P4 tests — fetch_page browser-fallback (in-tool) + URL classifier HEAD probe.

P4 narrowed the architecture: the low-yield → browser fallback now lives
inside `tools/fetch_page.py` itself, so caller paths (researcher, url_source,
planner) benefit uniformly. This file complements the existing
`test_tools_fetch_page.py` (which keeps the trafilatura-only path pure by
disabling the browser) by asserting the new fallback behaviors.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolResult
from deep_research.tools import build_tool_registry
from deep_research.tools.url_classifier import (
    UrlType,
    classify_url,
    classify_url_sync,
    head_probe_content_type,
)

# ---------------------------------------------------------------------------
# fetch_page in-tool browser fallback
# ---------------------------------------------------------------------------


def _long_html(title: str = "Long", body: str = "") -> str:
    body = body or ("This is substantial article body text. " * 200)
    return (
        f"<html><head><title>{title}</title></head><body>"
        f"<h1>{title}</h1><p>{body}</p></body></html>"
    )


def _short_html(title: str = "Short") -> str:
    return f"<html><head><title>{title}</title></head><body><p>x</p></body></html>"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_falls_back_to_browser_when_content_low(tmp_path) -> None:
    """P4: when trafilatura yields < threshold AND browser is enabled AND
    `browser_navigate` is registered, fetch_page calls it and surfaces its
    content as the result.
    """
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    # Keep threshold well above the short-trafilatura output length:
    cfg.fetch_page.min_content_chars_for_browser_fallback = 10
    cfg.browser.enabled = True

    # fetch_page's real HTTP call returns short HTML (trafilatura yield < threshold)
    respx.get("https://js.test/post").mock(
        return_value=httpx.Response(200, text=_short_html("JS Heavy"))
    )
    # browser_navigate is a *real* registered stub (ship-included P1 stub), so it
    # won't do real navigation; we override it with a deterministic stub by
    # re-registering after build_tool_registry's run. But ToolRegistry doesn't
    # allow re-register, so instead we monkeypatch by injecting state via the
    # agent's config-driven `browser.enabled` + a custom tool added manually.

    reg = await build_tool_registry(cfg)

    # Swap the browser_navigate registered by build_tool_registry with a stub.
    # Easiest: directly replace the underlying function dict entry.
    async def _stub_navigate(**kw: Any) -> Any:
        from deep_research.llm.tool_loop import ToolResult

        return ToolResult(
            content="rendered body via playwright MCP",
            citations=[],
        )

    reg._tools["browser_navigate"] = _stub_navigate  # type: ignore[attr-defined]

    res = await reg.call("fetch_page", {"url": "https://js.test/post"})
    assert res.error is None
    assert "rendered body via playwright MCP" in res.content
    # fetch_page synthesizes a Citation when browser returns none
    assert len(res.citations) == 1
    assert res.citations[0].discovered_by is not None
    assert res.citations[0].discovered_by.value == "browser"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_returns_browser_citations_verbatim(tmp_path) -> None:
    """When browser_navigate returns its own citations, fetch_page passes through."""
    from deep_research.llm.tool_loop import ToolResult
    from deep_research.state import Citation, ToolName

    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.fetch_page.min_content_chars_for_browser_fallback = 10
    cfg.browser.enabled = True

    respx.get("https://js.test/x").mock(return_value=httpx.Response(200, text=_short_html("Short")))

    reg = await build_tool_registry(cfg)

    async def _stub_navigate(**kw: Any) -> ToolResult:
        return ToolResult(
            content="browser-rendered page text",
            citations=[
                Citation(
                    url="https://js.test/x",
                    title="Browser Site",
                    snippet="from browser",
                    source_type="html",
                    confidence_score=0.6,
                    discovered_by=ToolName.browser,
                )
            ],
        )

    reg._tools["browser_navigate"] = _stub_navigate  # type: ignore[attr-defined]

    res = await reg.call("fetch_page", {"url": "https://js.test/x"})
    assert res.error is None
    assert "browser-rendered page text" in res.content
    assert len(res.citations) == 1
    assert res.citations[0].title == "Browser Site"
    assert res.citations[0].discovered_by.value == "browser"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_no_browser_fallback_when_browser_disabled(tmp_path) -> None:
    """When config.browser.enabled is False, fetch_page returns the raw HTML
    excerpt instead of attempting browser fallback, even if browser_navigate
    happens to be registered.
    """
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.fetch_page.min_content_chars_for_browser_fallback = 10
    cfg.browser.enabled = False  # disable dropdown explicitly

    respx.get("https://no-browser.test/").mock(
        return_value=httpx.Response(200, text=_short_html("No Browser"))
    )

    reg = await build_tool_registry(cfg)
    # browser_navigate still registered because pdf_vision / browser default true

    res = await reg.call("fetch_page", {"url": "https://no-browser.test/"})
    assert res.error is None
    # Should hit the low-yield raw-HTML branch, NOT the browser fallback
    assert "raw HTML excerpt" in res.content


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_browser_fallback_failure_falls_back_to_raw_html(
    tmp_path,
) -> None:
    """If the browser fallback errors, fetch_page degrades to raw HTML excerpt."""
    from deep_research.llm.tool_loop import ToolResult

    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.fetch_page.min_content_chars_for_browser_fallback = 10
    cfg.browser.enabled = True

    respx.get("https://browser-fails.test/").mock(
        return_value=httpx.Response(200, text=_short_html("Fail"))
    )

    reg = await build_tool_registry(cfg)

    async def _fail_navigate(**kw: Any) -> ToolResult:
        return ToolResult(content="", error="headless browser crashed")

    reg._tools["browser_navigate"] = _fail_navigate  # type: ignore[attr-defined]

    res = await reg.call("fetch_page", {"url": "https://browser-fails.test/"})
    assert res.error is None
    # Browser errored; should land on the raw-HTML-excerpt fallback
    assert "raw HTML excerpt" in res.content


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_no_browser_tool_registered_skips_browser(tmp_path) -> None:
    """If browser_navigate isn't registered at all, fetch_page doesn't crash."""
    raw_html = _short_html("No Browser Tool")
    respx.get("https://no-nav.test/x").mock(return_value=httpx.Response(200, text=raw_html))
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.fetch_page.min_content_chars_for_browser_fallback = 10

    # Hide browser + pdf tools from registry using a custom builder path:
    # disable both browser and pdf_vision so they won't register.
    cfg.browser.enabled = False
    cfg.pdf_vision.enabled = False

    reg = await build_tool_registry(cfg)
    assert "browser_navigate" not in reg.names()

    res = await reg.call("fetch_page", {"url": "https://no-nav.test/x"})
    assert res.error is None
    assert "raw HTML excerpt" in res.content


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_long_extraction_skips_browser(tmp_path) -> None:
    """When trafilatura yields >= threshold chars, fetch_page returns the
    extracted text directly — no browser invocation."""
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.browser.enabled = True  # default; but should NOT engage when content ok

    long_body = "Substantive body paragraph with detailed prose content. " * 50
    respx.get("https://long.test/article").mock(
        return_value=httpx.Response(200, text=_long_html("Long Article", body=long_body))
    )

    reg = await build_tool_registry(cfg)

    # Sabotage browser_navigate so the test fails loudly if it ever got called.
    async def _should_not_call(**kw: Any) -> Any:
        raise AssertionError("browser fallback should NOT have been called")

    reg._tools["browser_navigate"] = _should_not_call  # type: ignore[attr-defined]

    res = await reg.call("fetch_page", {"url": "https://long.test/article"})
    assert res.error is None
    assert "Substantive body paragraph" in res.content
    assert len(res.citations) == 1
    assert res.citations[0].discovered_by is not None
    assert res.citations[0].discovered_by.value == "fetch_page"  # not browser


# ---------------------------------------------------------------------------
# url_classifier: head_probe_content_type + classify_url (async)
# ---------------------------------------------------------------------------


class TestHeadProbe:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_pdf_content_type(self) -> None:
        respx.head("https://cdn.test/file").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/pdf"})
        )
        ctype = await head_probe_content_type("https://cdn.test/file")
        assert ctype == "application/pdf"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_html_content_type_normalized(self) -> None:
        respx.head("https://cdn.test/page").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"})
        )
        ctype = await head_probe_content_type("https://cdn.test/page")
        # Splits off the charset suffix and lowercases
        assert ctype == "text/html"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_404(self) -> None:
        respx.head("https://404.test/x").mock(return_value=httpx.Response(404))
        ctype = await head_probe_content_type("https://404.test/x")
        assert ctype is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_transport_error(self) -> None:
        respx.head("https://err.test/x").mock(side_effect=httpx.ConnectError("no route"))
        ctype = await head_probe_content_type("https://err.test/x")
        assert ctype is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_http_url(self) -> None:
        assert await head_probe_content_type("ftp://x.test/file") is None
        assert await head_probe_content_type("") is None


class TestClassifyUrlAsync:
    @pytest.mark.asyncio
    async def test_arxiv_url_short_circuits_no_head_probe(self) -> None:
        # Even without any HTTP mock, the sync heuristic already decides arxiv,
        # so classify_url never makes a HEAD probe — no respx errors here.
        url_type = await classify_url("https://arxiv.org/abs/2401.12345")
        assert url_type == UrlType.arxiv

    @pytest.mark.asyncio
    async def test_pdf_url_short_circuits_no_head_probe(self) -> None:
        url_type = await classify_url("https://example.com/paper.pdf")
        assert url_type == UrlType.pdf

    @pytest.mark.asyncio
    @respx.mock
    async def test_html_url_head_probe_returns_pdf(self) -> None:
        """A URL without .pdf in path but PDF Content-Type is reclassified to pdf."""
        respx.head("https://cdn.test/signed-link").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/pdf"})
        )
        url_type = await classify_url("https://cdn.test/signed-link")
        assert url_type == UrlType.pdf

    @pytest.mark.asyncio
    @respx.mock
    async def test_html_url_head_probe_returns_html(self) -> None:
        respx.head("https://blog.test/post").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"})
        )
        url_type = await classify_url("https://blog.test/post")
        assert url_type == UrlType.html

    @pytest.mark.asyncio
    async def test_html_url_head_probe_errors_falls_back_to_html(self) -> None:
        # No respx mock — the HEAD probe will fail to connect; classify_url
        # should swallow the error and return html (sync heuristic default).
        url_type = await classify_url("https://nonexistent.invalid/x", head_probe_timeout_s=2.0)
        assert url_type == UrlType.html


# ---------------------------------------------------------------------------
# regression: classify_url_sync (unchanged behavior)
# ---------------------------------------------------------------------------


class TestClassifyUrlSyncUnchanged:
    def test_arxiv(self) -> None:
        assert classify_url_sync("https://arxiv.org/abs/2401.12345") == UrlType.arxiv

    def test_pdf(self) -> None:
        assert classify_url_sync("https://example.com/p.pdf") == UrlType.pdf

    def test_html(self) -> None:
        assert classify_url_sync("https://example.com/post") == UrlType.html

    def test_empty(self) -> None:
        assert classify_url_sync("") == UrlType.unknown


# ---------------------------------------------------------------------------
# P7: PDF detection + dispatch to pdf_extract_text inside fetch_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_pdf_by_content_type(tmp_path) -> None:
    """PDF detected via Content-Type header → saved + extracted via pdf_extract_text."""
    from deep_research.llm.tool_loop import ToolResult

    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.browser.enabled = False

    pdf_bytes = b"%PDF-1.4 fake pdf content"
    respx.get("https://cdn.test/doc").mock(
        return_value=httpx.Response(
            200,
            content=pdf_bytes,
            headers={"content-type": "application/pdf"},
        )
    )

    reg = await build_tool_registry(cfg)

    # Stub pdf_extract_text so it returns deterministic text without real parsing.
    async def _stub_extract(file_path: str, **kw: Any) -> ToolResult:
        assert file_path.endswith(".pdf"), f"expected pdf path: {file_path}"
        return ToolResult(content="Extracted PDF text body here")

    reg._tools["pdf_extract_text"] = _stub_extract  # type: ignore[attr-defined]

    res = await reg.call("fetch_page", {"url": "https://cdn.test/doc"})
    assert res.error is None
    assert "Extracted PDF text body here" in res.content
    assert len(res.citations) == 1
    cit = res.citations[0]
    assert cit.source_type == "pdf"
    assert cit.discovered_by is not None
    assert cit.discovered_by.value == "fetch_page"
    # PDF bytes should have been saved to disk
    pdfs_dir = tmp_path / "cache" / "pdfs"
    assert pdfs_dir.is_dir()
    pdf_files = list(pdfs_dir.iterdir())
    assert len(pdf_files) >= 1


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_pdf_by_url_suffix(tmp_path) -> None:
    """PDF detected via .pdf URL suffix when Content-Type is absent."""
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.browser.enabled = False

    pdf_bytes = b"%PDF-1.4 fake pdf content"
    # No Content-Type header set — respx default is text/plain
    respx.get("https://cdn.test/report.pdf").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    reg = await build_tool_registry(cfg)

    async def _stub_extract(file_path: str, **kw: Any) -> ToolResult:
        return ToolResult(content="Extracted from .pdf URL")

    reg._tools["pdf_extract_text"] = _stub_extract  # type: ignore[attr-defined]

    res = await reg.call("fetch_page", {"url": "https://cdn.test/report.pdf"})
    assert res.error is None
    assert "Extracted from .pdf URL" in res.content


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_pdf_extract_fails_returns_error(tmp_path) -> None:
    """PDF extraction yields 0 chars → fetch_page returns clear error."""
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.browser.enabled = False

    pdf_bytes = b"%PDF-1.4 empty pdf"
    respx.get("https://cdn.test/empty").mock(
        return_value=httpx.Response(
            200,
            content=pdf_bytes,
            headers={"content-type": "application/pdf"},
        )
    )

    reg = await build_tool_registry(cfg)

    async def _stub_extract(file_path: str, **kw: Any) -> ToolResult:
        return ToolResult(content="")  # empty — simulates extraction failure

    reg._tools["pdf_extract_text"] = _stub_extract  # type: ignore[attr-defined]

    res = await reg.call("fetch_page", {"url": "https://cdn.test/empty"})
    assert res.error is not None
    assert "extracted 0 chars" in res.error


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_pdf_no_extract_tool_returns_error(tmp_path) -> None:
    """PDF detected but pdf_extract_text not registered → clear error."""
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.browser.enabled = False

    pdf_bytes = b"%PDF-1.4 fake"
    respx.get("https://cdn.test/no-extract").mock(
        return_value=httpx.Response(
            200,
            content=pdf_bytes,
            headers={"content-type": "application/pdf"},
        )
    )

    reg = await build_tool_registry(cfg)

    # Remove pdf_extract_text so the PDF branch has no tool to call.
    reg._tools.pop("pdf_extract_text", None)

    res = await reg.call("fetch_page", {"url": "https://cdn.test/no-extract"})
    assert res.error is not None
    assert "pdf_extract_text tool is not registered" in res.error


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_pdf_cache_hit_returns_text(tmp_path) -> None:
    """Cache hit on a PDF URL returns the previously extracted text directly."""
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.browser.enabled = False

    pdf_bytes = b"%PDF-1.4 fake pdf content"
    respx.get("https://cdn.test/doc").mock(
        return_value=httpx.Response(
            200,
            content=pdf_bytes,
            headers={"content-type": "application/pdf"},
        )
    )

    reg = await build_tool_registry(cfg)

    async def _stub_extract(file_path: str, **kw: Any) -> ToolResult:
        return ToolResult(content="Cached PDF text")

    reg._tools["pdf_extract_text"] = _stub_extract  # type: ignore[attr-defined]

    # First call — populates cache
    res1 = await reg.call("fetch_page", {"url": "https://cdn.test/doc"})
    assert res1.error is None
    assert "Cached PDF text" in res1.content

    # Second call — cache hit; should NOT call pdf_extract_text again
    # Replace the stub with one that would fail if called
    call_count = 0

    async def _should_not_call(file_path: str, **kw: Any) -> ToolResult:
        nonlocal call_count
        call_count += 1
        raise AssertionError("pdf_extract_text should NOT be called on cache hit")

    reg._tools["pdf_extract_text"] = _should_not_call  # type: ignore[attr-defined]

    res2 = await reg.call("fetch_page", {"url": "https://cdn.test/doc"})
    assert res2.error is None
    assert "Cached PDF text" in res2.content
    assert call_count == 0, "pdf_extract_text was called on cache hit"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_html_still_uses_trafilatura_not_pdf(tmp_path) -> None:
    """A normal HTML URL is unaffected by the PDF changes — still uses trafilatura."""
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.browser.enabled = False
    cfg.fetch_page.min_content_chars_for_browser_fallback = 10**9

    html = """
    <html><head><title>HTML Page</title></head>
    <body><p>This is normal HTML content that trafilatura should extract.</p></body>
    </html>
    """
    respx.get("https://html.test/page").mock(return_value=httpx.Response(200, text=html))

    reg = await build_tool_registry(cfg)
    res = await reg.call("fetch_page", {"url": "https://html.test/page"})
    assert res.error is None
    assert "normal HTML content" in res.content
    assert len(res.citations) == 1
    assert res.citations[0].source_type == "html"
