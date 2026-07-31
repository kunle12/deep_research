"""P5 tests — arxiv tool real implementations.

Mocks the sync `arxiv` library calls (via monkeypatch on our module's
`_sync_search` / `_sync_resolve`) and uses respx for the PDF-download httpx
flow. No network access required.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from deep_research.config import AgentTopConfig
from deep_research.state import Citation
from deep_research.tools import arxiv as arxiv_tool
from deep_research.tools import build_tool_registry

# ---------------------------------------------------------------------------
# Doubles for arxiv.Result
# ---------------------------------------------------------------------------


class _FakeAuthor:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeResult:
    def __init__(
        self,
        short_id: str,
        title: str,
        abstract: str,
        authors: list[str],
        pdf_url: str | None = None,
    ) -> None:
        self._short_id = short_id
        self.title = title
        self.summary = abstract
        self.authors = [_FakeAuthor(n) for n in authors]
        self.pdf_url = pdf_url

    def get_short_id(self) -> str:
        return self._short_id


def _citations_from(results: list[_FakeResult]) -> list[Citation]:
    """Mirror what the tool's _sync_search/_sync_resolve would return: Citation objects.

    The tool flow's content-build code expects Citation objects, so when we
    monkeypatch _sync_search / _sync_resolve we must return Citations (the
    real _sync_* do this via _result_to_citation).
    """
    return [arxiv_tool._result_to_citation(r) for r in results]


# ---------------------------------------------------------------------------
# search tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arxiv_search_returns_citations(monkeypatch) -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.request_delay_s = 0.0  # speed up the test

    fake_results = [
        _FakeResult("2401.12345", "Paper A", "Abstract A", ["Alice A"]),
        _FakeResult("2401.67890", "Paper B", "Abstract B", ["Bob B", "Carol C"]),
    ]

    def _fake_sync_search(query: str, max_results: int) -> list[Citation]:
        assert query == "rlhf"
        assert max_results == 5
        return _citations_from(fake_results)

    monkeypatch.setattr(arxiv_tool, "_sync_search", _fake_sync_search)

    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_search", {"query": "rlhf", "max_results": 5})
    assert res.error is None
    assert len(res.citations) == 2
    c0 = res.citations[0]
    assert c0.url == "https://arxiv.org/abs/2401.12345"
    assert c0.title == "Paper A"
    assert c0.arxiv_id == "2401.12345"
    assert c0.authors == ["Alice A"]
    assert c0.source_type == "arxiv"
    assert c0.discovered_by.value == "arxiv"

    # Content includes the title and id
    assert "Paper A" in res.content
    assert "2401.12345" in res.content


@pytest.mark.asyncio
async def test_arxiv_search_empty_results_returns_message(monkeypatch) -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.request_delay_s = 0.0
    monkeypatch.setattr(arxiv_tool, "_sync_search", lambda q, n: [])
    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_search", {"query": "obscure term", "max_results": 5})
    assert res.error is None
    assert "No arxiv search results" in res.content
    assert res.citations == []


@pytest.mark.asyncio
async def test_arxiv_search_caps_max_results(monkeypatch) -> None:
    """arxiv_search respects arxiv.max_results_per_query config cap."""
    cfg = AgentTopConfig()
    cfg.arxiv.max_results_per_query = 3
    cfg.arxiv.request_delay_s = 0.0

    captured: dict[str, Any] = {}

    def _fake_sync_search(query: str, max_results: int) -> list[Citation]:
        captured["max_results"] = max_results
        return _citations_from([_FakeResult("2401.1", "T", "S", [])])

    monkeypatch.setattr(arxiv_tool, "_sync_search", _fake_sync_search)
    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_search", {"query": "q", "max_results": 999})
    assert res.error is None
    assert captured["max_results"] == 3  # capped


# ---------------------------------------------------------------------------
# resolve tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arxiv_resolve_returns_citation(monkeypatch) -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.request_delay_s = 0.0

    fake_result = _FakeResult("2401.99999v2", "Resolved Paper", "Abstract R", ["X", "Y"])

    def _fake_sync_resolve(arxiv_id: str) -> Citation | None:
        assert arxiv_id == "2401.99999v2"
        return arxiv_tool._result_to_citation(fake_result)

    monkeypatch.setattr(arxiv_tool, "_sync_resolve", _fake_sync_resolve)

    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_resolve", {"arxiv_id": "2401.99999v2"})
    assert res.error is None
    assert len(res.citations) == 1
    c = res.citations[0]
    assert c.arxiv_id == "2401.99999"  # version stripped
    assert c.title == "Resolved Paper"
    assert c.authors == ["X", "Y"]
    assert "Resolved Paper" in res.content


@pytest.mark.asyncio
async def test_arxiv_resolve_not_found_returns_error(monkeypatch) -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.request_delay_s = 0.0
    monkeypatch.setattr(arxiv_tool, "_sync_resolve", lambda x: None)
    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_resolve", {"arxiv_id": "0000.00000"})
    assert res.error is not None
    assert "0000.00000" in res.error
    assert res.citations == []


@pytest.mark.asyncio
async def test_arxiv_resolve_empty_id_returns_error() -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.request_delay_s = 0.0
    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_resolve", {"arxiv_id": ""})
    assert res.error is not None
    assert "non-empty" in res.error


# ---------------------------------------------------------------------------
# download tool (uses respx for httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_arxiv_download_pdf_downloads_to_cache(tmp_path) -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.request_delay_s = 0.0
    cfg.arxiv.pdf_cache_dir = str(tmp_path / "arxiv_pdfs")
    pdf_bytes = b"%PDF-1.5 fake content"

    respx.get("https://arxiv.org/pdf/2401.12345.pdf").mock(
        return_value=httpx.Response(
            200, content=pdf_bytes, headers={"content-type": "application/pdf"}
        )
    )

    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_download_pdf", {"arxiv_id": "2401.12345"})
    assert res.error is None
    # The returned path should exist on disk and contain our PDF bytes
    from pathlib import Path

    p = Path(res.content)
    assert p.exists()
    assert p.read_bytes() == pdf_bytes
    # Filename is the version-stripped, sanitized id
    assert p.name == "2401.12345.pdf"


@pytest.mark.asyncio
async def test_arxiv_download_pdf_cache_hit(tmp_path) -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.request_delay_s = 0.0
    cfg.arxiv.pdf_cache_dir = str(tmp_path / "arxiv_pdfs")
    # Pre-populate the cache file
    from pathlib import Path

    cache_dir = Path(cfg.arxiv.pdf_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "2401.12345.pdf").write_bytes(b"%PDF already here")

    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_download_pdf", {"arxiv_id": "2401.12345v3"})
    assert res.error is None
    # Returns the path; version stripped so it reuses the existing 2401.12345.pdf
    assert res.content.endswith("2401.12345.pdf")


@pytest.mark.asyncio
@respx.mock
async def test_arxiv_download_pdf_http_error(tmp_path) -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.request_delay_s = 0.0
    cfg.arxiv.pdf_cache_dir = str(tmp_path / "arxiv_pdfs")
    respx.get("https://arxiv.org/pdf/1234.56789.pdf").mock(return_value=httpx.Response(503))
    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_download_pdf", {"arxiv_id": "1234.56789"})
    assert res.error is not None
    assert "503" in res.error


@pytest.mark.asyncio
async def test_arxiv_download_pdf_disabled_in_config(tmp_path) -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.download_pdfs = False
    cfg.arxiv.request_delay_s = 0.0
    cfg.arxiv.pdf_cache_dir = str(tmp_path / "arxiv_pdfs")
    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_download_pdf", {"arxiv_id": "2401.12345"})
    assert res.error is not None
    assert "download_pdfs" in res.error


@pytest.mark.asyncio
async def test_arxiv_download_pdf_empty_id(tmp_path) -> None:
    cfg = AgentTopConfig()
    cfg.arxiv.request_delay_s = 0.0
    cfg.arxiv.pdf_cache_dir = str(tmp_path / "arxiv_pdfs")
    reg = await build_tool_registry(cfg)
    res = await reg.call("arxiv_download_pdf", {"arxiv_id": ""})
    assert res.error is not None
    assert "arxiv_id" in res.error


# ---------------------------------------------------------------------------
# _strip_version + _safe_download_path helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_strip_version(self) -> None:
        assert arxiv_tool._strip_version("2401.12345") == "2401.12345"
        assert arxiv_tool._strip_version("2401.12345v3") == "2401.12345"
        assert arxiv_tool._strip_version("cs.LG/0702001v10") == "cs.LG/0702001"

    def test_safe_download_path_sanitizes(self, tmp_path) -> None:
        from pathlib import Path

        cache = Path(tmp_path)
        p = arxiv_tool._safe_download_path(cache, "2401.12345")
        assert p.name == "2401.12345.pdf"

        # Slash-containing old-style id is collapsed to a safe filename
        p2 = arxiv_tool._safe_download_path(cache, "cs.LG/0702001")
        assert p2.parent == cache
        assert "/" not in p2.name

        # Empty / weird id doesn't escape cache_dir or crash
        p3 = arxiv_tool._safe_download_path(cache, "../../etc/passwd")
        assert p3.parent == cache
        assert ".." not in p2.name

    def test_result_to_citation(self) -> None:
        r = _FakeResult("2402.54321v4", "My Title", "Long abstract text.", ["A", "B"])
        c = arxiv_tool._result_to_citation(r)
        assert c.url == "https://arxiv.org/abs/2402.54321"  # version stripped
        assert c.title == "My Title"
        assert c.arxiv_id == "2402.54321"
        assert c.authors == ["A", "B"]
        assert c.source_type == "arxiv"
        assert c.discovered_by.value == "arxiv"


# ---------------------------------------------------------------------------
# rate-limit behavior (smoke: two resolves complete; spacing observed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_runs_under_concurrency(monkeypatch) -> None:
    """Two consecutive arxiv_search calls succeed; semaphore+delay logic
    doesn't deadlock. We keep request_delay_s very small so the test is fast."""
    cfg = AgentTopConfig()
    cfg.arxiv.concurrency = 1
    cfg.arxiv.request_delay_s = 0.01
    cfg.arxiv.max_results_per_query = 5

    monkeypatch.setattr(
        arxiv_tool,
        "_sync_search",
        lambda q, n: _citations_from([_FakeResult("2401.1", "T", "S", [])]),
    )
    reg = await build_tool_registry(cfg)
    # Run five concurrent searches — should not deadlock under concurrency=1
    tasks = [reg.call("arxiv_search", {"query": f"q{i}", "max_results": 5}) for i in range(5)]
    results = await asyncio.gather(*tasks)
    assert all(r.error is None for r in results)
    assert len(results) == 5
