"""fetch_page tests — mocked via respx + a live test against example.com."""

from __future__ import annotations

import httpx
import pytest
import respx

from deep_research.config import AgentTopConfig
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


@pytest.mark.asyncio
async def test_live_fetch_example_com(requires_tavily) -> None:
    """Live test against example.com — exercises real httpx + trafilatura."""
    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call("fetch_page", {"url": "https://example.com"})
    # example.com returns minimal content -> falls back to raw HTML excerpt.
    assert res.error is None
    assert "Example" in res.content or "example" in res.content.lower()
