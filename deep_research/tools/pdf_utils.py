"""Shared PDF utilities — parse tool result paths and rendered-page data URLs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deep_research.llm.tool_loop import ToolResult


def parse_pdf_path(content_str: str) -> str | None:
    """Best-effort parse of the download_pdf tool's returned content as a path."""
    if not content_str or not content_str.strip():
        return None
    lines = content_str.strip().splitlines()
    s = lines[0].strip() if lines else ""
    return s if s.startswith("/") else None


def parse_rendered_pages(render_result: ToolResult) -> list[str]:
    """Decode the JSON returned by `pdf_render_pages` into a list of data URLs.

    The pdf tool returns {"pages": ["data:image/jpeg;base64,...", ...], "count": N}.
    Returns [] on any error or non-JSON content so callers stay robust.
    """
    if render_result.error is not None or not render_result.content:
        return []
    try:
        data = json.loads(render_result.content)
        pages = data.get("pages") if isinstance(data, dict) else None
        if not isinstance(pages, list):
            return []
        return [p for p in pages if isinstance(p, str) and p.startswith("data:")]
    except Exception:
        return []


__all__ = ["parse_pdf_path", "parse_rendered_pages"]
