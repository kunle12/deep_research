"""Tavily web_search tests — mocked HTTP via respx + a live skipped test."""

from __future__ import annotations

import os

import httpx
import pytest
import respx

from deep_research.config import AgentTopConfig
from deep_research.tools import build_tool_registry


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


FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/search"


def _firecrawl_resp(*hits: tuple[str, str, str]) -> dict:
    return {
        "success": True,
        "data": {
            "web": [
                {"url": url, "title": title, "description": desc}
                for url, title, desc in hits
            ]
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_search_returns_citations(monkeypatch) -> None:
    cfg = AgentTopConfig()
    cfg.search.primary = "firecrawl"
    cfg.search.fallback_chain = []
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-fake-test-key")

    respx.post(FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_firecrawl_resp(
                ("https://example.com/a", "Result A", "Result A snippet."),
                ("https://example.com/b", "Result B", "Result B snippet."),
            ),
        )
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("web_search", {"query": "test query", "max_results": 5})
    assert res.error is None
    assert len(res.citations) == 2
    assert res.citations[0].url == "https://example.com/a"
    assert res.citations[0].title == "Result A"
    assert res.citations[0].snippet == "Result A snippet."
    # No relevance score from Firecrawl → uniform confidence
    assert res.citations[0].confidence_score == 0.5
    assert res.citations[1].url == "https://example.com/b"


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_fallback_when_tavily_unavailable(monkeypatch) -> None:
    """Second-priority ordering: Tavily 502s → Firecrawl serves."""
    cfg = AgentTopConfig()
    cfg.search.primary = "tavily"
    cfg.search.fallback_chain = ["firecrawl", "searxng"]
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake-test-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-fake-test-key")

    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(502))
    fc_route = respx.post(FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_firecrawl_resp(("https://example.com/fc1", "FC 1", "snippet"))
        )
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("web_search", {"query": "test", "max_results": 5})
    assert res.error is None, f"expected firecrawl fallback; error={res.error}"
    assert fc_route.called
    assert len(res.citations) == 1
    assert res.citations[0].url == "https://example.com/fc1"


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_rate_limit_then_searxng_fallback(monkeypatch) -> None:
    """Firecrawl 429 → retry exhausts → seamless fallback to SearXNG."""
    cfg = AgentTopConfig()
    cfg.search.primary = "firecrawl"
    cfg.search.fallback_chain = ["searxng"]
    cfg.search.firecrawl.rate_limit_retries = 1
    cfg.search.searxng.url = "https://searxng.test/search"
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-fake-test-key")

    fc_route = respx.post(FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(429, json={"success": False, "error": "rate limit"})
    )
    searxng_resp = {
        "results": [{"url": "https://example.com/sx1", "title": "SearX fallback", "content": "ok"}]
    }
    respx.get("https://searxng.test/search").mock(
        return_value=httpx.Response(200, json=searxng_resp)
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("web_search", {"query": "test", "max_results": 5})
    assert res.error is None, f"expected fallback to succeed; error={res.error}"
    assert fc_route.call_count == 2  # initial + 1 retry
    assert len(res.citations) == 1
    assert res.citations[0].url == "https://example.com/sx1"


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_proactive_quota_fallback(monkeypatch) -> None:
    """When Firecrawl max_calls_per_session is exceeded, proactively fall back."""
    cfg = AgentTopConfig()
    cfg.search.primary = "firecrawl"
    cfg.search.fallback_chain = ["searxng"]
    cfg.search.firecrawl.max_calls_per_session = 1
    cfg.search.searxng.url = "https://searxng.test/search"
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-fake-test-key")

    respx.post(FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_firecrawl_resp(("https://example.com/a", "A", "snippet"))
        )
    )
    searxng_resp = {
        "results": [{"url": "https://example.com/sx1", "title": "SearX quota", "content": "ok"}]
    }
    respx.get("https://searxng.test/search").mock(
        return_value=httpx.Response(200, json=searxng_resp)
    )

    reg = await build_tool_registry(cfg)
    res1 = await reg.call("web_search", {"query": "first", "max_results": 5})
    assert res1.error is None
    assert res1.citations[0].url == "https://example.com/a"

    # Second call exceeds quota → falls back to SearXNG
    res2 = await reg.call("web_search", {"query": "second", "max_results": 5})
    assert res2.error is None
    assert res2.citations[0].url == "https://example.com/sx1"


@pytest.mark.asyncio
@respx.mock
async def test_firecrawl_skipped_without_key(monkeypatch) -> None:
    """Chain includes firecrawl but no key set → skipped, SearXNG serves."""
    cfg = AgentTopConfig()
    cfg.search.primary = "firecrawl"
    cfg.search.fallback_chain = ["searxng"]
    cfg.search.searxng.url = "https://searxng.test/search"
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    fc_route = respx.post(FIRECRAWL_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_firecrawl_resp())
    )
    searxng_resp = {
        "results": [{"url": "https://example.com/sx1", "title": "SearX", "content": "ok"}]
    }
    respx.get("https://searxng.test/search").mock(
        return_value=httpx.Response(200, json=searxng_resp)
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("web_search", {"query": "test", "max_results": 5})
    assert res.error is None
    assert not fc_route.called  # firecrawl never hit without a key
    assert res.citations[0].url == "https://example.com/sx1"


@pytest.mark.asyncio
async def test_live_firecrawl_search(requires_firecrawl) -> None:
    """Real Firecrawl call — only runs when FIRECRAWL_API_KEY is set."""
    cfg = AgentTopConfig()
    cfg.search.primary = "firecrawl"
    cfg.search.fallback_chain = []
    reg = await build_tool_registry(cfg)
    res = await reg.call("web_search", {"query": "python asyncio gather", "max_results": 3})
    assert res.error is None
    assert len(res.citations) >= 1
    assert all(c.url.startswith("http") for c in res.citations)
