"""Markdown renderer - turns a Report into final Markdown output.

Combines:
- The agent's already-rendered markdown body
- A bibliography section (if config.output.include_citations_bibliography)
- A citation graph (academic mode, if present)
"""

from __future__ import annotations

from deep_research.citations import (
    filter_citations_to_referenced,
    render_bibliography_markdown,
    render_citation_graph_markdown,
)
from deep_research.config import OutputConfig
from deep_research.state import BlockedSource, Report


def render_blocked_sources_markdown(blocked_sources: list[BlockedSource]) -> str:
    """Render the "Unavailable Sources" appendix for skipped sources."""
    if not blocked_sources:
        return ""
    lines = [
        "## Unavailable Sources",
        "",
        "The following sources could not be retrieved automatically (bot "
        "detection, rate limiting, or fetch errors). They were skipped rather "
        "than circumvented:",
        "",
    ]
    for s in blocked_sources:
        lines.append(f"- [{s.url}]({s.url}) — {s.reason or 'fetch blocked'}")
    return "\n".join(lines) + "\n"


def render_report_markdown(report: Report, output_cfg: OutputConfig) -> str:
    """Compose the final Markdown body."""
    parts: list[str] = []
    if report.markdown:
        parts.append(report.markdown.rstrip() + "\n")

    # Defense in depth: if the report body was built without the unavailable
    # sources section (e.g. a future path sets blocked_sources but forgets to
    # append it), render it here. Deep + url_source already embed it, so the
    # marker check prevents duplication.
    if report.blocked_sources and "## Unavailable Sources" not in report.markdown:
        blocked_md = render_blocked_sources_markdown(report.blocked_sources)
        if blocked_md:
            parts.append(blocked_md)

    if report.citation_graph and report.citation_graph.nodes:
        graph_md = render_citation_graph_markdown(report.citation_graph)
        if graph_md:
            parts.append(graph_md)

    if output_cfg.include_citations_bibliography and report.citations:
        # A bibliography lists sources the report actually cites. Citations
        # that were collected during research but never referenced in the
        # final markdown body are dropped here (defense in depth — the deep
        # path already gates citations per researcher).
        cited = filter_citations_to_referenced(report.markdown, report.citations)
        if cited:
            bib_md = render_bibliography_markdown(cited)
            if bib_md:
                parts.append(bib_md)

    return "\n".join(parts).strip() + "\n"


__all__ = ["render_blocked_sources_markdown", "render_report_markdown"]
