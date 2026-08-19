"""fetch_page tests — mocked via respx + a live test against example.com."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolResult
from deep_research.tools import build_tool_registry


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_extracts_article_text(tmp_path) -> None:
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")  # isolate cache per-test
    # Keep this a pure-trafilatura test by disabling browser fallback.
    cfg.browser.enabled = False
    cfg.fetch_page.min_content_chars_for_browser_fallback = 10**9

    html = """
    <html><head><title>A Test Article</title></head>
    <body>
      <h1>Title</h1>
      <p>This is the article body. It is long enough that trafilatura should
         extract it successfully without fallback to raw HTML. We add more text
         to exceed the min-content-chars threshold easily.</p>
      <p>Another paragraph also present in the main text.</p>
    </body></html>
    """
    respx.get("https://example.test/article").mock(return_value=httpx.Response(200, text=html))

    reg = await build_tool_registry(cfg)
    res = await reg.call("fetch_page", {"url": "https://example.test/article"})
    assert res.error is None
    assert "article body" in res.content
    assert len(res.citations) == 1
    cit = res.citations[0]
    assert cit.url == "https://example.test/article"
    assert cit.title == "A Test Article"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_returns_http_error(tmp_path) -> None:
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    respx.get("https://404.test/missing").mock(return_value=httpx.Response(404))

    reg = await build_tool_registry(cfg)
    res = await reg.call("fetch_page", {"url": "https://404.test/missing"})
    assert res.error == "BLOCKED:not_found (404)"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_page_cache_hits(tmp_path) -> None:
    """Same URL fetched twice should hit cache second time."""
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    html = """
    <html><head><title>Cache Test</title></head>
    <body>
      <p>This is longer article content with more words to exceed thresholds.</p>
      <p>Adding paragraphs here to make sure trafilatura picks it up.</p>
    </body></html>
    """
    route = respx.get("https://cache.test/x").mock(return_value=httpx.Response(200, text=html))
    reg = await build_tool_registry(cfg)
    # First call: HTTP
    await reg.call("fetch_page", {"url": "https://cache.test/x"})
    assert route.call_count == 1
    # Second call: should come from cache, NOT re-hit HTTP
    await reg.call("fetch_page", {"url": "https://cache.test/x"})
    assert route.call_count == 1, "second call should have hit cache, not HTTP"


FIRECRAWL_SCRAPE_ENDPOINT = "https://api.firecrawl.dev/v2/scrape"

_BLOCKED_HTML = "<html><body>Just a moment... Checking your browser.</body></html>"

_WAYBACK_ARTICLE = """
<html><head><title>Archived Article</title></head>
<body>
  <p>This archived article body is long enough that trafilatura should extract
     it successfully. We add a second sentence to be safely above the minimum
     archive text threshold used by the Wayback rescue path.</p>
</body></html>
"""


def _rescue_cfg(tmp_path, monkeypatch) -> AgentTopConfig:
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.fetch_page.firecrawl_rescue = True
    cfg.browser.enabled = False
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-fake-test-key")
    return cfg


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_rescue_blocked_page(tmp_path, monkeypatch) -> None:
    """Bot-blocked page → Firecrawl scrape rescue returns fresh markdown."""
    cfg = _rescue_cfg(tmp_path, monkeypatch)
    respx.get("https://blocked.test/article").mock(
        return_value=httpx.Response(403, text=_BLOCKED_HTML)
    )
    scrape_route = respx.post(FIRECRAWL_SCRAPE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "# Rescued Article\n\n" + "Rescued body text. " * 20,
                    "metadata": {"title": "Rescued Article", "statusCode": 200},
                },
            },
        )
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("fetch_page", {"url": "https://blocked.test/article"})
    assert res.error is None
    assert "Rescued body text." in res.content
    assert len(res.citations) == 1
    assert res.citations[0].url == "https://blocked.test/article"
    assert res.citations[0].title == "Rescued Article"
    assert scrape_route.call_count == 1

    # Second call: served from the "firecrawl" cache entry, no re-scrape.
    res2 = await reg.call("fetch_page", {"url": "https://blocked.test/article"})
    assert res2.error is None
    assert "Rescued body text." in res2.content
    assert scrape_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_rescue_single_flight(tmp_path, monkeypatch) -> None:
    """Concurrent fetch_page calls for one blocked URL share ONE scrape."""
    cfg = _rescue_cfg(tmp_path, monkeypatch)
    respx.get("https://blocked.test/x").mock(
        return_value=httpx.Response(403, text=_BLOCKED_HTML)
    )
    scrape_route = respx.post(FIRECRAWL_SCRAPE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "rescued body text. " * 15,
                    "metadata": {"title": "X"},
                },
            },
        )
    )

    reg = await build_tool_registry(cfg)
    r1, r2 = await asyncio.gather(
        reg.call("fetch_page", {"url": "https://blocked.test/x"}),
        reg.call("fetch_page", {"url": "https://blocked.test/x"}),
    )
    assert r1.error is None and r2.error is None
    assert scrape_route.call_count == 1  # single-flight — not 2 billed scrapes


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_rescue_failure_falls_to_wayback(tmp_path, monkeypatch) -> None:
    """Firecrawl scrape fails → existing Wayback fallback still applies."""
    cfg = _rescue_cfg(tmp_path, monkeypatch)
    respx.get("https://blocked.test/article").mock(
        return_value=httpx.Response(403, text=_BLOCKED_HTML)
    )
    respx.post(FIRECRAWL_SCRAPE_ENDPOINT).mock(return_value=httpx.Response(500))
    respx.get("https://web.archive.org/web/2/https://blocked.test/article").mock(
        return_value=httpx.Response(200, text=_WAYBACK_ARTICLE)
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("fetch_page", {"url": "https://blocked.test/article"})
    assert res.error is None
    assert "Wayback Machine archive" in res.content  # provenance annotation
    assert "archived article body" in res.content


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_rescue_quota_exhausted(tmp_path, monkeypatch) -> None:
    """After max_per_session rescues, blocked pages go straight to Wayback."""
    cfg = _rescue_cfg(tmp_path, monkeypatch)
    cfg.fetch_page.firecrawl_rescue_max_per_session = 1

    respx.get("https://blocked.test/a").mock(
        return_value=httpx.Response(403, text=_BLOCKED_HTML)
    )
    respx.get("https://blocked.test/b").mock(
        return_value=httpx.Response(403, text=_BLOCKED_HTML)
    )
    scrape_route = respx.post(FIRECRAWL_SCRAPE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "Rescued markdown body. " * 15,
                    "metadata": {"title": "A"},
                },
            },
        )
    )
    respx.get("https://web.archive.org/web/2/https://blocked.test/b").mock(
        return_value=httpx.Response(200, text=_WAYBACK_ARTICLE)
    )

    reg = await build_tool_registry(cfg)
    res_a = await reg.call("fetch_page", {"url": "https://blocked.test/a"})
    assert res_a.error is None
    assert scrape_route.call_count == 1

    # Quota spent — second blocked URL must NOT hit Firecrawl.
    res_b = await reg.call("fetch_page", {"url": "https://blocked.test/b"})
    assert res_b.error is None
    assert "Wayback Machine archive" in res_b.content
    assert scrape_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_rescue_disabled_by_default(tmp_path, monkeypatch) -> None:
    """Default config (rescue off) never calls Firecrawl for blocked pages."""
    cfg = AgentTopConfig()
    cfg.fetch_page.cache_dir = str(tmp_path / "cache")
    cfg.browser.enabled = False
    cfg.fetch_page.archive_org_fallback = False
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-fake-test-key")

    respx.get("https://blocked.test/article").mock(
        return_value=httpx.Response(403, text=_BLOCKED_HTML)
    )
    scrape_route = respx.post(FIRECRAWL_SCRAPE_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"markdown": "x" * 200}})
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("fetch_page", {"url": "https://blocked.test/article"})
    assert res.error == "BLOCKED:bot_detection:cloudflare (403)"
    assert scrape_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_rescue_skips_pdf_and_not_found(tmp_path, monkeypatch) -> None:
    """PDF URLs and 404s are never sent to Firecrawl rescue."""
    cfg = _rescue_cfg(tmp_path, monkeypatch)
    cfg.fetch_page.archive_org_fallback = False

    respx.get("https://blocked.test/paper.pdf").mock(
        return_value=httpx.Response(403, text=_BLOCKED_HTML)
    )
    respx.get("https://blocked.test/gone").mock(
        return_value=httpx.Response(404, text="not found")
    )
    scrape_route = respx.post(FIRECRAWL_SCRAPE_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"markdown": "x" * 200}})
    )

    reg = await build_tool_registry(cfg)
    res_pdf = await reg.call("fetch_page", {"url": "https://blocked.test/paper.pdf"})
    assert res_pdf.error is not None
    res_404 = await reg.call("fetch_page", {"url": "https://blocked.test/gone"})
    assert res_404.error == "BLOCKED:not_found (404)"
    assert scrape_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_rescue_browser_blocked_path(tmp_path, monkeypatch) -> None:
    """A challenge hit inside the browser fallback is also Firecrawl-rescued."""
    cfg = _rescue_cfg(tmp_path, monkeypatch)

    thin_html = "<html><head><title>Tiny</title></head><body><p>Hi.</p></body></html>"
    respx.get("https://blocked.test/js").mock(
        return_value=httpx.Response(200, text=thin_html)
    )
    scrape_route = respx.post(FIRECRAWL_SCRAPE_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "rescued body text. " * 15,
                    "metadata": {"title": "JS Page"},
                },
            },
        )
    )

    reg = await build_tool_registry(cfg)

    async def _stub_browser(url: str, **_: object) -> ToolResult:
        return ToolResult(content="", error="BLOCKED:bot_detection:cloudflare (403)")

    reg.register(
        "browser_navigate",
        _stub_browser,
        {
            "description": "stub",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    )
    cfg.browser.enabled = True  # low-yield path now sees the (stubbed) browser

    res = await reg.call("fetch_page", {"url": "https://blocked.test/js"})
    assert res.error is None
    assert "rescued body text." in res.content
    assert scrape_route.call_count == 1


@pytest.mark.asyncio
async def test_live_fetch_example_com(requires_tavily) -> None:
    """Live test against example.com — exercises real httpx + trafilatura."""
    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call("fetch_page", {"url": "https://example.com"})
    # example.com returns minimal content -> falls back to raw HTML excerpt.
    assert res.error is None
    assert "Example" in res.content or "example" in res.content.lower()
