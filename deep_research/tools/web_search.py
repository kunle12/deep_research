"""web_search tool — Tavily (primary) + SearXNG (fallback chain).

Returns a list of normalized `Citation` objects alongside the human-readable
result text. Retries Tavily on rate-limit errors with exponential backoff
before seamlessly falling back to SearXNG. Supports proactive quota-based
fallback via ``max_calls_per_session``.
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

logger = logging.getLogger(__name__)



SCHEMA = {
    "type": "function",
    "description": (
        "Search the web. Returns up to `max_results` results, each with a URL, "
        "title, and a content snippet. Use this for general queries, news, "
        "blog posts. Falls back to SearXNG if Tavily is unavailable."
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
    results = response.get("results") or []
    citations: list[Citation] = []
    for r in results:
        citations.append(
            Citation(
                url=r.get("url", ""),
                title=r.get("title") or "",
                snippet=r.get("content") or "",
                source_type="web",
                confidence_score=float(r.get("score") or 0.5),
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
            retry_after = e.retry_after_seconds or 2 ** attempt
            logger.warning(
                "Tavily keyless limit hit (attempt %d/%d), retrying after %ds",
                attempt + 1, retries + 1, retry_after,
            )
            await asyncio.sleep(retry_after)
        except UsageLimitExceededError as e:
            backoff = 2 ** attempt
            logger.warning(
                "Tavily rate limit (attempt %d/%d), backing off %ds",
                attempt + 1, retries + 1, backoff,
            )
            await asyncio.sleep(backoff)
        except TimeoutError as e:
            backoff = 2 ** attempt
            logger.warning(
                "Tavily timeout (attempt %d/%d), backing off %ds",
                attempt + 1, retries + 1, backoff,
            )
            await asyncio.sleep(backoff)
        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            # Treat HTTP-level errors as transient; retry once
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
                continue
            raise  # Exhausted retries — let caller fall through
        except Exception:
            # Non-recoverable Tavily errors (bad key, forbidden, bad request) —
            # don't retry, let caller fall through to SearXNG.
            raise
    raise RuntimeError("Tavily retry loop exited unexpectedly")


async def _searxng_search(
    query: str,
    max_results: int,
    searxng_url: str,
    user_agent: str,
) -> list[Citation]:
    """Call SearXNG's JSON API. Used as fallback when Tavily fails."""
    params = {"q": query, "format": "json"}
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(searxng_url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results") or []
    citations: list[Citation] = []
    for r in results[:max_results]:
        citations.append(
            Citation(
                url=r.get("url", ""),
                title=r.get("title", "") or "",
                snippet=r.get("content", "") or "",
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
    _tavily_call_count: int = 0
    _tavily_call_lock = asyncio.Lock()

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
            "will skip to SearXNG fallback if available.",
            cfg.tavily.api_key_env,
        )
        backends = [b for b in backends if b != "tavily"]

    # Proactive quota guard: max calls allowed this session
    tavily_max_calls = cfg.tavily.max_calls_per_session
    tavily_rate_limit_retries = cfg.tavily.rate_limit_retries

    async def _call(query: str, max_results: int = 10, **_: Any) -> ToolResult:
        nonlocal _tavily_call_count
        if not backends:
            return ToolResult(
                content="web_search has no usable backend (no tavily key, no searxng).",
                error="no backend configured",
            )
        last_error: str = ""
        for backend in backends:
            try:
                if backend == "tavily":
                    if not tavily_key:
                        continue
                    # Proactive quota check — fall back to SearXNG if exhausted
                    # Lock ensures read + increment are atomic under concurrent calls
                    async with _tavily_call_lock:
                        if tavily_max_calls is not None and _tavily_call_count >= tavily_max_calls:
                            logger.info(
                                "Tavily call quota exhausted (%d >= %d), falling back",
                                _tavily_call_count, tavily_max_calls,
                            )
                            continue  # skip Tavily, try next backend (SearXNG)
                        _tavily_call_count += 1
                    citations = await _tavily_with_retry(
                        query=query,
                        max_results=max_results,
                        api_key=tavily_key,
                        search_depth=cfg.tavily.search_depth,
                        retries=tavily_rate_limit_retries,
                    )
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
                    backend, query, len(citations),
                )
                return ToolResult(
                    content=_format_for_llm(citations),
                    citations=citations,
                )
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    "web_search backend=%s failed: %s; trying next", backend, last_error,
                )
                continue
        return ToolResult(
            content=f"All web_search backends failed. Last error: {last_error}",
            error=last_error,
        )

    reg.register("web_search", _call, SCHEMA)


__all__ = ["SCHEMA", "register"]
