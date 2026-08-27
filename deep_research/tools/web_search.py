"""web_search tool — Tavily (primary), Firecrawl (second), SearXNG (last).

Returns a list of normalized `Citation` objects alongside the human-readable
result text. Retries Tavily/Firecrawl on rate-limit errors with exponential
backoff before seamlessly falling back to the next backend in the chain.
Supports proactive quota-based fallback via ``max_calls_per_session``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from tavily import AsyncTavilyClient
from tavily.errors import (
    TavilyKeylessLimitError,
    TimeoutError,
    UsageLimitExceededError,
)

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, ToolName
from deep_research.util import coerce_float

logger = logging.getLogger(__name__)


SCHEMA = {
    "type": "function",
    "description": (
        "Search the web. Returns up to `max_results` results, each with a URL, "
        "title, and a content snippet. Use this for general queries, news, "
        "blog posts. Falls back through the configured backend chain "
        "(Tavily -> Firecrawl -> SearXNG) when a backend is unavailable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query string"},
            "max_results": {
                "type": "integer",
                "description": "Max results to return (default 10).",
                "default": 10,
            },
        },
        "required": ["query"],
    },
}


async def _tavily_search(
    query: str, max_results: int, api_key: str, search_depth: str
) -> list[Citation]:
    """Call Tavily. Returns normalized Citation objects."""
    client = AsyncTavilyClient(api_key=api_key)
    # The SDK exposes both async `search` and `async_search`; prefer async.
    response = await client.search(
        query=query,
        max_results=max_results,
        search_depth=search_depth,
        include_answer=False,  # we want raw results; our LLM synthesizes the answer
        timeout=30.0,
    )
    # Response shape: {"results": [{"url","title","content","score"}], "answer": "..."}
    results = response.get("results") if isinstance(response, dict) else []
    citations: list[Citation] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        citations.append(
            Citation(
                url=r.get("url", ""),
                title=r.get("title") or "",
                snippet=r.get("content") or "",
                source_type="web",
                confidence_score=coerce_float(r.get("score"), 0.5),
                discovered_by=ToolName.web_search,
            )
        )
    return citations


async def _tavily_with_retry(
    query: str,
    max_results: int,
    api_key: str,
    search_depth: str,
    retries: int,
) -> list[Citation]:
    """Call Tavily with exponential-backoff retry on rate-limit errors.

    Retries only on ``UsageLimitExceededError`` / ``TavilyKeylessLimitError`` /
    ``TimeoutError`` — other errors (bad key, forbidden) propagate immediately
    so the caller can fall through to SearXNG.
    """
    for attempt in range(retries + 1):
        try:
            return await _tavily_search(query, max_results, api_key, search_depth)
        except TavilyKeylessLimitError as e:
            retry_after = e.retry_after_seconds or 2**attempt
            logger.warning(
                "Tavily keyless limit hit (attempt %d/%d), retrying after %ds",
                attempt + 1,
                retries + 1,
                retry_after,
            )
            if attempt >= retries:
                raise  # exhausted — propagate the real error
            await asyncio.sleep(retry_after)
        except UsageLimitExceededError:
            backoff = 2**attempt
            logger.warning(
                "Tavily rate limit (attempt %d/%d), backing off %ds",
                attempt + 1,
                retries + 1,
                backoff,
            )
            if attempt >= retries:
                raise  # exhausted — propagate the real error
            await asyncio.sleep(backoff)
        except TimeoutError:
            backoff = 2**attempt
            logger.warning(
                "Tavily timeout (attempt %d/%d), backing off %ds",
                attempt + 1,
                retries + 1,
                backoff,
            )
            if attempt >= retries:
                raise  # exhausted — propagate the real error
            await asyncio.sleep(backoff)
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            if attempt < retries:
                await asyncio.sleep(2**attempt)
                continue
            raise  # Exhausted retries — let caller fall through
        except Exception:
            # Non-recoverable Tavily errors (bad key, forbidden, bad request) —
            # don't retry, let caller fall through to SearXNG.
            raise
    raise RuntimeError("Tavily retry loop exited unexpectedly")


async def _firecrawl_search(
    query: str,
    max_results: int,
    api_key: str,
    endpoint: str,
    timeout_s: float,
    retries: int,
) -> list[Citation]:
    """Call Firecrawl's /v2/search endpoint. Returns normalized Citations.

    Retries with exponential backoff on 429 (rate limit), 408 (timeout), 5xx
    and transport errors (connect/read/protocol); other statuses (bad key,
    bad request) raise immediately so the caller can fall through to the
    next backend.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"query": query, "limit": max_results}
    data: Any = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                if resp.status_code in (408, 429) or resp.status_code >= 500:
                    if attempt < retries:
                        backoff = 2**attempt
                        logger.warning(
                            "Firecrawl HTTP %d (attempt %d/%d), backing off %ds",
                            resp.status_code,
                            attempt + 1,
                            retries + 1,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    resp.raise_for_status()
                resp.raise_for_status()
                data = resp.json()
            break
        except httpx.TransportError:
            if attempt < retries:
                backoff = 2**attempt
                logger.warning(
                    "Firecrawl timeout (attempt %d/%d), backing off %ds",
                    attempt + 1,
                    retries + 1,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue
            raise
    if not isinstance(data, dict) or not data.get("success", False):
        raise RuntimeError(f"Firecrawl search failed: {data if isinstance(data, dict) else 'non-dict response'}")
    web = data.get("data", {}).get("web", []) if isinstance(data.get("data"), dict) else []
    citations: list[Citation] = []
    for r in web:
        if not isinstance(r, dict):
            continue
        citations.append(
            Citation(
                url=r.get("url", ""),
                title=r.get("title") or "",
                snippet=r.get("description") or "",
                source_type="web",
                # Firecrawl returns no relevance score; uniform confidence
                confidence_score=0.5,
                discovered_by=ToolName.web_search,
            )
        )
    return citations


async def _searxng_search(
    query: str,
    max_results: int,
    searxng_url: str,
    user_agent: str,
) -> list[Citation]:
    """Call SearXNG's JSON API. Used as fallback when Tavily fails."""
    params = {"q": query, "format": "json"}
    # Local SearXNG instances often run the botdetection limiter, which 429s
    # requests that look like bots: a `python-httpx` UA or a missing
    # `Accept-Language` header are both flagged. Send a real browser UA and an
    # Accept-Language so the request is treated as a normal client.
    headers = {
        "User-Agent": user_agent,
        "Accept-Language": "en-US,en;q=0.5",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(searxng_url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results") if isinstance(data, dict) else []
    citations: list[Citation] = []
    for r in results[:max_results]:
        citations.append(
            Citation(
                url=r.get("url", "") if isinstance(r, dict) else "",
                title=r.get("title", "") if isinstance(r, dict) else "",
                snippet=r.get("content", "") if isinstance(r, dict) else "",
                source_type="web",
                # SearXNG doesn't return a score; assign uniform confidence
                confidence_score=0.5,
                discovered_by=ToolName.web_search,
            )
        )
    return citations


def _format_for_llm(citations: list[Citation]) -> str:
    """Render the citations list as a human/LLM-readable text chunk."""
    if not citations:
        return "No web search results found."
    lines: list[str] = []
    for i, c in enumerate(citations, start=1):
        title = c.title or "(no title)"
        snippet = c.snippet.strip()[:500]
        lines.append(f"{i}. {title}\n   URL: {c.url}\n   Snippet: {snippet}")
    return "\n\n".join(lines)


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    cfg = config.search

    # Resolve the ordered list of backends to try (primary + fallback_chain, deduped)
    backends: list[str] = []
    if cfg.primary:
        backends.append(cfg.primary)
    for b in cfg.fallback_chain:
        if b not in backends:
            backends.append(b)

    tavily_key = cfg.resolve_tavily_key()
    if "tavily" in backends and not tavily_key:
        logger.warning(
            "Tavily selected as web_search backend but %s is unset; "
            "will skip to the next backend if available.",
            cfg.tavily.api_key_env,
        )
        backends = [b for b in backends if b != "tavily"]

    firecrawl_key = cfg.resolve_firecrawl_key()
    if "firecrawl" in backends and not firecrawl_key:
        logger.warning(
            "Firecrawl selected as web_search backend but %s is unset; "
            "will skip to the next backend if available.",
            cfg.firecrawl.api_key_env,
        )
        backends = [b for b in backends if b != "firecrawl"]

    # Proactive quota guard: atomic counter with Lock.
    # When max_calls_per_session is None, unlimited calls are allowed.
    # A Lock is needed because we must atomically check-and-increment — if
    # quota is exhausted we skip the backend (not block), so Semaphore won't work.
    tavily_max_calls = cfg.tavily.max_calls_per_session
    _tavily_call_count: int = 0
    _tavily_call_lock = asyncio.Lock()
    tavily_rate_limit_retries = cfg.tavily.rate_limit_retries

    firecrawl_max_calls = cfg.firecrawl.max_calls_per_session
    _firecrawl_call_count: int = 0
    _firecrawl_call_lock = asyncio.Lock()
    firecrawl_rate_limit_retries = cfg.firecrawl.rate_limit_retries

    async def _call(query: str, max_results: int = 10, **_: Any) -> ToolResult:
        nonlocal _tavily_call_count, _firecrawl_call_count
        if not backends:
            return ToolResult(
                content=(
                    "web_search has no usable backend (no tavily key, no firecrawl "
                    "key, no searxng)."
                ),
                error="no backend configured",
            )
        last_error: str = ""
        for backend in backends:
            try:
                if backend == "tavily":
                    if not tavily_key:
                        continue
                    async with _tavily_call_lock:
                        if tavily_max_calls is not None and _tavily_call_count >= tavily_max_calls:
                            logger.info(
                                "Tavily call quota exhausted (%d >= %d), falling back",
                                _tavily_call_count,
                                tavily_max_calls,
                            )
                            continue
                        _tavily_call_count += 1
                    try:
                        citations = await _tavily_with_retry(
                            query=query,
                            max_results=max_results,
                            api_key=tavily_key,
                            search_depth=cfg.tavily.search_depth,
                            retries=tavily_rate_limit_retries,
                        )
                    except Exception:
                        # Decrement under lock so the counter stays accurate
                        async with _tavily_call_lock:
                            _tavily_call_count -= 1
                        raise
                elif backend == "firecrawl":
                    if not firecrawl_key:
                        continue
                    async with _firecrawl_call_lock:
                        if (
                            firecrawl_max_calls is not None
                            and _firecrawl_call_count >= firecrawl_max_calls
                        ):
                            logger.info(
                                "Firecrawl call quota exhausted (%d >= %d), falling back",
                                _firecrawl_call_count,
                                firecrawl_max_calls,
                            )
                            continue
                        _firecrawl_call_count += 1
                    try:
                        citations = await _firecrawl_search(
                            query=query,
                            max_results=max_results,
                            api_key=firecrawl_key,
                            endpoint=cfg.firecrawl.endpoint,
                            timeout_s=cfg.firecrawl.timeout_s,
                            retries=firecrawl_rate_limit_retries,
                        )
                    except Exception:
                        # Decrement under lock so the counter stays accurate
                        async with _firecrawl_call_lock:
                            _firecrawl_call_count -= 1
                        raise
                elif backend == "searxng":
                    citations = await _searxng_search(
                        query=query,
                        max_results=max_results,
                        searxng_url=cfg.searxng.url,
                        user_agent=config.fetch_page.user_agent,
                    )
                else:
                    logger.warning("unknown backend: %s", backend)
                    continue
                logger.info(
                    "web_search backend=%s query=%r -> %d results",
                    backend,
                    query,
                    len(citations),
                )
                return ToolResult(
                    content=_format_for_llm(citations),
                    citations=citations,
                )
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    "web_search backend=%s failed: %s; trying next",
                    backend,
                    last_error,
                )
                continue
        return ToolResult(
            content=f"All web_search backends failed. Last error: {last_error}",
            error=last_error,
        )

    reg.register("web_search", _call, SCHEMA)


__all__ = ["SCHEMA", "register"]
