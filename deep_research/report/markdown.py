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
from deep_research.state import Report


def render_report_markdown(report: Report, output_cfg: OutputConfig) -> str:
    """Compose the final Markdown body."""
    parts: list[str] = []
    if report.markdown:
        parts.append(report.markdown.rstrip() + "\n")

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


__all__ = ["render_report_markdown"]
