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

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import trafilatura
from diskcache import Cache

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.state import BLOCKED_PREFIX, Citation, ToolName

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bot-detection / unavailability classification
# ---------------------------------------------------------------------------

# Distinctive body markers for known bot-challenge vendors. Keys are the
# vendor name surfaced in the error; values are lowercased substrings that are
# specific enough to avoid false positives on legitimate articles (bare
# "cloudflare" / "access denied" are intentionally NOT markers).
_CHALLENGE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "cloudflare",
        (
            "cf-chl-",
            "cf-challenge",
            "__cf_chl",
            "challenge-platform",
            "just a moment",
            "checking your browser",
            "enable javascript and cookies to continue",
        ),
    ),
    (
        "datadome",
        (
            "datadome",
            "x-datadome",
            "ddos protection by datadome",
            "blocked by datadome",
        ),
    ),
    (
        "akamai",
        (
            "ak_bmsc",
            "bm_sz",
            "bm-verify",
            "/_sec/cp_challenge",
            "akamai bot manager",
            "you don't have permission to access",
        ),
    ),
    (
        "perimeterx",
        (
            "perimeterx",
            "px-captcha",
            "_pxhd",
        ),
    ),
    (
        "recaptcha",
        (
            "recaptcha",
            "g-recaptcha",
            "i'm not a robot",
            "unusual traffic",
        ),
    ),
    (
        "hcaptcha",
        (
            "hcaptcha",
            "h-captcha",
        ),
    ),
    (
        "generic",
        (
            "verify you are human",
            "are you a human",
            "robot check",
            "your request has been blocked",
            "request has been blocked",
            "bot detected",
        ),
    ),
)


class BlockedVerdict:
    """Structured classification of an unretrievable response.

    Serialized into a ToolResult error of the form
    ``BLOCKED:<category>[:<vendor>] (<status>)`` so the researcher LLM and
    programmatic callers can branch without parsing prose.
    """

    __slots__ = ("category", "status", "vendor")

    def __init__(self, category: str, vendor: str | None, status: int | None) -> None:
        self.category = category
        self.vendor = vendor
        self.status = status

    @property
    def error(self) -> str:
        parts = [BLOCKED_PREFIX, self.category]
        if self.category == "bot_detection" and self.vendor:
            parts.append(f":{self.vendor}")
        if self.status is not None:
            parts.append(f" ({self.status})")
        return "".join(parts)


def detect_challenge_vendor(text: str) -> str | None:
    """Return the bot-challenge vendor name for *text*, or None.

    Used on raw HTML (fetch path) and browser accessibility snapshots (browser
    path) so 200-OK challenge pages are caught, not just 403s.
    """
    if not text:
        return None
    lowered = text.lower()
    for vendor, markers in _CHALLENGE_MARKERS:
        for marker in markers:
            if marker in lowered:
                return vendor
    return None


def classify_blocked_response(
    status_code: int,
    headers: Mapping[str, str],
    body: str,
) -> BlockedVerdict | None:
    """Classify an HTTP response as blocked/unavailable, or None when fine.

    Order matters: a 429 is always rate limiting; a 404 is always missing; a
    403 or a 200 challenge body is bot detection when vendor markers match;
    anything else >= 400 is a generic HTTP error.
    """
    if status_code == 429:
        return BlockedVerdict("rate_limited", None, 429)
    if status_code == 404:
        return BlockedVerdict("not_found", None, 404)

    vendor = detect_challenge_vendor(body)
    if vendor is None:
        # Header-level challenge signals (Cloudflare mitigated, DataDome).
        cf = (headers.get("cf-mitigated") or "").lower()
        if cf in {"challenge", "block"}:
            vendor = "cloudflare"
        elif (headers.get("x-datadome") or "").lower() in {"block", "captcha"}:
            vendor = "datadome"

    if vendor is not None and (status_code == 403 or 200 <= status_code < 300):
        return BlockedVerdict("bot_detection", vendor, status_code)
    if status_code >= 400:
        return BlockedVerdict("http_error", None, status_code)
    return None


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

    A fourth kind, ``"archive"``, stores Wayback-rescued content under the
    ORIGINAL URL; the ``html`` slot then carries the concrete snapshot URL
    instead of raw HTML (see ``_call``).

    Blocked verdicts are stored under ``blocked:<url>`` with a separate,
    shorter TTL so a bot-blocked page is not re-fetched repeatedly within a
    run (the main cache only ever stores successful extractions).
    """

    def __init__(self, directory: str, ttl_seconds: int, blocked_ttl_seconds: int) -> None:
        try:
            self._cache = Cache(directory)
        except Exception:
            logger.warning("diskcache init failed at %s; running without cache", directory)
            self._cache = None
        self._ttl = ttl_seconds
        self._blocked_ttl = blocked_ttl_seconds

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

    def get_blocked(self, url: str) -> str | None:
        """Return the cached ``BLOCKED:...`` error for *url*, or None."""
        if self._cache is None:
            return None
        try:
            row = self._cache.get(f"blocked:{url}")
        except Exception:
            return None
        if not row:
            return None
        error, fetched_at = row
        if (time.time() - fetched_at) > self._blocked_ttl:
            return None
        return error

    def set_blocked(self, url: str, error: str) -> None:
        if self._cache is None:
            return
        with contextlib.suppress(Exception):
            self._cache.set(f"blocked:{url}", (error, time.time()))

    async def aget(self, url: str) -> tuple[str, str, str] | None:
        """Non-blocking variant of `get` (diskcache I/O runs in a thread)."""
        return await asyncio.to_thread(self.get, url)

    async def aset(self, url: str, kind: str, html: str, text: str) -> None:
        """Non-blocking variant of `set` (diskcache I/O runs in a thread)."""
        await asyncio.to_thread(self.set, url, kind, html, text)

    async def aget_blocked(self, url: str) -> str | None:
        """Non-blocking variant of `get_blocked`."""
        return await asyncio.to_thread(self.get_blocked, url)

    async def aset_blocked(self, url: str, error: str) -> None:
        """Non-blocking variant of `set_blocked`."""
        await asyncio.to_thread(self.set_blocked, url, error)


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
    """Run trafilatura extraction (sync — call via asyncio.to_thread)."""
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


_MIN_ARCHIVE_TEXT_CHARS = 100


def _wayback_url(url: str) -> str:
    """Build the Wayback Machine "latest snapshot" URL for *url*."""
    return f"https://web.archive.org/web/2/{url}"


def _is_archive_url(url: str) -> bool:
    """True when *url* is already on the Wayback Machine / archive.org."""
    lowered = url.lower()
    return "web.archive.org" in lowered or "archive.org/wayback" in lowered


async def _fetch_wayback(
    original_url: str,
    user_agent: str,
    timeout_s: int,
) -> tuple[str, str] | None:
    """Fetch the latest Wayback snapshot of *original_url*.

    Returns ``(extracted_text, concrete_snapshot_url)`` or None when there is
    no usable snapshot (no capture, extraction too thin, or archive.org itself
    blocks / rate-limits). The concrete snapshot URL is the redirect target
    (``/web/<timestamp>/<url>``) so citations point at a stable capture.
    """
    wayback_url = _wayback_url(original_url)
    try:
        resp = await _fetch(wayback_url, user_agent, timeout_s)
    except Exception:
        return None
    if classify_blocked_response(resp.status_code, resp.headers, resp.text) is not None:
        return None
    text = await asyncio.to_thread(_extract, resp.text, str(resp.url))
    if len(text.strip()) < _MIN_ARCHIVE_TEXT_CHARS:
        return None
    return (text, str(resp.url))


def _annotate_archived(text: str, original_url: str) -> str:
    """Prefix archived content with provenance so the researcher cites it correctly."""
    return (
        f"(Original URL {original_url} was blocked by bot detection or a fetch "
        f"error; this content was retrieved via the Wayback Machine archive.)\n\n{text}"
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
    page_cache = _PageCache(
        directory=cfg.cache_dir,
        ttl_seconds=cfg.cache_ttl_hours * 3600,
        blocked_ttl_seconds=cfg.blocked_cache_ttl_s,
    )

    async def _call(url: str, **_: Any) -> ToolResult:
        if not url or not url.startswith(("http://", "https://")):
            return ToolResult(content="", error=f"invalid url: {url!r}")

        cached = await page_cache.aget(url)
        if cached is not None:
            kind, html, text = cached
            logger.info("fetch_page cache hit (%s): %s", kind, url)
            if kind == "archive":
                # Wayback-rescued content cached under the original URL; the
                # `html` slot carries the concrete snapshot URL.
                archive_url = html
                if not text.strip():
                    return ToolResult(
                        content="",
                        error=(
                            f"fetch_page cached Wayback content for {url} but it extracted 0 chars."
                        ),
                    )
                first_line = text.strip().splitlines()[0][:120]
                cit = Citation(
                    url=archive_url,
                    title=first_line or archive_url,
                    snippet=text[:200],
                    source_type="html",
                    confidence_score=0.5,
                    discovered_by=ToolName.fetch_page,
                )
                return ToolResult(
                    content=_annotate_archived(text, url),
                    citations=[cit],
                )
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
                )
            # kind == "html" — fall through to the HTML low-yield / browser
            # fallback logic below using the cached (html, text).
        else:
            blocked = await page_cache.aget_blocked(url)
            if blocked is not None:
                logger.info("fetch_page blocked-cache hit: %s", url)
                return ToolResult(content="", error=blocked)
            # Cache miss — fetch + dispatch by Content-Type.
            try:
                resp = await _fetch(url, cfg.user_agent, cfg.request_timeout_s)
            except httpx.HTTPError as e:
                return ToolResult(content="", error=f"httpx HTTP error: {type(e).__name__}: {e}")
            except Exception as e:
                return ToolResult(content="", error=f"{type(e).__name__}: {e}")

            verdict = classify_blocked_response(resp.status_code, resp.headers, resp.text)
            if verdict is not None:
                if cfg.archive_org_fallback and not _is_archive_url(url):
                    archived = await _fetch_wayback(url, cfg.user_agent, cfg.request_timeout_s)
                    if archived is not None:
                        text, archive_url = archived
                        await page_cache.aset(url, "archive", archive_url, text)
                        first_line = text.strip().splitlines()[0][:120]
                        cit = Citation(
                            url=archive_url,
                            title=first_line or archive_url,
                            snippet=text[:200],
                            source_type="html",
                            confidence_score=0.5,
                            discovered_by=ToolName.fetch_page,
                        )
                        logger.info(
                            "fetch_page rescued %s via Wayback snapshot %s",
                            url,
                            archive_url,
                        )
                        return ToolResult(
                            content=_annotate_archived(text, url),
                            citations=[cit],
                        )
                    logger.info(
                        "fetch_page: no usable Wayback snapshot for %s (%s)",
                        url,
                        verdict.error,
                    )
                await page_cache.aset_blocked(url, verdict.error)
                return ToolResult(content="", error=verdict.error)

            ctype = resp.headers.get("content-type", "") or ""
            ctype = ctype.split(";")[0].strip().lower()

            if _is_pdf(ctype, url):
                # Save bytes to disk and dispatch to pdf_extract_text.
                try:
                    pdf_path = await asyncio.to_thread(
                        _save_pdf_bytes, url, resp.content, cfg.cache_dir
                    )
                except Exception as e:
                    return ToolResult(
                        content="",
                        error=f"failed to save PDF for extraction: {type(e).__name__}: {e}",
                    )

                pdf_extract_available = "pdf_extract_text" in reg.names()
                text = ""
                if pdf_extract_available:
                    # call_internal: fetch_page already holds a semaphore permit
                    # via the outer ToolRegistry.call; re-acquiring the shared
                    # semaphore here would self-deadlock when a batch saturates
                    # max_concurrent_tools.
                    extract_res = await reg.call_internal("pdf_extract_text", {"file_path": str(pdf_path)})
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

                await page_cache.aset(url, "pdf", "", text)

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
                )

            # HTML path — decode and run trafilatura.
            html = resp.text
            text = await asyncio.to_thread(_extract, html, url)
            await page_cache.aset(url, "html", html, text)

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
            browser_res = await reg.call_internal("browser_navigate", {"url": url})
            if browser_res.error and browser_res.error.startswith(BLOCKED_PREFIX):
                # Browser hit the same challenge — propagate the blocked verdict
                # instead of degrading to the challenge page's raw HTML.
                return ToolResult(content="", error=browser_res.error)
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
