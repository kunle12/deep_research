"""Citations JSON renderer — emit the Report's citation list as a JSON array.

Useful for the CLI's `--cite PATH` flag and for callers who want the
structured citation metadata alongside the markdown body (the markdown
renderer hides author lists + accessed_at timestamps).
"""

from __future__ import annotations

import json

from deep_research.state import Report


def render_report_citations_json(report: Report) -> str:
    """Serialize the Report's citations as a pretty JSON array."""
    return json.dumps(
        [c.model_dump(mode="json", exclude_none=True) for c in report.citations],
        indent=2,
        ensure_ascii=False,
    )


__all__ = ["render_report_citations_json"]
