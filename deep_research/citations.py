"""Citation helpers — dedup, bibliography formatting, BibTeX export.

The `Citation` model itself lives in `state.py`; this module provides
the post-processing utilities.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from deep_research.state import Citation, CitationGraph, PaperNode

# A reasonable arxiv-id regex. Matches:
#   2401.12345   2401.12345v3   2401.12345v12   1234.56789   cs.LG/0702001
_ARXIV_RX = re.compile(
    r"""
    (?:
        \b(\d{4}\.\d{4,5}(?:v\d+)?)\b
        |
        \b([a-z\-]+/[A-Z]{2}\.\d{7})\b
    )
    """,
    re.VERBOSE,
)


def extract_arxiv_ids(text: str) -> list[str]:
    """Return all arxiv IDs found in `text`, preserving order, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _ARXIV_RX.finditer(text):
        aid = m.group(1) or m.group(2)
        if aid and aid not in seen:
            seen.add(aid)
            out.append(aid)
    return out


def dedup_citations(citations: list[Citation]) -> list[Citation]:
    """Dedup by URL, keeping highest-confidence variant."""
    out: OrderedDict[str, Citation] = OrderedDict()
    for c in citations:
        key = c.url
        existing = out.get(key)
        if existing is None or existing.confidence_score < c.confidence_score:
            out[key] = c
    return list(out.values())


def render_bibliography_markdown(citations: list[Citation]) -> str:
    """Render a bibliography section."""
    if not citations:
        return ""
    lines = ["## Bibliography", ""]
    for i, c in enumerate(citations, start=1):
        title = c.title or c.url
        snippet = c.snippet.strip().replace("\n", " ")
        if snippet:
            snippet = f" — {snippet[:160]}"
        lines.append(f"{i}. [{title}]({c.url}){snippet}")
    return "\n".join(lines) + "\n"


def render_citation_graph_markdown(graph: CitationGraph) -> str:
    """Render the academic-mode citation graph as a nested markdown list."""
    if not graph.nodes:
        return ""
    # Find roots: nodes with parent_arxiv_id is None
    roots = [n for n in graph.nodes.values() if n.parent_arxiv_id is None]
    if not roots:
        # Fall back to all depth-0 nodes
        roots = [n for n in graph.nodes.values() if n.depth == 0]
        if not roots:
            roots = list(graph.nodes.values())

    def _render(node: PaperNode, depth: int, lines: list[str]) -> None:
        indent = "  " * depth
        bullet = "-" if depth == 0 else "└─"
        title = node.title or node.arxiv_id
        line = f"{indent}{bullet} [{title} (arxiv:{node.arxiv_id})](https://arxiv.org/abs/{node.arxiv_id})"
        if node.rationale:
            line += f" — _{node.rationale}_"
        lines.append(line)
        for child_id in graph.edges.get(node.arxiv_id, []):
            child = graph.nodes.get(child_id)
            if child:
                _render(child, depth + 1, lines)

    lines = ["## Citation Graph", ""]
    for root in roots:
        _render(root, 0, lines)
    return "\n".join(lines) + "\n"


def render_bibtex(graph: CitationGraph) -> str:
    """Emit a .bib file content from the citation graph."""
    if not graph.nodes:
        return ""
    entries: list[str] = []
    for node in graph.nodes.values():
        key = _bibtex_key(node)
        authors = " and ".join(node.authors) if node.authors else "Anonymous"
        title = _bibtex_escape(node.title or "Untitled")
        entries.append(
            "@article{" + key + ",\n"
            "  title = {" + title + "},\n"
            "  author = {" + authors + "},\n"
            "  eprint = {" + node.arxiv_id + "},\n"
            "  archivePrefix = {arXiv},\n"
            "  url = {https://arxiv.org/abs/" + node.arxiv_id + "}\n"
            "}\n"
        )
    return "\n".join(entries)


def _bibtex_key(node: PaperNode) -> str:
    """Stable BibTeX key from author + arxiv id."""
    if node.authors:
        first_author_last = node.authors[0].split()[-1].lower()
        # strip non-alnum
        first_author_last = re.sub(r"[^a-z0-9]", "", first_author_last) or "anon"
    else:
        first_author_last = "anon"
    aid = re.sub(r"[^a-z0-9]", "", node.arxiv_id.lower())
    return f"{first_author_last}{aid}"


def _bibtex_escape(s: str) -> str:
    return s.replace("{", "\\{").replace("}", "\\}")


__all__ = [
    "dedup_citations",
    "extract_arxiv_ids",
    "render_bibliography_markdown",
    "render_bibtex",
    "render_citation_graph_markdown",
]
