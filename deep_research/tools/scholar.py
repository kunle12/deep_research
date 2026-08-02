"""scholar tool — Google Scholar discovery backend.

Phase 2 of the Scholar integration (see docs/PLAN_SCHOLAR.md).

Backend choice:
- Primary: Serper API (`https://google.serper.dev/scholar`) — paid, JSON,
  no scraping risk. Returns title/authors/year/cited_by/url/pdf side-link.
- Fallback: SearXNG with the `scholar` engine enabled — free, opt-in via the
  instance settings.

Tool schema:
- scholar_search(query, max_results) -> list of academic-paper citations

Dedup:
- Hits resolving to an arxiv ID (DOI `10.48550/arXiv.<id>` or URL on
  arxiv.org) carry `arxiv_id` so the academic-path seed gatherer can fold
  them into the arxiv-seed set without double-counting.

Rate limit:
- Serper cheapest tier is ~1 rps. A global `asyncio.Semaphore(concurrency)`
  plus a `request_delay_s` spacing delay between calls, mirroring the arxiv
  tool's pattern.

Paywall ethics:
- Only the free `[PDF]` side link surfaced by Scholar is fetched. This tool
  never attempts to bypass paywalls.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, ToolName
from deep_research.util import strip_arxiv_version as _strip_version

logger = logging.getLogger(__name__)


SEARCH_SCHEMA = {
    "type": "function",
    "description": (
        "Search Google Scholar (via Serper) for papers matching the query. "
        "Returns paper metadata — title, authors, year, citation count, "
        "abstract snippet, and a free-PDF link when Scholar surfaces one. "
        "Covers non-arxiv venues (Nature, NEJM, ACM, IEEE, conference "
        "proceedings) that arxiv_search misses."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Scholar search query"},
            "max_results": {
                "type": "integer",
                "description": "max papers to return (default 10).",
                "default": 10,
            },
        },
        "required": ["query"],
    },
}


# Arxiv ID detection — strip version suffix + extract from URL or DOI.
# `_strip_version` is an alias for the shared `deep_research.util` helper.
_ARXIV_ID_RX = re.compile(r"(\d{4}\.\d{4,5})")
_ARXIV_URL_RX = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?")
_ARXIV_DOI_RX = re.compile(r"10\.48550/arXiv\.([0-9]{4}\.[0-9]{4,5})", re.IGNORECASE)


def _infer_arxiv_id(url: str | None, doi: str | None, title: str | None) -> str | None:
    """Best-effort: extract an arxiv ID from a Scholar hit's URL / DOI.

    Used for arxiv ↔ scholar dedup in the academic seed gatherer.
    Returns a version-stripped arxiv ID (e.g. '2401.12345') or None.
    """
    if doi:
        m = _ARXIV_DOI_RX.search(doi)
        if m:
            return m.group(1)
    if url:
        m = _ARXIV_URL_RX.search(url)
        if m:
            return _strip_version(m.group(1))
        # Sometimes the url itself contains `2401.12345` as a path fragment.
        m2 = _ARXIV_ID_RX.search(url)
        if m2 and "arxiv" in url.lower():
            return _strip_version(m2.group(1))
    return None


def _parse_year(s: Any) -> int | None:
    if s is None:
        return None
    if isinstance(s, int):
        return s
    if isinstance(s, str):
        s = s.strip()
        if s.isdigit() and 1900 < int(s) < 2100:
            return int(s)
        m = re.search(r"\b(19|20)\d{2}\b", s)
        if m:
            return int(m.group(0))
    return None


def _confidence(cited_by: int | None) -> float:
    """Map cited_by count to a [0.6, 0.95] confidence band."""
    if cited_by is None or cited_by <= 0:
        return 0.6
    return min(0.6 + cited_by / 1000.0, 0.95)


# ---------------------------------------------------------------------------
# Serper primary path
# ---------------------------------------------------------------------------


def _serper_request_body(
    query: str, max_results: int, year_from: int | None, year_to: int | None
) -> dict[str, Any]:
    body: dict[str, Any] = {"q": query, "num": max_results}
    # Serper accepts Google `tbs` parameter for date windows.
    # Format: "cdr:1,cd_min:yYYYY,cd_max:yYYYY" for low+high year bounds.
    # Simpler: Google Scholar uses "as_ylo"/"as_yhi" via URL params; Serper
    # forwards `tbs` verbatim to scholar. We construct the canonical form.
    if year_from is not None or year_to is not None:
        lo = year_from if year_from is not None else 1900
        hi = year_to if year_to is not None else 2100
        body["tbs"] = f"cdr:1,cd_min:y{lo},cd_max:y{hi}"
    return body


def _serper_hit_to_citation(hit: dict[str, Any]) -> Citation:
    """Coerce a Serper /scholar organic hit into a normalized Citation.

    Defensive: tolerate missing keys; Serper's scholar schema has occasional
    gaps (some hits omit `pdf`, `publication`, `cited_by`).
    """
    link = hit.get("link") or hit.get("url") or ""
    pdf_link = hit.get("pdf") or None
    doi = hit.get("doi") or None
    title = (hit.get("title") or "").strip()
    snippet = (hit.get("snippet") or "").strip()
    authors_raw = hit.get("authors") or ""
    if isinstance(authors_raw, str):
        authors = [a.strip() for a in authors_raw.split(",") if a.strip()]
    elif isinstance(authors_raw, list):
        authors = [str(a).strip() for a in authors_raw if a]
    else:
        authors = []
    year = _parse_year(hit.get("year"))
    venue = hit.get("publication") or None
    cited_by_count_raw = hit.get("cited_by") or hit.get("citedBy")
    cited_by_count = int(cited_by_count_raw) if isinstance(cited_by_count_raw, int) else None

    arxiv_id = _infer_arxiv_id(link, doi, title)

    return Citation(
        url=link,
        title=title,
        snippet=snippet,
        source_type="scholar",
        discovered_by=ToolName.scholar,
        arxiv_id=arxiv_id,
        authors=authors,
        pdf_url=pdf_link,
        doi=doi,
        year=year,
        venue=venue,
        cited_by_count=cited_by_count,
        confidence_score=_confidence(cited_by_count),
    )


async def _serper_search(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    query: str,
    max_results: int,
    year_from: int | None,
    year_to: int | None,
) -> list[Citation]:
    body = _serper_request_body(query, max_results, year_from, year_to)
    resp = await client.post(
        endpoint,
        json=body,
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
            "User-Agent": "DeepResearchBot/0.1 (+scholar search)",
        },
    )
    if resp.status_code >= 400:
        # 429 rate-limit / 5xx transient — let caller decide retry
        raise httpx.HTTPStatusError(
            f"serper scholar HTTP {resp.status_code}", request=resp.request, response=resp
        )
    data = resp.json() or {}
    if isinstance(data, dict):
        hits = data.get("organic") or data.get("results") or data.get("scholar") or []
    else:
        hits = []
    return [_serper_hit_to_citation(h) for h in hits if isinstance(h, dict)]


# ---------------------------------------------------------------------------
# SearXNG fallback
# ---------------------------------------------------------------------------


async def _searxng_search(
    client: httpx.AsyncClient,
    url: str,
    query: str,
    max_results: int,
    year_from: int | None,
    year_to: int | None,
) -> list[Citation]:
    params: dict[str, Any] = {
        "q": query,
        "categories": "scholar",
        "format": "json",
    }
    if year_from is not None or year_to is not None:
        # SearXNG `time_range` is a string ({day,week,month,year}); no precise
        # year-window support. Map our window to the longest range that fits.
        params["time_range"] = "year"  # best-effort; SearXNG ignores year_from/to
    resp = await client.get(
        url,
        params=params,
        headers={"User-Agent": "DeepResearchBot/0.1 (+scholar search)"},
    )
    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"searxng scholar HTTP {resp.status_code}", request=resp.request, response=resp
        )
    data = resp.json() or {}
    hits = data.get("results") if isinstance(data, dict) else []
    citations: list[Citation] = []
    for h in hits[:max_results]:
        if not isinstance(h, dict):
            continue
        link = h.get("url") or ""
        title = (h.get("title") or "").strip()
        snippet = (h.get("content") or "").strip()
        doi = h.get("doi") or None
        # SearXNG scholar results don't expose cited_by consistently; leave None.
        arxiv_id = _infer_arxiv_id(link, doi, title)
        citations.append(
            Citation(
                url=link,
                title=title,
                snippet=snippet,
                source_type="scholar",
                discovered_by=ToolName.scholar,
                arxiv_id=arxiv_id,
                doi=doi,
                confidence_score=0.6,
            )
        )
    return citations


# ---------------------------------------------------------------------------
# Rate-limit helper
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Per-backend rate-limit semaphore + inter-call spacing delay."""

    def __init__(self, concurrency: int, request_delay_s: float) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._last_call: list[float] = [0.0]
        self._request_delay_s = request_delay_s

    async def __call__(self, coro_factory, *args):
        async with self._sem:
            delay = 0.0
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_call[0]
                if elapsed < self._request_delay_s:
                    delay = self._request_delay_s - elapsed
                self._last_call[0] = time.monotonic() + delay
            if delay > 0:
                await asyncio.sleep(delay)
            return await coro_factory(*args)


async def _backoff_retry(rate_limiter, coro_factory, *args, retries: int = 1):
    """Single retry on 429/5xx with exponential backoff (1s, 2s)."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await rate_limiter(coro_factory, *args)
        except httpx.HTTPStatusError as e:
            last_exc = e
            status = e.response.status_code if e.response is not None else 0
            if status not in {429, 500, 502, 503, 504} or attempt >= retries:
                raise
            backoff = 2**attempt
            logger.warning("scholar search HTTP %d, backing off %ds", status, backoff)
            await asyncio.sleep(backoff)
        except httpx.HTTPError as e:
            last_exc = e
            if attempt >= retries:
                raise
            await asyncio.sleep(2**attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("scholar search retry loop exited unexpectedly")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    cfg = config.scholar
    if not cfg.enabled:
        logger.info("scholar disabled — not registering tools")
        return

    serper_limiter = _RateLimiter(cfg.concurrency, cfg.request_delay_s)
    searxng_limiter = _RateLimiter(cfg.concurrency, cfg.request_delay_s)

    # Proactive Serper quota tracking
    _serper_call_count: int = 0
    _serper_call_lock = asyncio.Lock()
    _serper_max_calls = cfg.serper.max_calls_per_session

    async def _search(query: str, max_results: int = 10, **_: Any) -> ToolResult:
        nonlocal _serper_call_count
        max_results = min(max_results, cfg.max_results_per_query)
        if not query.strip():
            return ToolResult(content="", error="scholar_search requires non-empty query")

        primary_key = cfg.resolve_serper_key()

        async def _primary():
            async with httpx.AsyncClient(timeout=cfg.serper.timeout_s) as client:
                return await _serper_search(
                    client,
                    cfg.serper.endpoint,
                    primary_key or "",
                    query,
                    max_results,
                    cfg.year_from,
                    cfg.year_to,
                )

        async def _fallback():
            async with httpx.AsyncClient(timeout=cfg.searxng.timeout_s) as client:
                return await _searxng_search(
                    client,
                    cfg.searxng.url,
                    query,
                    max_results,
                    cfg.year_from,
                    cfg.year_to,
                )

        # Pick ordered backends
        ordered: list[tuple[str, Any]] = []
        if cfg.primary == "serper" and primary_key:
            ordered.append(("serper", _primary))
        elif cfg.primary == "searxng":
            ordered.append(("searxng", _fallback))
        for fb in cfg.fallback_chain:
            if fb == "serper" and primary_key and ("serper", _primary) not in ordered:
                ordered.append(("serper", _primary))
            elif fb == "searxng" and ("searxng", _fallback) not in ordered:
                ordered.append(("searxng", _fallback))

        if not ordered:
            return ToolResult(
                content="",
                error="scholar.enabled but no usable backend: set SERPER_API_KEY "
                "or run a SearXNG instance with the `scholar` engine enabled.",
            )

        citations: list[Citation] = []
        last_err: str = ""
        for name, factory in ordered:
            try:
                if name == "serper":
                    async with _serper_call_lock:
                        if (
                            _serper_max_calls is not None
                            and _serper_call_count >= _serper_max_calls
                        ):
                            logger.info(
                                "Serper call quota exhausted (%d >= %d), falling back",
                                _serper_call_count,
                                _serper_max_calls,
                            )
                            continue
                        _serper_call_count += 1
                rate_limiter = serper_limiter if name == "serper" else searxng_limiter
                try:
                    citations = await _backoff_retry(rate_limiter, factory)
                except Exception:
                    # Only charge quota for calls that actually executed.
                    if name == "serper":
                        async with _serper_call_lock:
                            _serper_call_count -= 1
                    raise
                if citations:
                    logger.info(
                        "scholar_search %s -> %d results (via %s)",
                        repr(query),
                        len(citations),
                        name,
                    )
                    break
                # Backend returned 0 hits (no error) — fall through to the next
                # backend in the chain rather than giving up immediately.
                logger.info("scholar_search %s -> 0 hits via %s (trying next backend)", repr(query), name)
                continue
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning("scholar_search backend %s failed: %s", name, last_err)

        if not citations:
            note = f"No scholar results for query: {query!r}"
            err: str | None = None
            if last_err:
                note += f" (last backend error: {last_err})"
                err = last_err
            return ToolResult(content=note, error=err, citations=[])

        if not cfg.include_pdf_links:
            for c in citations:
                c.pdf_url = None

        lines = [
            f"{i}. {c.title or '(untitled)'}\n   {c.url}\n   "
            f"authors: {', '.join(c.authors) or 'N/A'}  "
            f"year: {c.year or 'N/A'}  cited_by: {c.cited_by_count or 'N/A'}"
            + (f"\n   pdf: {c.pdf_url}" if c.pdf_url else "")
            + (f"\n   snippet: {c.snippet[:300]}" if c.snippet else "")
            for i, c in enumerate(citations, start=1)
        ]
        return ToolResult(content="\n\n".join(lines), citations=citations)

    reg.register("scholar_search", _search, SEARCH_SCHEMA)


__all__ = ["SEARCH_SCHEMA", "_infer_arxiv_id", "register"]
