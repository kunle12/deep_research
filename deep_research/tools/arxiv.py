"""arxiv tool — arxiv.org search, resolve, and PDF download.

P5: real implementations backed by the `arxiv` PyPI lib (sync; wrapped in
`asyncio.to_thread` + a global `asyncio.Semaphore` for the arXiv 3s rate
limit) + httpx PDF downloads to a disk cache.

Tool schemas:
- arxiv_search(query, max_results) -> list of paper citation summaries
- arxiv_resolve(arxiv_id) -> paper metadata citation
- arxiv_download_pdf(arxiv_id) -> local file path

Quick rate-limit notes:
- arxiv.org requires ≤1 request every 3 seconds. The agent uses a global
  `asyncio.Semaphore(concurrency)` plus a `request_delay_s` spacing between
  consecutive requests on the same Client instance.
- PDFs are downloaded via httpx (separate from the API rate limit) and
  cached to disk keyed by arxiv_id (version stripped, since the latest
  revision supersedes prior versions).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, ToolName

logger = logging.getLogger(__name__)


SEARCH_SCHEMA = {
    "type": "function",
    "description": (
        "Search arxiv.org for papers matching the query. Returns paper "
        "metadata (title, authors, abstract, arxiv_id) for each hit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "arxiv search query"},
            "max_results": {
                "type": "integer",
                "description": "max papers to return (default 10).",
                "default": 10,
            },
        },
        "required": ["query"],
    },
}


RESOLVE_SCHEMA = {
    "type": "function",
    "description": (
        "Resolve an arxiv paper's metadata by id (e.g., 2401.12345 or "
        "2401.12345v3). Returns the title, authors, abstract, and arxiv_id "
        "as a citation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "arxiv_id": {"type": "string", "description": "arxiv id like 2401.12345"},
        },
        "required": ["arxiv_id"],
    },
}


DOWNLOAD_SCHEMA = {
    "type": "function",
    "description": (
        "Download an arxiv paper's PDF by id and return the local file path. "
        "Subsequent PDF analysis tools (pdf_extract_text, pdf_render_pages) "
        "consume this path. Cached on disk keyed by the arxiv id, so repeated "
        "calls for the same id do not re-download."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "arxiv_id": {"type": "string", "description": "arxiv id like 2401.12345"},
        },
        "required": ["arxiv_id"],
    },
}


# Strip the version suffix so 2401.12345v3 and 2401.12345 share a cache slot
# (the latest revision always supersedes prior versions on arxiv).
_VERSION_RX = re.compile(r"v\d+$")


def _strip_version(arxiv_id: str) -> str:
    return _VERSION_RX.sub("", arxiv_id)


def _safe_download_path(cache_dir: Path, arxiv_id: str) -> Path:
    """Return a safe filesystem path for the cached PDF of this arxiv_id.

    Strips the version + any path separators / '..' that could escape cache_dir.
    """
    base = _strip_version(arxiv_id).strip().replace("/", "_")
    # Defang anything that's not safe on the local filesystem
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "unknown"
    return cache_dir / f"{base}.pdf"


def _result_to_citation(result: Any) -> Citation:
    """Coerce an `arxiv.Result` into a normalized `Citation`."""
    short_id = result.get_short_id()  # e.g. "2401.12345v3"
    base_id = _strip_version(short_id)
    authors = [a.name for a in getattr(result, "authors", []) or []]
    return Citation(
        url=f"https://arxiv.org/abs/{base_id}",
        title=getattr(result, "title", "") or "",
        snippet=(getattr(result, "summary", "") or "")[:1000],
        source_type="arxiv",
        arxiv_id=base_id,
        authors=authors,
        confidence_score=0.9,  # arxiv metadata is human-curated
        discovered_by=ToolName.arxiv,
    )


def _sync_search(query: str, max_results: int) -> list[Citation]:
    """Synchronous arxiv search via the `arxiv` library."""
    import arxiv

    client = arxiv.Client()
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.Relevance,
    )
    out: list[Citation] = []
    try:
        for result in client.results(search):
            out.append(_result_to_citation(result))
    except Exception as e:
        # The arxiv lib raises on transport errors / empty queries.
        # We log here and surface an empty list; the caller's ToolResult will
        # carry whatever we got.
        logger.warning("arxiv_search query=%r failed: %s: %s", query, type(e).__name__, e)
    return out


def _sync_resolve(arxiv_id: str) -> Citation | None:
    """Synchronous arxiv resolve via the `arxiv` library."""
    import arxiv

    client = arxiv.Client()
    search = arxiv.Search(id_list=[arxiv_id], max_results=1)
    try:
        result = next(client.results(search))
    except StopIteration:
        logger.info("arxiv_resolve id=%r -> no results", arxiv_id)
        return None
    except Exception as e:
        logger.warning("arxiv_resolve id=%r failed: %s: %s", arxiv_id, type(e).__name__, e)
        return None
    return _result_to_citation(result)


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    cfg = config.arxiv
    cache_dir = Path(cfg.pdf_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Global semaphore so the agent never exceeds `concurrency` simultaneous
    # arxiv API calls (per the arXiv 3s/call rate limit). Uses a single lock
    # for ALL tool names since arxiv's rate limit is per-API-key, not per-operation.
    api_sem = asyncio.Semaphore(cfg.concurrency)
    _global_lock = asyncio.Lock()
    _last_call_time: float = 0.0

    async def _rate_limited(fn: Any, *args: Any) -> Any:
        """Wrap a sync arxiv-library call with concurrency semaphore and
        a global spacing delay of `request_delay_s` seconds between calls.

        Lock is held only to atomically read-and-project _last_call_time;
        the actual sleep happens outside the lock so concurrent callers under
        the semaphore can queue up and get their own projected delay.
        """
        nonlocal _last_call_time
        async with api_sem:
            import time

            delay = 0.0
            async with _global_lock:
                now = time.monotonic()
                elapsed = now - _last_call_time
                if elapsed < cfg.request_delay_s:
                    delay = cfg.request_delay_s - elapsed
                # Project _last_call_time forward so concurrent callers
                # see the occupied slot and compute their own delay.
                _last_call_time = time.monotonic() + delay
            # Lock released — allow other semaphore-acquired callers to
            # enter the timing check while this one sleeps.
            if delay > 0:
                await asyncio.sleep(delay)
            result = await asyncio.to_thread(fn, *args)
            return result

    async def _search(query: str, max_results: int = 10, **_: Any) -> ToolResult:
        max_results = min(max_results, cfg.max_results_per_query)
        citations = await _rate_limited(_sync_search, query, max_results)
        if not citations:
            return ToolResult(
                content=f"No arxiv search results for query: {query!r}",
                citations=[],
            )
        logger.info("arxiv_search query=%r -> %d results", query, len(citations))
        content_lines = [
            f"{i}. {c.title}\n   arxiv_id: {c.arxiv_id}\n   {c.snippet[:300]}"
            for i, c in enumerate(citations, start=1)
        ]
        return ToolResult(content="\n\n".join(content_lines), citations=citations)

    async def _resolve(arxiv_id: str, **_: Any) -> ToolResult:
        arxiv_id = arxiv_id.strip()
        if not arxiv_id:
            return ToolResult(content="", error="arxiv_resolve requires non-empty arxiv_id")
        cit = await _rate_limited(_sync_resolve, arxiv_id)
        if cit is None:
            return ToolResult(
                content=f"No arxiv result for id {arxiv_id!r}",
                error=f"arxiv id not found: {arxiv_id}",
            )
        return ToolResult(
            content=f"Resolved: {cit.title}\narxiv_id: {cit.arxiv_id}\nAuthors: {', '.join(cit.authors)}\n\nAbstract:\n{cit.snippet}",
            citations=[cit],
        )

    async def _download(arxiv_id: str, **_: Any) -> ToolResult:
        if not cfg.download_pdfs:
            return ToolResult(
                content="",
                error="arxiv.download_pdfs is false in config; refusing to download",
            )
        arxiv_id = arxiv_id.strip()
        if not arxiv_id:
            return ToolResult(content="", error="arxiv_download_pdf requires arxiv_id")
        target = _safe_download_path(cache_dir, arxiv_id)
        if target.exists() and target.stat().st_size > 1024:
            logger.info("arxiv_download_pdf cache hit: %s -> %s", arxiv_id, target)
            return ToolResult(content=str(target))
        # Build canonical arxiv PDF URL
        # (current lib exposes result.pdf_url, but for download we can just use
        # the well-known pattern. Use the version-less id to fetch the latest.)
        url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        try:
            async with httpx.AsyncClient(
                timeout=120.0, follow_redirects=True, max_redirects=3
            ) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "DeepResearchBot/0.1 (+arxiv pdf downloader)"},
                )
                if resp.status_code >= 400:
                    return ToolResult(
                        content="",
                        error=f"arxiv download HTTP {resp.status_code} for {url}",
                    )
                # Quick content-type sanity check
                ctype = (resp.headers.get("content-type") or "").lower()
                if "pdf" not in ctype and "octet-stream" not in ctype:
                    return ToolResult(
                        content="",
                        error=f"unexpected content-type {ctype!r} for {url}",
                    )
                target.write_bytes(resp.content)
        except httpx.HTTPError as e:
            return ToolResult(
                content="",
                error=f"arxiv download failed: {type(e).__name__}: {e}",
            )
        except OSError as e:
            return ToolResult(
                content="",
                error=f"write to cache failed: {type(e).__name__}: {e}",
            )
        logger.info("arxiv_download_pdf %s -> %s (%d bytes)", arxiv_id, target, target.stat().st_size)
        return ToolResult(content=str(target))

    reg.register("arxiv_search", _search, SEARCH_SCHEMA)
    reg.register("arxiv_resolve", _resolve, RESOLVE_SCHEMA)
    reg.register("arxiv_download_pdf", _download, DOWNLOAD_SCHEMA)


__all__ = ["register"]
