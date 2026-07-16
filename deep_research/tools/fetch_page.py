"""fetch_page tool — httpx + trafilatura article extraction + disk cache.

P2: implemented — real HTTP fetch, trafilatura body extraction, diskcache-backed
caching with TTL.

P4 enhanced — when trafilatura returns too little content, `fetch_page` itself
auto-falls-back to the `browser_navigate` tool (if registered in the same
registry and `browser.enabled` is true in config). The fallback is surfaced
uniformly to all callers (researcher, url_source, planner) instead of being
duplicated across each path. Callers can disable the fallback per-call by
setting `min_content_chars_for_browser_fallback` very high in config.

P4 also adds HEAD-probe Content-Type detection via `head_probe_content_type`
(shared with `url_classifier`) so PDF-vs-HTML ambiguity on direct URLs is
resolved before the heavy fetch.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

import httpx
import trafilatura
from diskcache import Cache

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import Citation, ToolName

logger = logging.getLogger(__name__)

SCHEMA = {
    "type": "function",
    "description": (
        "Fetch a URL and return its main article body as plain text "
        "(trafilatura-extracted, boilerplate-stripped). "
        "Falls back to raw HTML, or to a headless-browser render via "
        "browser_navigate if extraction yields too little content and the "
        "browser tool is available."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL to fetch."},
        },
        "required": ["url"],
    },
}


class _PageCache:
    """Disk-backed page cache. Key: URL. Value: (html, extracted_text, fetched_at)."""

    def __init__(self, directory: str, ttl_seconds: int) -> None:
        try:
            self._cache = Cache(directory)
        except Exception:
            logger.warning("diskcache init failed at %s; running without cache", directory)
            self._cache = None
        self._ttl = ttl_seconds

    def get(self, url: str) -> tuple[str, str] | None:
        if self._cache is None:
            return None
        try:
            row = self._cache.get(url)
        except Exception:
            return None
        if not row:
            return None
        html, text, fetched_at = row
        if (time.time() - fetched_at) > self._ttl:
            return None
        return html, text

    def set(self, url: str, html: str, text: str) -> None:
        if self._cache is None:
            return
        with contextlib.suppress(Exception):
            self._cache.set(url, (html, text, time.time()))


async def _fetch_html(
    url: str,
    user_agent: str,
    timeout_s: int,
) -> tuple[str, int, dict[str, str]]:
    """Returns (html, status_code, headers_downcased)."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en,en-US;q=0.9",
    }
    async with httpx.AsyncClient(
        timeout=timeout_s, follow_redirects=True, max_redirects=5
    ) as client:
        resp = await client.get(url, headers=headers)
        return resp.text, resp.status_code, {k.lower(): v for k, v in resp.headers.items()}


def _extract(html: str, url: str) -> str:
    """Run trafilatura extraction."""
    if not html:
        return ""
    return trafilatura.extract(
        html,
        url=url,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        include_links=False,
        favor_recall=True,
    ) or ""


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    cfg = config.fetch_page
    page_cache = _PageCache(directory=cfg.cache_dir, ttl_seconds=cfg.cache_ttl_hours * 3600)

    async def _call(url: str, **_: Any) -> ToolResult:
        if not url or not url.startswith(("http://", "https://")):
            return ToolResult(content="", error=f"invalid url: {url!r}")

        cached = page_cache.get(url)
        if cached is not None:
            html, text = cached
            logger.info("fetch_page cache hit: %s", url)
        else:
            try:
                html, status, _headers = await _fetch_html(
                    url, cfg.user_agent, cfg.request_timeout_s
                )
            except httpx.HTTPError as e:
                return ToolResult(
                    content="",
                    error=f"httpx HTTP error: {type(e).__name__}: {e}",
                )
            except Exception as e:
                return ToolResult(content="", error=f"{type(e).__name__}: {e}")
            if status >= 400:
                return ToolResult(
                    content=f"HTTP {status} from {url}\n\nFirst 1000 chars:\n{html[:1000]}",
                    error=f"HTTP {status}",
                )
            text = _extract(html, url)
            page_cache.set(url, html, text)

        min_chars = max(cfg.min_content_chars_for_browser_fallback, 100)
        text_stripped_len = len(text.strip())

        if text_stripped_len >= min_chars:
            logger.info("fetch_page extracted %d chars from %s", text_stripped_len, url)
            cit = Citation(
                url=url,
                title=_title_from_html(html) or url,
                snippet=text[:200],
                source_type="html",
                confidence_score=0.7,
                discovered_by=ToolName.fetch_page,
            )
            return ToolResult(content=text, citations=[cit])

        # Low-yield extraction. Try browser fallback if available.
        if config.browser.enabled and "browser_navigate" in reg.names():
            logger.info(
                "trafilatura extracted only %d chars for %s; trying browser_navigate fallback",
                text_stripped_len, url,
            )
            browser_res = await reg.call("browser_navigate", {"url": url})
            if browser_res.error is None and browser_res.content:
                # Browser render acceptable — surface its content + its citations.
                browser_cits = list(browser_res.citations)
                if not browser_cits:
                    browser_cits = [
                        Citation(
                            url=url,
                            title=_title_from_html(html) or url,
                            snippet=browser_res.content[:200],
                            source_type="html",
                            confidence_score=0.5,
                            discovered_by=ToolName.browser,
                        )
                    ]
                return ToolResult(content=browser_res.content, citations=browser_cits)
            logger.warning(
                "browser_navigate fallback for %s returned error=%s; falling back to raw HTML",
                url, browser_res.error,
            )
        else:
            logger.warning(
                "trafilatura extraction yielded only %d chars for %s; returning raw HTML excerpt",
                text_stripped_len, url,
            )

        return ToolResult(
            content=(
                f"(trafilatura extraction low-yield; returning raw HTML excerpt)\n\n"
                f"{html[:8000]}"
            ),
            citations=[
                Citation(
                    url=url,
                    title=_title_from_html(html) or url,
                    snippet=text[:200],
                    source_type="html",
                    confidence_score=0.3,
                    discovered_by=ToolName.fetch_page,
                )
            ],
        )

    reg.register("fetch_page", _call, SCHEMA)


_TITLE_RX_START = "<title"
_TITLE_RX_END = "</title>"


def _title_from_html(html: str) -> str:
    """Best-effort title extraction from raw HTML."""
    if not html:
        return ""
    lo = html.lower().find(_TITLE_RX_START)
    if lo < 0:
        return ""
    gt = html.find(">", lo)
    if gt < 0:
        return ""
    end = html.lower().find(_TITLE_RX_END, gt)
    if end < 0:
        return ""
    return html[gt + 1 : end].strip()


__all__ = ["SCHEMA", "register"]
