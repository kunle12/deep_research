"""Citations JSON renderer — emit the Report's citation list as a JSON array.

Useful for the CLI's `--cite PATH` flag and for callers who want the
structured citation metadata alongside the markdown body (the markdown
renderer hides author lists + accessed_at timestamps).
"""

from __future__ import annotations

import json

from deep_research.state import Citation, Report


def render_citations_json(citations: list[Citation]) -> str:
    """Serialize a list of `Citation` objects as a pretty JSON array."""
    return json.dumps(
        [c.model_dump(mode="json", exclude_none=True) for c in citations],
        indent=2,
        ensure_ascii=False,
    )


def render_report_citations_json(report: Report) -> str:
    """Convenience wrapper: emit the citations on a Report."""
    return render_citations_json(report.citations)


__all__ = ["render_citations_json", "render_report_citations_json"]
