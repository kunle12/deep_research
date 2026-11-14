from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, ToolName
from deep_research.util import coerce_float

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

_ANCHOR_RX = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RX = re.compile(r"<[^>]+>")
_ARXIV_ID_RX = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")
# Navigation / legal links that are never blog posts.
_SKIP_LINK_MARKERS = (
    "/privacy",
    "/terms",
    "/cookie",
    "/login",
    "/signup",
    "/signin",
    "/account",
    "/careers",
    "/contact",
    "/about",
    "/feed",
    "/rss",
    "/sitemap",
    "#",
    "mailto:",
    "javascript:",
)


def _anchor_text(raw_html: str) -> str:
    text = _TAG_RX.sub(" ", raw_html)
    return re.sub(r"\s+", " ", text).strip()


def _extract_post_links(html: str, base_url: str, query: str) -> list[tuple[float, str, str]]:
    """Extract same-domain anchor links that look like posts for *query*.

    Returns ``(score, absolute_url, anchor_text)`` tuples sorted by score. The
    old direct-domain fallback cited the domain index page for every “post”;
    real anchors give usable permalinks.
    """
    base_host = urlparse(base_url).netloc.lower()
    terms = [t for t in query.lower().split() if len(t) > 1]
    if not terms:
        return []
    found: list[tuple[float, str, str]] = []
    seen: set[str] = set()
    for href, raw_text in _ANCHOR_RX.findall(html):
        text = _anchor_text(raw_text)
        if not text or len(text) < 8:
            continue
        absolute = urljoin(base_url, href.strip())
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() != base_host:
            continue
        lowered = absolute.lower()
        if any(marker in lowered for marker in _SKIP_LINK_MARKERS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        score = sum(1 for t in terms if t in text.lower()) / len(terms)
        if score >= 0.15:
            found.append((min(score, 1.0), absolute, text[:120]))
    found.sort(key=lambda t: t[0], reverse=True)
    return found


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

    # Build site: filter from known domains, keeping total query ≤ 400 chars
    domain_list = domains or _KNOWN_DOMAINS
    max_site_len = 400 - len(query) - len(" ()")  # room for the wrapper
    if max_site_len < 0:
        site_filter = ""
    else:
        # Include as many domains as fit
        parts: list[str] = []
        running = 0
        for d in domain_list:
            candidate = f"site:{d}"
            added = len(candidate) + (len(" OR ") if parts else 0)
            if running + added <= max_site_len:
                parts.append(candidate)
                running += added
            else:
                break
        site_filter = " OR ".join(parts)
    site_query = f"{query} ({site_filter})" if site_filter else query

    response = await client.search(
        query=site_query,
        max_results=max_results,
        search_depth="basic",
        include_answer=False,
    )
    results = response.get("results") if isinstance(response, dict) else []
    citations: list[Citation] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        citations.append(
            Citation(
                url=r.get("url", ""),
                title=r.get("title", "") or "",
                snippet=r.get("content", "") or "",
                source_type="blog",
                confidence_score=coerce_float(r.get("score"), 0.6),
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
    per_domain_limit: int = 3,
    concurrency: int = 8,
) -> list[Citation]:
    """Direct-domain fallback: fetch blog index pages and extract post links."""
    domains = domains or _KNOWN_DOMAINS
    citations: list[Citation] = []
    per_domain_limit = max(1, int(per_domain_limit))
    sem = asyncio.Semaphore(max(1, int(concurrency)))

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
        base_url = str(resp.url) or url
        domain_cits: list[Citation] = []

        # Prefer real anchor permalinks. Fall back to matched text lines only
        # when the page exposes no usable links (some index pages are JS-only).
        links = _extract_post_links(html, base_url, query)
        for score, link_url, title in links[:per_domain_limit]:
            domain_cits.append(
                Citation(
                    url=link_url,
                    title=title,
                    snippet=title,
                    source_type="blog",
                    confidence_score=score,
                    discovered_by=ToolName.web_search,
                )
            )

        if not domain_cits:
            text = (
                await asyncio.to_thread(
                    trafilatura.extract,
                    html,
                    url=base_url,
                    output_format="txt",
                    include_comments=False,
                    favor_recall=True,
                )
                or ""
            )
            text_lines = [ln for ln in text.split("\n") if ln.strip()]
            query_terms = query.lower().split()
            for line in text_lines[:30]:
                line_lower = line.lower()
                score = sum(1 for t in query_terms if t in line_lower) / max(
                    len(query_terms), 1
                )
                if score >= 0.15:
                    domain_cits.append(
                        Citation(
                            url=base_url,
                            title=line.strip()[:120],
                            snippet=line.strip()[:200],
                            source_type="blog",
                            confidence_score=min(score, 1.0),
                            discovered_by=ToolName.web_search,
                        )
                    )
            domain_cits = domain_cits[:per_domain_limit]

        return domain_cits

    async def _limited(domain: str, idx: int) -> list[Citation]:
        async with sem:
            return await _fetch_domain(domain, idx)

    tasks = [_limited(d, i) for i, d in enumerate(domains[: max_results * 2])]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, list):
            citations.extend(res)

    # Dedup by URL (real permalinks); keep the highest-confidence copy.
    best: dict[str, Citation] = {}
    for c in citations:
        cur = best.get(c.url)
        if cur is None or cur.confidence_score < c.confidence_score:
            best[c.url] = c
    deduped = sorted(best.values(), key=lambda x: x.confidence_score, reverse=True)[
        :max_results
    ]
    return deduped


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    cfg = config.blog_search
    tavily_key = config.search.resolve_tavily_key()

    async def _call(query: str, max_results: int | None = None, **_: Any) -> ToolResult:
        if not query:
            return ToolResult(content="No query provided.", error="empty query")
        max_results = int(max_results or cfg.search_limit or 8)
        max_results = max(1, min(max_results, 50))

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
        if cfg.use_domains_fallback and (cfg.primary in ("direct", "both") or not citations):
            try:
                direct_cits = await _direct_domain_fetch(
                    query,
                    max_results,
                    domains=cfg.known_domains,
                    user_agent=config.fetch_page.user_agent,
                    min_spacing_ms=cfg.domain_fallback_min_spacing_ms,
                    per_domain_limit=cfg.domain_fallback_per_domain_limit,
                    concurrency=cfg.concurrency,
                )
                citations.extend(direct_cits)
                logger.info("blog_search direct returned %d results", len(direct_cits))
            except Exception as e:
                logger.warning("blog_search direct failed: %s", e)

        # Cross-ref arxiv IDs in blog content so downstream citation handling
        # can dedupe a blog mention against the actual arXiv paper.
        if cfg.cross_ref_arxiv:
            for c in citations:
                if c.arxiv_id:
                    continue
                m = _ARXIV_ID_RX.search(f"{c.title} {c.snippet}")
                if m:
                    c.arxiv_id = m.group(1)

        # Dedup by URL
        seen = set()
        deduped: list[Citation] = []
        for c in sorted(citations, key=lambda x: x.confidence_score, reverse=True):
            if c.url not in seen:
                seen.add(c.url)
                deduped.append(c)

        if not deduped:
            return ToolResult(
                content=json.dumps(
                    {
                        "hits": [],
                        "backend_used": "none",
                        "note": "No blog posts found. Tavily may be unconfigured or domains returned empty.",
                    }
                ),
                error=None,
            )

        return ToolResult(
            content=json.dumps(
                {
                    "hits": [
                        {
                            "url": c.url,
                            "title": c.title,
                            "snippet": c.snippet[:300],
                            "confidence": c.confidence_score,
                        }
                        for c in deduped
                    ],
                    "backend_used": cfg.primary,
                }
            ),
            citations=deduped,
        )

    reg.register("blog_search", _call, SEARCH_SCHEMA)


__all__ = ["SEARCH_SCHEMA", "register"]
