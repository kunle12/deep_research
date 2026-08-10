"""Archive fetched HTML sources as a rendered PDF (preferred) or as a screenshot
image (fallback).

The archive seam used to store fetched blog/web pages as plain HTML text. Now
`archive_html_source` tries to convert the fetched content to a real PDF via
weasyprint (the same HTML->PDF renderer the report pipeline uses) and validates
the result; if the PDF isn't usable it falls back to a Playwright screenshot
image, and only if neither works stores the HTML text as before.

The `pdf_render_pages` vision tool's downscaled JPEG data URLs are never
persisted — we archive the original PDF bytes, not the VLM render output.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging

from deep_research.config import AgentTopConfig
from deep_research.library.writer import LibraryWriter, NullLibraryWriter
from deep_research.llm.tool_loop import ToolRegistry

logger = logging.getLogger(__name__)

# A rendered PDF is only "correct" enough to archive when it has at least one
# page and a non-trivial amount of extractable text. Blank / degenerate renders
# fall through to the image fallback.
_MIN_PDF_TEXT_CHARS = 50

# The fetched HTML is untrusted and unbounded, so we refuse to render pages
# larger than this (protects against OOM / pathological pages) and reject
# oversized PDF output. The whole render runs on a worker thread with a
# wait_for timeout in the caller so a slow render can never block the run.
_MAX_HTML_CHARS = 2_000_000
_MAX_PDF_BYTES = 10 * 1024 * 1024
_PDF_RENDER_TIMEOUT_S = 30.0

# Per-call timeout for the browser navigate / screenshot fallback so a hung
# page (slow load, bot-block wall) can't block the research run indefinitely.
_BROWSER_TIMEOUT_S = 30.0


def _render_html_to_pdf(html: str) -> bytes | None:
    """Synchronously render *html* to validated PDF bytes, or None.

    Degrades to None on any failure (missing weasyprint, render error, blank /
    degenerate output, oversized input/output) so callers can fall back to an
    image.
    """
    if len(html) > _MAX_HTML_CHARS:
        return None
    try:
        import weasyprint
    except Exception:
        return None
    try:
        buf = io.BytesIO()
        weasyprint.HTML(string=html).write_pdf(buf)
        data = buf.getvalue()
    except Exception:
        return None
    if not data.startswith(b"%PDF") or len(data) < 512:
        return None
    if len(data) > _MAX_PDF_BYTES:
        return None
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(data))
        if len(reader.pages) < 1:
            return None
        text_len = sum(len(p.extract_text() or "") for p in reader.pages)
    except Exception:
        return None
    if text_len < _MIN_PDF_TEXT_CHARS:
        return None
    return data


async def _capture_page_image(url: str, tools: ToolRegistry, config: AgentTopConfig) -> bytes | None:
    """Screenshot *url* via the Playwright browser; return PNG bytes or None.

    Returns None when the browser is disabled, the tools aren't registered, or
    navigation / screenshot fails / times out — callers then fall back to plain
    HTML.
    """
    if not config.browser.enabled:
        return None
    if "browser_navigate" not in tools.names() or "browser_take_screenshot" not in tools.names():
        return None
    try:
        nav = await asyncio.wait_for(
            tools.call("browser_navigate", {"url": url}), timeout=_BROWSER_TIMEOUT_S
        )
    except TimeoutError:
        logger.debug("screenshot fallback: navigate timed out")
        return None
    if nav.error is not None:
        logger.debug("screenshot fallback: navigate failed: %s", nav.error)
        return None
    try:
        shot = await asyncio.wait_for(
            tools.call("browser_take_screenshot", {}), timeout=_BROWSER_TIMEOUT_S
        )
    except TimeoutError:
        logger.debug("screenshot fallback: screenshot timed out")
        return None
    if shot.error is not None or not shot.content:
        logger.debug("screenshot fallback: screenshot failed: %s", shot.error)
        return None
    try:
        return base64.b64decode(shot.content, validate=True)
    except Exception:
        logger.debug("screenshot fallback: could not decode image data")
        return None


async def archive_html_source(
    url: str,
    html: str,
    *,
    tools: ToolRegistry,
    config: AgentTopConfig,
    writer: LibraryWriter | NullLibraryWriter | None,
) -> str:
    """Archive an HTML source as PDF when conversion is usable, else as an image.

    Returns the artifact_id, or "" when nothing was archived (NullLibraryWriter
    or all fallbacks failed). Never persists `pdf_render_pages` output — only
    the original rendered PDF bytes or a screenshot image.
    """
    if not isinstance(writer, LibraryWriter):
        return ""
    if config.pdl.archive_html_as_pdf:
        try:
            pdf_bytes = await asyncio.wait_for(
                asyncio.to_thread(_render_html_to_pdf, html),
                timeout=_PDF_RENDER_TIMEOUT_S,
            )
        except TimeoutError:
            logger.info("pdf render for %s timed out; using image/html fallback", url)
            pdf_bytes = None
        if pdf_bytes is not None:
            logger.info("archiving %s as PDF (%d bytes)", url, len(pdf_bytes))
            return await writer.archive_html(url, html, pdf_bytes=pdf_bytes)
    if config.pdl.archive_html_image_fallback:
        image_bytes = await _capture_page_image(url, tools, config)
        if image_bytes is not None:
            logger.info("archiving %s as image (%d bytes)", url, len(image_bytes))
            return await writer.archive_image(url, image_bytes)
    return await writer.archive_html(url, html)


__all__ = [
    "_capture_page_image",
    "_render_html_to_pdf",
    "archive_html_source",
]
