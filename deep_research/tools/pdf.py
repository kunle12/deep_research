"""pdf tool — extract text + render pages as downscaled JPEG data URLs.

P6: real implementations backed by:
- `pypdf` (fast text extraction)
- `pdfplumber` (accuracy fallback for tables / complex layouts)
- `pdf2image` (subprocess to poppler's `pdftoppm`) -> PIL pages
- `PIL` (downscale to `max_dim` via LANCZOS, JPEG quality 80) — via
  `deep_research.llm.vision.resize_for_vlm`
- OpenAI `image_url` content blocks assembled via
  `deep_research.llm.vision.jpeg_bytes_to_data_url`

Poppler is invoked as a subprocess; a missing binary is surfaced as a
clear runtime error pointing at the README's install instructions, so the
agent degrades cleanly (the text path still works) on hosts without poppler.

Tool schemas:
- pdf_extract_text(file_path) -> plaintext (pypdf first; pdfplumber fallback
  when text is sparse or contains embedded tables)
- pdf_render_pages(file_path, max_pages) -> JSON list of base64 data URLs
  ready to embed as chat-image_url content blocks. Unavailable when poppler
  is missing or pdf_vision is disabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from deep_research.config import AgentTopConfig
from deep_research.llm.tool_loop import ToolRegistry, ToolResult
from deep_research.llm.vision import jpeg_bytes_to_data_url, resize_for_vlm

logger = logging.getLogger(__name__)


EXTRACT_SCHEMA = {
    "type": "function",
    "description": (
        "Extract plain text from a local PDF file path. Tries pypdf first "
        "(fast); falls back to pdfplumber when the result looks suspicious "
        "(very few chars or empty). Use this after `arxiv_download_pdf` "
        "returned a path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Local PDF path"},
        },
        "required": ["file_path"],
    },
}


RENDER_SCHEMA = {
    "type": "function",
    "description": (
        "Render PDF pages to downscaled JPEG data URLs (VLM-ready). "
        "Returns a JSON string shaped as "
        '{"pages": ["data:image/jpeg;base64,...", ...], "count": N}. '
        "Set max_pages high enough to cover the paper you actually want to read. "
        "Unavailable when poppler is missing or pdf_vision is disabled."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Local PDF path"},
            "max_pages": {
                "type": "integer",
                "description": "Cap on pages to render (default 25).",
                "default": 25,
            },
        },
        "required": ["file_path"],
    },
}


_MIN_PYPDF_CHARS = 100  # below this we fall back to pdfplumber


def _sync_extract(file_path: str) -> str:
    """Synchronous extract: pypdf first, then pdfplumber fallback."""
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return f"(pdf_extract_text: file not found: {file_path})"

    # --- pypdf (fast path) ---------------------------------------------------
    text: str = ""
    try:
        import pypdf

        with pypdf.PdfReader(str(p)) as reader:
            chunks: list[str] = []
            for page in reader.pages:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception as e:
                    logger.debug("pypdf page extraction failed: %s", e)
            text = "\n\n".join(chunks).strip()
    except Exception as e:
        logger.warning("pypdf extraction failed for %s: %s", p, e)
        text = ""

    if len(text) >= _MIN_PYPDF_CHARS:
        return text

    # --- pdfplumber (accuracy fallback) --------------------------------------
    logger.info(
        "pypdf yielded %d chars (below %d); falling back to pdfplumber for %s",
        len(text), _MIN_PYPDF_CHARS, p,
    )
    try:
        import pdfplumber

        chunks: list[str] = []
        with pdfplumber.open(str(p)) as pdf:
            for page in pdf.pages:
                try:
                    page_text = page.extract_text() or ""
                except Exception as e:
                    logger.debug("pdfplumber page failed: %s", e)
                    page_text = ""
                if page_text:
                    chunks.append(page_text)
        text2 = "\n\n".join(chunks).strip()
    except Exception as e:
        logger.warning("pdfplumber extraction failed for %s: %s", p, e)
        return text  # last best-effort output

    # Use whichever produced more text.
    return text2 if len(text2) > len(text) else text


def _sync_render(file_path: str, max_pages: int, render_dpi: int, poppler_path: str | None) -> list[Any]:
    """Synchronous pdf2image conversion → list of PIL Images.

    Raises `poppler-missing` exception types as RuntimeError-derived; we catch
    them up at the async boundary so the tool returns a clean error message
    instead of crashing the run.
    """
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"PDF not found: {file_path}")

    from pdf2image import convert_from_path

    images = convert_from_path(
        str(p),
        dpi=render_dpi,
        first_page=1,
        last_page=max_pages,
        fmt="png",  # PIL→JPEG resize happens in vision.resize_for_vlm
        poppler_path=poppler_path,
    )
    return images


class _PopplerMissingError(RuntimeError):
    """Raised when pdf2image can't find poppler's pdftoppm binary."""


def _is_poppler_missing(exc: Exception) -> bool:
    """Heuristic: detect pdf2image's `PDFInfoNotInstalledError` without importing it."""
    cls_name = type(exc).__name__
    return cls_name in {"PDFInfoNotInstalledError", "PDFPopplerNotInstalledError"} or (
        "poppler" in str(exc).lower() and ("not installed" in str(exc).lower() or "not found" in str(exc).lower())
    )


async def register(reg: ToolRegistry, config: AgentTopConfig) -> None:
    cfg = config.pdf_vision

    async def _extract(file_path: str, **_: Any) -> ToolResult:
        if not file_path:
            return ToolResult(content="", error="file_path is required")
        try:
            text = await asyncio.to_thread(_sync_extract, file_path)
        except Exception as e:
            return ToolResult(content="", error=f"{type(e).__name__}: {e}")
        if not text:
            return ToolResult(content="(no extractable text)", citations=[])
        # _sync_extract prefixes missing-file cases with `(pdf_extract_text: ...)`
        # so callers can tell that apart from real extracted text. Surface it
        # as an `error` flag so the dispatcher's "fetch-failure short-circuit"
        # logic kicks in.
        if text.startswith("(pdf_extract_text: file not found"):
            return ToolResult(content="", error=text)
        return ToolResult(content=text)

    async def _render(file_path: str, max_pages: int = 25, **_: Any) -> ToolResult:
        if not cfg.enabled:
            return ToolResult(content="", error="pdf_vision is disabled in config")
        if not file_path:
            return ToolResult(content="", error="file_path is required")
        try:
            images = await asyncio.to_thread(
                _sync_render, file_path, int(max_pages), cfg.render_dpi, cfg.poppler_path
            )
        except FileNotFoundError as e:
            return ToolResult(content="", error=str(e))
        except Exception as e:
            if _is_poppler_missing(e):
                msg = (
                    "poppler-utils not found on PATH. pdf2image needs the "
                    "`pdftoppm` binary. See README for install instructions "
                    "(e.g., `brew install poppler` on macOS, "
                    "`apt-get install poppler-utils` on Debian/Ubuntu)."
                )
                return ToolResult(content="", error=msg)
            logger.exception("pdf_render_pages failed for %s", file_path)
            return ToolResult(content="", error=f"{type(e).__name__}: {e}")

        if not images:
            return ToolResult(content='{"pages": [], "count": 0}')

        # Downscale + JPEG-encode each page; turn into a base64 data URL the
        # OpenAI chat-completions API accepts as `image_url.url`.
        data_urls: list[str] = []
        try:
            for img in images:
                jpeg = await asyncio.to_thread(
                    resize_for_vlm, img, int(cfg.max_dim), int(cfg.jpeg_quality)
                )
                data_urls.append(jpeg_bytes_to_data_url(jpeg))
        except Exception as e:
            return ToolResult(content="", error=f"vision post-process failed: {type(e).__name__}: {e}")

        payload = {"pages": data_urls, "count": len(data_urls)}
        return ToolResult(content=json.dumps(payload))

    reg.register("pdf_extract_text", _extract, EXTRACT_SCHEMA)
    # The vision-rendering path requires poppler + is more expensive; gate it.
    if cfg.enabled:
        reg.register("pdf_render_pages", _render, RENDER_SCHEMA)


__all__ = ["register"]
