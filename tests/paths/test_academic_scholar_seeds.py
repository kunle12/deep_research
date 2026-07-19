"""Integration tests for Scholar seed gathering in the academic path.

Phase 6: verifies that _gather_seeds correctly:
- Dispatches both arxiv and scholar when seed_backends includes "scholar"
- Dedups scholar hits that overlap arxiv by arxiv_id
- Handles scholar-only (non-arxiv) hits as synthetic PaperNodes
- Respects backward-compat: default seed_backends ["arxiv"] never calls scholar
"""

from __future__ import annotations

from typing import Any

import pytest

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.paths.academic import _gather_seeds
from deep_research.state import Citation

# Reuse helpers from test_paths_academic
from tests.test_paths_academic import _cfg as _base_cfg
from tests.test_paths_academic import _classified


def _cfg(**overrides: Any) -> AgentTopConfig:
    """Extend _base_cfg to allow scholar-related overrides."""
    cfg = _base_cfg(**{k: v for k, v in overrides.items() if k != "seed_backends"})
    # seed_backends is not handled by _base_cfg; set it explicitly
    cfg.academic.seed_backends = overrides.get("seed_backends", ["arxiv"])
    cfg.scholar.enabled = overrides.get("scholar_enabled", True)
    return cfg


def _arxiv_citation(aid: str, title: str = "") -> Citation:
    return Citation(
        url=f"https://arxiv.org/abs/{aid}",
        title=title or f"Paper {aid}",
        arxiv_id=aid,
        source_type="arxiv",
        discovered_by=None,
    )


def _scholar_citation(
    title: str = "",
    url: str = "https://example.com/paper",
    arxiv_id: str | None = None,
    pdf_url: str | None = None,
) -> Citation:
    return Citation(
        url=url,
        title=title or url,
        snippet="A paper abstract",
        source_type="scholar",
        discovered_by=None,
        arxiv_id=arxiv_id,
        pdf_url=pdf_url,
    )


def _tools(
    arxiv_citations: list[Citation],
    scholar_citations: list[Citation] | None = None,
) -> ToolRegistry:
    """Build a ToolRegistry with stubbed arxiv_search and optional scholar_search."""
    reg = ToolRegistry()

    async def _arxiv_search(query: str, max_results: int = 10, **_: Any) -> ToolResult:
        return ToolResult(content="arxiv searched", citations=arxiv_citations)

    reg.register("arxiv_search", _arxiv_search, {"type": "function", "name": "arxiv_search"})

    if scholar_citations is not None:
        async def _scholar_search(query: str, max_results: int = 10, **_: Any) -> ToolResult:
            return ToolResult(content="scholar searched", citations=scholar_citations)

        reg.register("scholar_search", _scholar_search, {"type": "function", "name": "scholar_search"})

    return reg


# ---------------------------------------------------------------------------
# Backward compat — default seed_backends ["arxiv"]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_seed_backends_arxiv_only() -> None:
    """Default seed_backends=["arxiv"]: scholar_search is never called."""
    cfg = _cfg(seed_backends=["arxiv"])
    classified = _classified(search_hint="test")
    arxiv_cits = [_arxiv_citation("2401.1"), _arxiv_citation("2401.2")]
    # Scholar tool NOT registered — backward compat
    reg = _tools(arxiv_cits, scholar_citations=None)

    seeds_cits: list[Citation] = []
    nodes = await _gather_seeds(classified, "test query", reg, cfg, seeds_cits)
    assert len(nodes) == 2
    assert nodes[0].arxiv_id == "2401.1"
    assert nodes[1].arxiv_id == "2401.2"
    assert len(seeds_cits) == 2


@pytest.mark.asyncio
async def test_backward_compat_no_scholar_tool() -> None:
    """Even with seed_backends=["arxiv","scholar"], no crash when scholar tool missing."""
    cfg = _cfg(seed_backends=["arxiv", "scholar"])
    classified = _classified(search_hint="test")
    arxiv_cits = [_arxiv_citation("2401.1")]
    # Scholar tool NOT registered
    reg = _tools(arxiv_cits, scholar_citations=None)

    seeds_cits: list[Citation] = []
    nodes = await _gather_seeds(classified, "test query", reg, cfg, seeds_cits)
    assert len(nodes) == 1
    assert nodes[0].arxiv_id == "2401.1"


# ---------------------------------------------------------------------------
# Scholar enabled — parallel dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_arxiv_and_scholar() -> None:
    """Both backends called; seeds are the union (deduped by arxiv_id)."""
    cfg = _cfg(seed_backends=["arxiv", "scholar"])
    classified = _classified(search_hint="test")

    arxiv_cits = [
        _arxiv_citation("2401.1", "Arxiv Paper A"),
        _arxiv_citation("2401.2", "Arxiv Paper B"),
    ]
    scholar_cits = [
        _scholar_citation("Scholar Paper C", url="https://example.com/c"),
        # Overlap with arxiv — same arxiv_id
        _scholar_citation("Scholar Paper A", url="https://arxiv.org/abs/2401.1", arxiv_id="2401.1"),
    ]
    reg = _tools(arxiv_cits, scholar_cits)

    seeds_cits: list[Citation] = []
    nodes = await _gather_seeds(classified, "test query", reg, cfg, seeds_cits)
    # 2 arxiv seeds + 1 non-overlap scholar seed = 3 total
    assert len(nodes) == 3

    # Check dedup: 2401.1 appears only once
    arxiv_ids = [n.arxiv_id for n in nodes]
    assert arxiv_ids.count("2401.1") == 1  # deduped

    # Scholar-only node has synthetic id
    scholar_only = [n for n in nodes if n.arxiv_id.startswith("scholar:")]
    assert len(scholar_only) == 1
    assert scholar_only[0].title == "Scholar Paper C"

    # seeds_citations includes both arxiv and scholar citations (deduped by URL)
    assert len(seeds_cits) == 3  # arxiv 2401.1, 2401.2 + scholar C
    scholar_c_urls = [c.url for c in seeds_cits if c.source_type == "scholar"]
    assert len(scholar_c_urls) == 1


@pytest.mark.asyncio
async def test_scholar_only_hits_no_arxiv() -> None:
    """Scholar-only hits (no arxiv overlap) become synthetic PaperNodes."""
    cfg = _cfg(seed_backends=["arxiv", "scholar"])
    classified = _classified(search_hint="test")

    arxiv_cits = []  # no arxiv results
    scholar_cits = [
        _scholar_citation("Nature Paper", url="https://nature.com/articles/xxx"),
        _scholar_citation("ACM Paper", url="https://dl.acm.org/doi/yyy"),
    ]
    reg = _tools(arxiv_cits, scholar_cits)

    seeds_cits: list[Citation] = []
    nodes = await _gather_seeds(classified, "test query", reg, cfg, seeds_cits)
    assert len(nodes) == 2
    for n in nodes:
        assert n.arxiv_id.startswith("scholar:")
        assert n.rationale == "scholar search hit"
    assert len(seeds_cits) == 2


@pytest.mark.asyncio
async def test_scholar_paywall_abstract_only() -> None:
    """Scholar hit with no pdf_url becomes abstract-only leaf node."""
    cfg = _cfg(seed_backends=["arxiv", "scholar"])
    classified = _classified(search_hint="test")

    arxiv_cits = []
    # Hit has no pdf_url and no arxiv_id — paywalled / abstract-only
    scholar_cits = [
        _scholar_citation(
            "Paywalled Paper",
            url="https://www.jstor.org/stable/xxx",
            arxiv_id=None,
            pdf_url=None,
        ),
    ]
    reg = _tools(arxiv_cits, scholar_cits)

    seeds_cits: list[Citation] = []
    nodes = await _gather_seeds(classified, "test query", reg, cfg, seeds_cits)
    assert len(nodes) == 1
    n = nodes[0]
    assert n.arxiv_id.startswith("scholar:")
    assert n.abstract  # snippet used as abstract
    assert len(seeds_cits) == 1


# ---------------------------------------------------------------------------
# Cost guardrail — skip_if_arxiv_hits_ge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_scholar_when_arxiv_enough_hits() -> None:
    """When arxiv seeds already >= skip_if_arxiv_hits_ge, scholar is skipped."""
    cfg = _cfg(seed_backends=["arxiv", "scholar"])
    cfg.scholar.skip_if_arxiv_hits_ge = 2
    classified = _classified(search_hint="test")

    arxiv_cits = [
        _arxiv_citation("2401.1"),
        _arxiv_citation("2401.2"),
        _arxiv_citation("2401.3"),
    ]
    # Scholar tool registered but should not be called
    reg = _tools(arxiv_cits, [])

    seeds_cits: list[Citation] = []
    nodes = await _gather_seeds(classified, "test query", reg, cfg, seeds_cits)
    assert len(nodes) == 3
    # Only arxiv seeds
    for n in nodes:
        assert n.rationale == "arxiv search hit"
