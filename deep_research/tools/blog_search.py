from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import trafilatura

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, ToolName

logger = logging.getLogger(__name__)

SEARCH_SCHEMA = {
    "type": "function",
    "description": (
        "Search technical blogs for posts matching the query. Uses Tavily site: queries "
        "as primary backend, with a direct-domain fetch fallback for known blog domains. "
        "Returns up to `max_results` blog post summaries with permalink URLs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for blog posts",
            },
            "max_results": {
                "type": "integer",
                "description": "Max blog posts to return (default 8)",
                "default": 8,
            },
        },
        "required": ["query"],
    },
}

# Known blog domains for direct-domain fallback
_KNOWN_DOMAINS: list[str] = [
    "openai.com/index",
    "anthropic.com/research",
    "deepmind.google",
    "ai.meta.com/blog",
    "research.microsoft.com",
    "developers.googleblog.com",
    "stripe.com/blog",
    "distill.pub",
    "neclab.org",
    "paperswithcode.com",
    "github.blog",
]


async def _tavily_blog_search(
    query: str,
    max_results: int,
    tavily_key: str,
    domains: list[str] | None = None,
) -> list[Citation]:
    """Search blogs via Tavily site: queries."""
    from tavily import AsyncTavilyClient
    client = AsyncTavilyClient(api_key=tavily_key)

    # Build site: filter from known domains
    site_filter = " OR ".join(f"site:{d}" for d in (domains or _KNOWN_DOMAINS))
    site_query = f"{query} ({site_filter})"

    response = await client.search(
        query=site_query,
        max_results=max_results,
        search_depth="basic",
        include_answer=False,
    )
    results = response.get("results") or []
    citations: list[Citation] = []
    for r in results:
        citations.append(
            Citation(
                url=r.get("url", ""),
                title=r.get("title", "") or "",
                snippet=r.get("content", "") or "",
                source_type="blog",
                confidence_score=float(r.get("score") or 0.6),
                discovered_by=ToolName.web_search,
            )
        )
    return citations


async def _direct_domain_fetch(
    query: str,
    max_results: int,
    domains: list[str] | None = None,
    user_agent: str = "DeepResearchBot/0.1",
    min_spacing_ms: float = 500,
) -> list[Citation]:
    """Direct-domain fallback: fetch blog index pages and extract posts."""
    domains = domains or _KNOWN_DOMAINS
    citations: list[Citation] = []

    async def _fetch_domain(domain: str, idx: int) -> list[Citation]:
        if idx > 0:
            await asyncio.sleep(min_spacing_ms / 1000)
        url = f"https://{domain}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": user_agent, "Accept": "text/html"},
                    follow_redirects=True,
                )
                resp.raise_for_status()
        except Exception as e:
            logger.warning("direct-domain fetch failed for %s: %s", domain, e)
            return []

        html = resp.text
        text = trafilatura.extract(html, url=url, output_format="txt", include_comments=False, favor_recall=True) or ""

        # Simple title matching: find lines that contain query terms
        text_lines = [ln for ln in text.split("\n") if ln.strip()]
        domain_cits: list[Citation] = []
        for line in text_lines[:30]:  # Check first 30 lines
            line_lower = line.lower()
            query_terms = query.lower().split()
            score = sum(1 for t in query_terms if t in line_lower) / max(len(query_terms), 1)
            if score >= 0.15:
                domain_cits.append(
                    Citation(
                        url=url,
                        title=line.strip()[:120],
                        snippet=line.strip()[:200],
                        source_type="blog",
                        confidence_score=min(score, 1.0),
                        discovered_by=ToolName.web_search,
                    )
                )

        return domain_cits[:3]  # Cap per domain

    tasks = [_fetch_domain(d, i) for i, d in enumerate(domains[:max_results * 2])]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, list):
            citations.extend(res)

    # Dedup and limit
    seen = set()
    deduped: list[Citation] = []
    for c in sorted(citations, key=lambda x: x.confidence_score, reverse=True):
        if c.url not in seen and len(deduped) < max_results:
            seen.add(c.url)
            deduped.append(c)
    return deduped


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    cfg = config.blog_search
    tavily_key = config.search.resolve_tavily_key()

    async def _call(query: str, max_results: int = 8, **_: Any) -> ToolResult:
        if not query:
            return ToolResult(content="No query provided.", error="empty query")

        citations: list[Citation] = []

        # Tavily primary path
        if cfg.primary in ("tavily", "both") and tavily_key:
            try:
                tavily_cits = await _tavily_blog_search(
                    query, max_results, tavily_key, cfg.known_domains
                )
                citations.extend(tavily_cits)
                logger.info("blog_search tavily returned %d results", len(tavily_cits))
            except Exception as e:
                logger.warning("blog_search tavily failed: %s", e)

        # Direct-domain fallback
        if cfg.use_domains_fallback and (
            cfg.primary in ("direct", "both") or not citations
        ):
            try:
                direct_cits = await _direct_domain_fetch(
                    query, max_results,
                    domains=cfg.known_domains,
                    user_agent=config.fetch_page.user_agent,
                    min_spacing_ms=cfg.domain_fallback_min_spacing_ms,
                )
                citations.extend(direct_cits)
                logger.info("blog_search direct returned %d results", len(direct_cits))
            except Exception as e:
                logger.warning("blog_search direct failed: %s", e)

        # Cross-ref arxiv IDs in blog content (placeholder for future implementation)

        # Dedup by URL
        seen = set()
        deduped: list[Citation] = []
        for c in sorted(citations, key=lambda x: x.confidence_score, reverse=True):
            if c.url not in seen:
                seen.add(c.url)
                deduped.append(c)

        if not deduped:
            return ToolResult(
                content=json.dumps({
                    "hits": [],
                    "backend_used": "none",
                    "note": "No blog posts found. Tavily may be unconfigured or domains returned empty.",
                }),
                error=None,
            )

        return ToolResult(
            content=json.dumps({
                "hits": [{"url": c.url, "title": c.title, "confidence": c.confidence_score} for c in deduped],
                "backend_used": cfg.primary,
            }),
            citations=deduped,
        )

    reg.register("blog_search", _call, SEARCH_SCHEMA)


__all__ = ["SEARCH_SCHEMA", "register"]
