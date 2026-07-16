"""BibTeX renderer - emit .bib file content from a citation graph (academic mode)."""

from __future__ import annotations

from deep_research.citations import render_bibtex
from deep_research.state import Report


def render_report_bibtex(report: Report) -> str:
    if report.citation_graph is None:
        return ""
    return render_bibtex(report.citation_graph)


__all__ = ["render_report_bibtex"]
