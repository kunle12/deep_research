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

P7 enhanced — when the GET response's Content-Type is `application/pdf`
(or the URL ends in `.pdf`), fetch_page saves the bytes to a tmp cache file
and dispatches to `pdf_extract_text` (if registered in the same registry).
This lets callers that hit a direct PDF link (e.g. scholar side-links in
the academic path, blog_search PDF results in the applied path) get real
extracted text instead of trafilatura choking on the binary stream and
triggering a useless browser_navigate fallback.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import time
from pathlib import Path
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
        "Fetch a URL and return its main article body as plain text. "
        "For HTML pages, the body is trafilatura-extracted (boilerplate-stripped) "
        "with a headless-browser fallback via browser_navigate when extraction "
        "yields too little content. "
        "For PDFs (detected via Content-Type or .pdf URL suffix), the bytes "
        "are saved locally and dispatched to pdf_extract_text; no browser "
        "fallback is attempted for PDFs."
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
    """Disk-backed page cache.

    Key: URL. Value: ``(kind, html, text, fetched_at)`` where:
      - ``kind`` is ``"html"`` or ``"pdf"`` (determines how cache hits are
        re-dispatched by the caller).
      - ``html`` is the raw HTML for HTML pages, or ``""`` for PDFs (we
        never store the PDF binary in the cache; the bytes live on disk
        at ``<cache_dir>/pdfs/<digest>.pdf`` and the *extracted text* is
        cached here).
      - ``text`` is the extracted (trafilatura or pdf_extract_text) text.
      - ``fetched_at`` is a ``time.time()`` timestamp (wall-clock, persists
        across process restarts).

    Legacy 3-tuple entries ``(html, text, fetched_at)`` written by older
    versions are tolerated and treated as ``("html", html, text)``.
    """

    def __init__(self, directory: str, ttl_seconds: int) -> None:
        try:
            self._cache = Cache(directory)
        except Exception:
            logger.warning("diskcache init failed at %s; running without cache", directory)
            self._cache = None
        self._ttl = ttl_seconds

    def get(self, url: str) -> tuple[str, str, str] | None:
        if self._cache is None:
            return None
        try:
            row = self._cache.get(url)
        except Exception:
            return None
        if not row:
            return None
        if len(row) == 3:
            html, text, fetched_at = row
            kind = "html"
        elif len(row) == 4:
            kind, html, text, fetched_at = row
        else:
            return None
        if (time.time() - fetched_at) > self._ttl:
            return None
        return kind, html, text

    def set(self, url: str, kind: str, html: str, text: str) -> None:
        if self._cache is None:
            return
        with contextlib.suppress(Exception):
            self._cache.set(url, (kind, html, text, time.time()))


async def _fetch(
    url: str,
    user_agent: str,
    timeout_s: int,
) -> httpx.Response:
    """Single GET. Caller is responsible for branching on Content-Type."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        "Accept-Language": "en,en-US;q=0.9",
    }
    async with httpx.AsyncClient(
        timeout=timeout_s, follow_redirects=True, max_redirects=5
    ) as client:
        return await client.get(url, headers=headers)


def _extract(html: str, url: str) -> str:
    """Run trafilatura extraction."""
    if not html:
        return ""
    return (
        trafilatura.extract(
            html,
            url=url,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            include_links=False,
            favor_recall=True,
        )
        or ""
    )


def _is_pdf(ctype: str, url: str) -> bool:
    """True when the response Content-Type or URL suffix indicates a PDF.

    Content-Type is authoritative when present. The suffix heuristic is a
    fallback only for servers that omit the Content-Type header entirely
    (empty or ``None`` after the GET).
    """
    if ctype == "application/pdf":
        return True
    # application/octet-stream is ambiguous — check URL suffix
    if ctype == "application/octet-stream":
        return url.lower().split("?", 1)[0].split("#", 1)[0].endswith(".pdf")
    # If the server sent a non-empty Content-Type that isn't PDF, trust it.
    if ctype:
        return False
    # No Content-Type from server — fall back to URL suffix heuristic.
    return url.lower().split("?", 1)[0].split("#", 1)[0].endswith(".pdf")


def _save_pdf_bytes(url: str, content: bytes, cache_dir: str) -> Path:
    """Persist PDF bytes to <cache_dir>/pdfs/<digest>.pdf; return the path."""
    pdf_dir = Path(cache_dir) / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    pdf_path = pdf_dir / f"{digest}.pdf"
    pdf_path.write_bytes(content)
    return pdf_path


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    cfg = config.fetch_page
    page_cache = _PageCache(directory=cfg.cache_dir, ttl_seconds=cfg.cache_ttl_hours * 3600)

    async def _call(url: str, **_: Any) -> ToolResult:
        if not url or not url.startswith(("http://", "https://")):
            return ToolResult(content="", error=f"invalid url: {url!r}")

        cached = page_cache.get(url)
        if cached is not None:
            kind, html, text = cached
            logger.info("fetch_page cache hit (%s): %s", kind, url)
            if kind == "pdf":
                # Cached PDF extraction result — return directly. No browser
                # fallback: a PDF URL is unambiguously a PDF, not a JS-heavy
                # HTML page that could benefit from rendering.
                cit = Citation(
                    url=url,
                    title=url,
                    snippet=text[:200],
                    source_type="pdf",
                    confidence_score=0.7,
                    discovered_by=ToolName.fetch_page,
                )
                if text.strip():
                    return ToolResult(content=text, citations=[cit])
                return ToolResult(
                    content="",
                    error=(
                        f"fetch_page received a PDF at {url} but extracted 0 chars "
                        "(cached result). Consider pdf_render_pages (vision) "
                        "or a different source."
                    ),
                    citations=[cit],
                )
            # kind == "html" — fall through to the HTML low-yield / browser
            # fallback logic below using the cached (html, text).
        else:
            # Cache miss — fetch + dispatch by Content-Type.
            try:
                resp = await _fetch(url, cfg.user_agent, cfg.request_timeout_s)
            except httpx.HTTPError as e:
                return ToolResult(content="", error=f"httpx HTTP error: {type(e).__name__}: {e}")
            except Exception as e:
                return ToolResult(content="", error=f"{type(e).__name__}: {e}")

            if resp.status_code >= 400:
                return ToolResult(
                    content=(
                        f"HTTP {resp.status_code} from {url}\n\n"
                        f"First 1000 chars:\n{(resp.text or '')[:1000]}"
                    ),
                    error=f"HTTP {resp.status_code}",
                )

            ctype = resp.headers.get("content-type", "") or ""
            ctype = ctype.split(";")[0].strip().lower()

            if _is_pdf(ctype, url):
                # Save bytes to disk and dispatch to pdf_extract_text.
                try:
                    pdf_path = _save_pdf_bytes(url, resp.content, cfg.cache_dir)
                except Exception as e:
                    return ToolResult(
                        content="",
                        error=f"failed to save PDF for extraction: {type(e).__name__}: {e}",
                    )

                pdf_extract_available = "pdf_extract_text" in reg.names()
                text = ""
                if pdf_extract_available:
                    extract_res = await reg.call("pdf_extract_text", {"file_path": str(pdf_path)})
                    if extract_res.error is None:
                        text = extract_res.content or ""
                    else:
                        logger.warning(
                            "pdf_extract_text failed for %s (saved at %s): %s",
                            url,
                            pdf_path,
                            extract_res.error,
                        )
                else:
                    logger.warning(
                        "fetch_page received a PDF at %s but pdf_extract_text is not "
                        "registered; PDF saved at %s, returning empty content",
                        url,
                        pdf_path,
                    )

                page_cache.set(url, "pdf", "", text)

                cit = Citation(
                    url=url,
                    title=url,
                    snippet=text[:200],
                    source_type="pdf",
                    confidence_score=0.7,
                    discovered_by=ToolName.fetch_page,
                )

                if text.strip():
                    logger.info(
                        "fetch_page extracted %d chars from PDF %s",
                        len(text.strip()),
                        url,
                    )
                    return ToolResult(content=text, citations=[cit])

                # PDF text extraction yielded nothing (or no tool available).
                # Surface a clear error so callers (academic.py, applied.py)
                # can decide to fall back to abstract / vision rendering
                # instead of treating this as generic low-yield HTML.
                error_detail = (
                    "extracted 0 chars"
                    if pdf_extract_available
                    else "pdf_extract_text tool is not registered"
                )
                return ToolResult(
                    content="",
                    error=(
                        f"fetch_page received a PDF at {url} but {error_detail}. "
                        f"PDF saved at: {pdf_path}. "
                        "Consider pdf_render_pages (vision) or a different source."
                    ),
                    citations=[cit],
                )

            # HTML path — decode and run trafilatura.
            html = resp.text
            text = _extract(html, url)
            page_cache.set(url, "html", html, text)

        # --- HTML low-yield + browser fallback (existing P4 logic) ----------
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
                text_stripped_len,
                url,
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
                url,
                browser_res.error,
            )
        else:
            logger.warning(
                "trafilatura extraction yielded only %d chars for %s; returning raw HTML excerpt",
                text_stripped_len,
                url,
            )

        return ToolResult(
            content=(
                f"(trafilatura extraction low-yield; returning raw HTML excerpt)\n\n{html[:8000]}"
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
