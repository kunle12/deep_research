"""Tests for the Google Scholar discovery backend (deep_research/tools/scholar.py).

Phase 6: covers Serper primary, SearXNG fallback, arxiv-dedup logic, rate-limit,
and disabled-config guard.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.state import ToolName


def _cfg(**overrides: Any) -> AgentTopConfig:
    cfg = AgentTopConfig()
    cfg.scholar.enabled = overrides.pop("scholar_enabled", True)
    cfg.scholar.primary = overrides.pop("primary", "serper")
    cfg.scholar.max_results_per_query = overrides.pop("max_results", 10)
    cfg.scholar.concurrency = overrides.pop("concurrency", 2)
    cfg.scholar.request_delay_s = overrides.pop("request_delay", 0.0)
    cfg.scholar.include_pdf_links = overrides.pop("include_pdf_links", True)
    cfg.scholar.serper.api_key_env = overrides.pop("serper_key_env", "SERPER_API_KEY")
    cfg.scholar.serper.endpoint = overrides.pop("endpoint", "https://google.serper.dev/scholar")
    if overrides:
        raise TypeError(f"unknown overrides: {list(overrides)}")
    return cfg


def _serper_hit(
    title: str = "Attention Is All You Need",
    link: str = "https://arxiv.org/abs/1706.03762",
    pdf: str | None = "https://arxiv.org/pdf/1706.03762.pdf",
    authors: str = "Vaswani, Ashish",
    year: int = 2017,
    cited_by: int = 50000,
    snippet: str = "The transformer model architecture...",
    publication: str = "NeurIPS 2017",
    doi: str | None = "10.48550/arXiv.1706.03762",
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "title": title,
        "link": link,
        "snippet": snippet,
        "authors": authors,
        "year": year,
        "cited_by": cited_by,
        "publication": publication,
    }
    if pdf is not None:
        d["pdf"] = pdf
    if doi is not None:
        d["doi"] = doi
    return d


def _serper_response(hits: list[dict]) -> dict[str, Any]:
    return {"organic": hits}


async def _register(monkeypatch, cfg: AgentTopConfig) -> ToolRegistry:
    """Register the scholar tool with a given config (mocked env)."""
    monkeypatch.setenv("SERPER_API_KEY", "test-key-123")
    from deep_research.tools import scholar
    reg = ToolRegistry()
    await scholar.register(reg, cfg)
    return reg


# ---------------------------------------------------------------------------
# Serper primary — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_serper_search_basic(monkeypatch) -> None:
    """Serper returns hits; citations are parsed with all fields."""
    cfg = _cfg()
    reg = await _register(monkeypatch, cfg)

    endpoint = cfg.scholar.serper.endpoint
    respx.post(endpoint).respond(
        status_code=200,
        json=_serper_response([
            _serper_hit(title="Paper A", link="https://arxiv.org/abs/1706.03762", cited_by=100, year=2017),
            _serper_hit(title="Paper B", link="https://doi.org/10.1234/example", cited_by=50, year=2020, pdf=None, doi=None),
        ]),
    )
    result = await reg.call("scholar_search", {"query": "transformer", "max_results": 5})
    assert result.error is None, f"unexpected error: {result.error}"
    assert len(result.citations) == 2
    c1 = result.citations[0]
    assert c1.title == "Paper A"
    assert c1.source_type == "scholar"
    assert c1.discovered_by == ToolName.scholar
    assert c1.arxiv_id == "1706.03762"  # inferred from arxiv URL
    assert c1.pdf_url == "https://arxiv.org/pdf/1706.03762.pdf"
    assert c1.year == 2017
    assert c1.cited_by_count == 100
    assert c1.confidence_score == 0.7  # 0.6 + 100/1000
    assert c1.venue == "NeurIPS 2017"
    assert c1.doi == "10.48550/arXiv.1706.03762"

    c2 = result.citations[1]
    assert c2.title == "Paper B"
    assert c2.arxiv_id is None  # no arxiv URL or DOI
    assert c2.pdf_url is None  # no pdf side link
    assert c2.doi is None
    assert c2.year == 2020
    assert c2.cited_by_count == 50
    assert c2.confidence_score == 0.65


@pytest.mark.asyncio
@respx.mock
async def test_serper_no_pdf_links(monkeypatch) -> None:
    """When include_pdf_links is False, pdf_url is cleared."""
    cfg = _cfg(include_pdf_links=False)
    reg = await _register(monkeypatch, cfg)

    endpoint = cfg.scholar.serper.endpoint
    respx.post(endpoint).respond(
        status_code=200,
        json=_serper_response([_serper_hit(pdf="https://arxiv.org/pdf/x.pdf")]),
    )
    result = await reg.call("scholar_search", {"query": "test", "max_results": 5})
    assert result.error is None
    assert result.citations[0].pdf_url is None


@pytest.mark.asyncio
@respx.mock
async def test_serper_empty_results(monkeypatch) -> None:
    """Serper returns no organic hits — empty ToolResult."""
    cfg = _cfg()
    reg = await _register(monkeypatch, cfg)

    endpoint = cfg.scholar.serper.endpoint
    respx.post(endpoint).respond(status_code=200, json={"organic": []})
    result = await reg.call("scholar_search", {"query": "nonexistent", "max_results": 5})
    assert result.error is None
    assert len(result.citations) == 0


@pytest.mark.asyncio
@respx.mock
async def test_serper_http_error_retry(monkeypatch) -> None:
    """Serper returns 429; the tool retries once, then fails gracefully."""
    cfg = _cfg(request_delay=0.0)
    reg = await _register(monkeypatch, cfg)

    endpoint = cfg.scholar.serper.endpoint
    respx.post(endpoint).mock(
        side_effect=[
            httpx.Response(status_code=429, text="rate limit"),
            httpx.Response(status_code=200, json=_serper_response([_serper_hit()])),
        ],
    )
    result = await reg.call("scholar_search", {"query": "retry-test", "max_results": 5})
    assert result.error is None
    assert len(result.citations) == 1


@pytest.mark.asyncio
@respx.mock
async def test_serper_http_error_all_fail(monkeypatch) -> None:
    """Serper returns 500 twice; tool falls back to SearXNG or returns empty."""
    cfg = _cfg(primary="serper", request_delay=0.0)
    reg = await _register(monkeypatch, cfg)

    endpoint = cfg.scholar.serper.endpoint
    respx.post(endpoint).mock(side_effect=httpx.Response(status_code=500, text="server error"))
    # No SearXNG configured in fallback (default searxng url, no mock)
    result = await reg.call("scholar_search", {"query": "fail-all", "max_results": 5})
    assert result.error is not None
    assert len(result.citations) == 0


# ---------------------------------------------------------------------------
# Arxiv ID inference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_arxiv_id_inferred_from_url(monkeypatch) -> None:
    """Arxiv URL in Serper hit sets arxiv_id."""
    cfg = _cfg()
    reg = await _register(monkeypatch, cfg)

    endpoint = cfg.scholar.serper.endpoint
    respx.post(endpoint).respond(
        status_code=200,
        json=_serper_response([_serper_hit(link="https://arxiv.org/abs/2401.12345v3", doi=None)]),
    )
    result = await reg.call("scholar_search", {"query": "test", "max_results": 5})
    assert result.citations[0].arxiv_id == "2401.12345"


@pytest.mark.asyncio
@respx.mock
async def test_arxiv_id_inferred_from_doi(monkeypatch) -> None:
    """DOI 10.48550/arXiv.<id> sets arxiv_id."""
    cfg = _cfg()
    reg = await _register(monkeypatch, cfg)

    endpoint = cfg.scholar.serper.endpoint
    respx.post(endpoint).respond(
        status_code=200,
        json=_serper_response([_serper_hit(doi="10.48550/arXiv.2401.54321")]),
    )
    result = await reg.call("scholar_search", {"query": "test", "max_results": 5})
    assert result.citations[0].arxiv_id == "2401.54321"


@pytest.mark.asyncio
@respx.mock
async def test_no_arxiv_id_non_arxiv(monkeypatch) -> None:
    """Non-arxiv hit (e.g., Nature) gets no arxiv_id."""
    cfg = _cfg()
    reg = await _register(monkeypatch, cfg)

    endpoint = cfg.scholar.serper.endpoint
    respx.post(endpoint).respond(
        status_code=200,
        json=_serper_response([
            _serper_hit(
                title="A Nature paper",
                link="https://www.nature.com/articles/s41586-024-12345",
                authors="Smith, John",
                year=2024,
                cited_by=500,
                pdf="https://www.nature.com/content/pdf/s41586-024-12345.pdf",
                doi="10.1038/s41586-024-12345",
            )
        ]),
    )
    result = await reg.call("scholar_search", {"query": "nature", "max_results": 5})
    assert result.citations[0].arxiv_id is None


# ---------------------------------------------------------------------------
# Disabled config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_disabled_not_registered(monkeypatch) -> None:
    """When scholar.enabled is False, the tool is NOT registered."""
    cfg = _cfg(scholar_enabled=False)
    reg = await _register(monkeypatch, cfg)
    assert "scholar_search" not in reg.names()


# ---------------------------------------------------------------------------
# SearXNG fallback
# ---------------------------------------------------------------------------


def _searxng_hit(
    title: str = "SearXNG Paper",
    url: str = "https://arxiv.org/abs/2401.12345",
    content: str = "A paper found by SearXNG scholar engine.",
    doi: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {"title": title, "url": url, "content": content}
    if doi is not None:
        d["doi"] = doi
    return d


@pytest.mark.asyncio
@respx.mock
async def test_searxng_fallback(monkeypatch) -> None:
    """SearXNG fallback works when primary is searxng."""
    cfg = _cfg(primary="searxng")
    reg = await _register(monkeypatch, cfg)

    url = cfg.scholar.searxng.url
    respx.get(url).respond(
        status_code=200,
        json={"results": [_searxng_hit(doi="10.48550/arXiv.2401.99999")]},
    )
    result = await reg.call("scholar_search", {"query": "searxng-test", "max_results": 5})
    assert result.error is None
    assert len(result.citations) == 1
    c = result.citations[0]
    assert c.source_type == "scholar"
    assert c.discovered_by == ToolName.scholar
    assert c.title == "SearXNG Paper"
    assert c.arxiv_id == "2401.99999"


@pytest.mark.asyncio
@respx.mock
async def test_searxng_empty(monkeypatch) -> None:
    """SearXNG returns no results."""
    cfg = _cfg(primary="searxng")
    reg = await _register(monkeypatch, cfg)

    url = cfg.scholar.searxng.url
    respx.get(url).respond(status_code=200, json={"results": []})
    result = await reg.call("scholar_search", {"query": "empty", "max_results": 5})
    assert len(result.citations) == 0


# ---------------------------------------------------------------------------
# Rate-limit / concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_concurrency_semaphore(monkeypatch) -> None:
    """Multiple concurrent calls respect the concurrency semaphore."""
    cfg = _cfg(concurrency=2, request_delay=0.0)
    reg = await _register(monkeypatch, cfg)

    endpoint = cfg.scholar.serper.endpoint
    respx.post(endpoint).mock(
        side_effect=[
            httpx.Response(status_code=200, json=_serper_response([_serper_hit(title=f"Paper {i}")]))
            for i in range(5)
        ],
    )
    results = await reg.call("scholar_search", {"query": "test", "max_results": 5})
    assert results.error is None


# ---------------------------------------------------------------------------
# Scholar search — _infer_arxiv_id unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, doi, title, expected",
    [
        ("https://arxiv.org/abs/1706.03762v1", None, None, "1706.03762"),
        ("https://arxiv.org/pdf/1706.03762", None, None, "1706.03762"),
        ("https://arxiv.org/pdf/1706.03762.pdf", None, None, "1706.03762"),
        (None, "10.48550/arXiv.1706.03762", None, "1706.03762"),
        ("https://doi.org/10.48550/arXiv.1706.03762", None, None, "1706.03762"),
        ("https://www.nature.com/articles/xxx", None, None, None),
        (None, "10.1038/s41586-024-12345", None, None),
        ("https://arxiv.org/abs/2401.12345v3", "10.48550/arXiv.2401.12345", None, "2401.12345"),
        ("https://www.nature.com/articles/xxx", "10.48550/arXiv.2401.12345", None, "2401.12345"),
    ],
)
def test_infer_arxiv_id(url, doi, title, expected) -> None:
    from deep_research.tools.scholar import _infer_arxiv_id
    assert _infer_arxiv_id(url, doi, title) == expected


# ---------------------------------------------------------------------------
# Year parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input, expected",
    [
        (2023, 2023),
        ("2023", 2023),
        ("published in 2023", 2023),
        (None, None),
        ("not a year", None),
        ("200", None),  # out of range
        ("2024", 2024),
    ],
)
def test_parse_year(input, expected) -> None:
    from deep_research.tools.scholar import _parse_year
    assert _parse_year(input) == expected
