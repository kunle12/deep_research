"""Text formatting helpers for the library web UI."""

from __future__ import annotations

import json
import re
from typing import Any

_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_INLINE_SYNTAX_RE = re.compile(r"[*_~`#>]+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_HEADING_OR_FENCE_OR_TABLE = re.compile(r"^(#{1,6}\s|```|~~~|\|)")
_LIST_OR_HR = re.compile(r"^(\s*([-*+]|\d+\.)\s|(-{3,}|={3,})$)")


def parse_citations(citations_json: str | None) -> list[dict[str, Any]]:
    """Parse the reports.citations_json column into a list of dicts."""
    if not citations_json:
        return []
    try:
        data = json.loads(citations_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    return []


def citation_count(citations_json: str | None) -> int:
    """Number of citations stored on a report."""
    return len(parse_citations(citations_json))


def _clean_inline(text: str) -> str:
    """Strip markdown link/emphasis syntax for a plain-text snippet."""
    text = _LINK_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = _INLINE_SYNTAX_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = -1
    for m in _SENTENCE_END_RE.finditer(cut):
        if m.start() <= limit - 20:
            best = m.start() + 1
    if best > 0:
        return cut[:best].rstrip() + " …"
    cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + " …"


def make_snippet(markdown: str, limit: int = 280) -> str:
    """Extract a short readable snippet from a report's markdown body."""
    if not markdown:
        return ""
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line or _HEADING_OR_FENCE_OR_TABLE.match(line) or _LIST_OR_HR.match(line):
            continue
        if line.startswith("![") or line.startswith("<"):
            continue
        cleaned = _clean_inline(line)
        if len(cleaned) < 40:
            continue
        return _truncate(cleaned, limit)
    return ""
