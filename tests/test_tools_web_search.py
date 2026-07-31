"""Tavily web_search tests — mocked HTTP via respx + a live skipped test."""

from __future__ import annotations

import os

import httpx
import pytest
import respx

from deep_research.config import AgentTopConfig
from deep_research.tools import build_tool_registry
from deep_research.tools import web_search as _web_search_mod


@pytest.fixture(autouse=True)
def _reset_web_search_globals() -> None:
    """Reset module-level globals between tests to avoid cross-test pollution."""
    _web_search_mod._tavily_call_count = 0


def _make_cfg_with_tavily_key() -> AgentTopConfig:
    cfg = AgentTopConfig()
    return cfg


@pytest.mark.asyncio
@respx.mock
async def test_tavily_search_returns_citations() -> None:
    cfg = _make_cfg_with_tavily_key()
    os.environ.pop("TAVILY_API_KEY", None)
    os.environ["TAVILY_API_KEY"] = "tvly-fake-test-key"

    tavily_resp = {
        "results": [
            {
                "url": "https://example.com/a",
                "title": "Result A",
                "content": "Result A snippet.",
                "score": 0.95,
            },
            {
                "url": "https://example.com/b",
                "title": "Result B",
                "content": "Result B snippet.",
                "score": 0.7,
            },
        ]
    }
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json=tavily_resp)
    )

    reg = await build_tool_registry(cfg)
    assert "web_search" in reg.names()
    res = await reg.call("web_search", {"query": "test query", "max_results": 5})
    assert res.error is None
    assert len(res.citations) == 2
    assert res.citations[0].url == "https://example.com/a"
    assert res.citations[0].title == "Result A"
    assert res.citations[0].confidence_score == 0.95
    assert res.citations[1].url == "https://example.com/b"

    os.environ.pop("TAVILY_API_KEY", None)


@pytest.mark.asyncio
@respx.mock
async def test_searxng_fallback_when_tavily_unavailable() -> None:
    """When Tavily 502s, fall back to SearXNG."""
    cfg = AgentTopConfig()
    cfg.search.primary = "tavily"
    cfg.search.fallback_chain = ["searxng"]
    cfg.search.searxng.url = "https://searxng.test/search"
    os.environ["TAVILY_API_KEY"] = "tvly-fake-test-key"

    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(502))
    searxng_resp = {
        "results": [
            {"url": "https://example.com/sx1", "title": "SearX 1", "content": "snippet 1"},
            {"url": "https://example.com/sx2", "title": "SearX 2", "content": "snippet 2"},
        ]
    }
    respx.get("https://searxng.test/search").mock(
        return_value=httpx.Response(200, json=searxng_resp)
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("web_search", {"query": "test", "max_results": 5})
    assert res.error is None, f"expected fallback to succeed; error={res.error}"
    assert len(res.citations) == 2
    assert all(c.url.startswith("https://example.com/sx") for c in res.citations)

    os.environ.pop("TAVILY_API_KEY", None)


@pytest.mark.asyncio
@respx.mock
async def test_tavily_rate_limit_then_fallback() -> None:
    """Tavily 429 (rate limit) → retry exhausts → seamless fallback to SearXNG."""
    cfg = AgentTopConfig()
    cfg.search.primary = "tavily"
    cfg.search.fallback_chain = ["searxng"]
    cfg.search.tavily.rate_limit_retries = 1  # retry once before falling back
    cfg.search.searxng.url = "https://searxng.test/search"
    os.environ["TAVILY_API_KEY"] = "tvly-fake-test-key"

    # Tavily returns 429 twice (first call + retry both hit limit)
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(429, json={"detail": {"error": "rate limit exceeded"}})
    )
    searxng_resp = {
        "results": [
            {"url": "https://example.com/sx1", "title": "SearX fallback", "content": "ok"},
        ]
    }
    respx.get("https://searxng.test/search").mock(
        return_value=httpx.Response(200, json=searxng_resp)
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("web_search", {"query": "test", "max_results": 5})
    assert res.error is None, f"expected fallback to succeed; error={res.error}"
    assert len(res.citations) == 1
    assert res.citations[0].url == "https://example.com/sx1"

    os.environ.pop("TAVILY_API_KEY", None)


@pytest.mark.asyncio
@respx.mock
async def test_tavily_proactive_quota_fallback() -> None:
    """When Tavily max_calls_per_session is exceeded, proactively fall back to SearXNG."""
    cfg = AgentTopConfig()
    cfg.search.primary = "tavily"
    cfg.search.fallback_chain = ["searxng"]
    cfg.search.tavily.max_calls_per_session = 1
    cfg.search.searxng.url = "https://searxng.test/search"
    os.environ["TAVILY_API_KEY"] = "tvly-fake-test-key"

    tavily_resp = {
        "results": [
            {"url": "https://example.com/a", "title": "A", "content": "snippet", "score": 0.8}
        ]
    }
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json=tavily_resp)
    )
    searxng_resp = {
        "results": [{"url": "https://example.com/sx1", "title": "SearX quota", "content": "ok"}]
    }
    respx.get("https://searxng.test/search").mock(
        return_value=httpx.Response(200, json=searxng_resp)
    )

    reg = await build_tool_registry(cfg)
    # First call uses Tavily (quota not exceeded)
    res1 = await reg.call("web_search", {"query": "first", "max_results": 5})
    assert res1.error is None
    assert len(res1.citations) == 1
    assert res1.citations[0].url == "https://example.com/a"

    # Second call exceeds quota → falls back to SearXNG
    res2 = await reg.call("web_search", {"query": "second", "max_results": 5})
    assert res2.error is None
    assert len(res2.citations) == 1
    assert res2.citations[0].url == "https://example.com/sx1"

    os.environ.pop("TAVILY_API_KEY", None)


@pytest.mark.asyncio
async def test_no_backends_returns_error() -> None:
    """When no tavily key AND searxng unreachable, return a clean error."""
    cfg = AgentTopConfig()
    cfg.search.primary = "searxng"
    cfg.search.fallback_chain = []
    cfg.search.searxng.url = "https://nonexistent.invalid/search"
    reg = await build_tool_registry(cfg)
    res = await reg.call("web_search", {"query": "test", "max_results": 5})
    assert res.error is not None
    assert "failed" in res.content.lower() or "no backend" in res.content.lower()


@pytest.mark.asyncio
async def test_live_tavily_search(requires_tavily) -> None:
    """Real Tavily call — only runs when TAVILY_API_KEY is set."""
    cfg = AgentTopConfig()
    reg = await build_tool_registry(cfg)
    res = await reg.call("web_search", {"query": "python asyncio gather", "max_results": 3})
    assert res.error is None
    assert len(res.citations) >= 1
    assert all(c.url.startswith("http") for c in res.citations)
