"""Tests for citation URL matching helpers."""

from __future__ import annotations

from deep_research.citations import (
    extract_urls_from_markdown,
    filter_citations_to_referenced,
    normalize_url,
    render_bibliography_markdown,
    render_bibtex,
    render_citation_graph_markdown,
)
from deep_research.state import Citation, CitationGraph, PaperNode


class TestNormalizeUrl:
    def test_strips_autolink_delimiters(self) -> None:
        assert normalize_url("<https://Example.com/X/>") == "https://example.com/x"

    def test_strips_brackets_and_punctuation(self) -> None:
        assert normalize_url("https://example.com/x],") == "https://example.com/x"
        assert normalize_url("https://example.com/x.") == "https://example.com/x"
        assert normalize_url("https://example.com/x)") == "https://example.com/x"

    def test_strips_trailing_slash(self) -> None:
        assert normalize_url("https://example.com/x/") == "https://example.com/x"


class TestExtractUrlsFromMarkdown:
    def test_handles_common_markdown_styles(self) -> None:
        md = (
            "See <https://a.example/x> and [https://b.example/y] and "
            "[label](https://c.example/z) plus bare https://d.example/q."
        )
        urls = extract_urls_from_markdown(md)
        assert set(urls) == {
            "https://a.example/x",
            "https://b.example/y",
            "https://c.example/z",
            "https://d.example/q",
        }

    def test_preserves_original_text(self) -> None:
        urls = extract_urls_from_markdown("Source: <https://A.example/Doc>")
        assert urls == {"https://a.example/doc": "https://A.example/Doc"}

    def test_empty_text(self) -> None:
        assert extract_urls_from_markdown("") == {}


class TestRenderBibliography:
    def test_empty_returns_empty(self) -> None:
        assert render_bibliography_markdown([]) == ""

    def test_renders_entries_with_snippets(self) -> None:
        cits = [
            Citation(url="https://a.example/x", title="A", snippet="line1\nline2"),
            Citation(url="https://b.example/y", title=""),
        ]
        out = render_bibliography_markdown(cits)
        assert "## Bibliography" in out
        assert "[A](https://a.example/x)" in out
        assert "line1 line2" in out
        assert "[https://b.example/y](https://b.example/y)" in out


class TestRenderCitationGraph:
    def test_empty_graph_returns_empty(self) -> None:
        assert render_citation_graph_markdown(CitationGraph()) == ""

    def test_renders_scholar_node_with_rationale_and_edges(self) -> None:
        graph = CitationGraph()
        parent = PaperNode(arxiv_id="2401.00001", title="Parent", rationale="why", depth=0)
        scholar = PaperNode(
            arxiv_id="scholar:abc",
            title="Scholar Hit",
            url="https://paper.example/x",
            parent_arxiv_id="2401.00001",
            depth=1,
        )
        graph.add_node(parent)
        graph.add_node(scholar)
        graph.add_edge("2401.00001", "scholar:abc")
        out = render_citation_graph_markdown(graph)
        assert "Parent" in out
        assert "why" in out
        assert "[Scholar Hit](https://paper.example/x)" in out

    def test_falls_back_when_no_roots(self) -> None:
        graph = CitationGraph()
        graph.add_node(PaperNode(arxiv_id="2401.00001", title="A", depth=1))
        out = render_citation_graph_markdown(graph)
        assert "A" in out


class TestRenderBibtex:
    def test_empty_graph_returns_empty(self) -> None:
        assert render_bibtex(CitationGraph()) == ""

    def test_renders_article_and_scholar_entries(self) -> None:
        graph = CitationGraph()
        graph.add_node(
            PaperNode(
                arxiv_id="2401.00001",
                title="Regular Paper",
                authors=["Ada Lovelace", "Grace Hopper"],
            )
        )
        graph.add_node(
            PaperNode(
                arxiv_id="scholar:abc",
                title="Scholar {Hit}",
                url="https://paper.example/x",
                doi="10.1/abc",
                year=2024,
            )
        )
        out = render_bibtex(graph)
        assert "@article{" in out
        assert "eprint = {2401.00001}" in out
        assert "@misc{" in out
        assert "doi = {10.1/abc}" in out
        assert "year = {2024}" in out

    def test_fallback_key_without_authors(self) -> None:
        graph = CitationGraph()
        graph.add_node(PaperNode(arxiv_id="2401.00001", title="No Author"))
        out = render_bibtex(graph)
        assert "anon" in out.splitlines()[0]


class TestFilterCitationsToReferenced:
    def test_keeps_only_referenced(self) -> None:
        cits = [
            Citation(url="https://a.example/x", title="A"),
            Citation(url="https://unused.example/y", title="B"),
        ]
        kept = filter_citations_to_referenced("Report cites <https://a.example/x>.", cits)
        assert [c.url for c in kept] == ["https://a.example/x"]

    def test_no_urls_in_body_returns_empty(self) -> None:
        cits = [Citation(url="https://a.example/x", title="A")]
        assert filter_citations_to_referenced("No links here.", cits) == []

    def test_keeps_arxiv_id_referenced_citations(self) -> None:
        """Academic-mode synthesis cites papers as arxiv:ID even when the
        citation's canonical URL is a non-arxiv landing page (Scholar)."""
        md = "See _arxiv:[scholar:abc123](https://arxiv.org/abs/scholar:abc123)_"
        cits = [
            Citation(
                url="https://proceedings.mlr.press/v70/pinto17a/pinto17a.pdf",
                title="Scholar Paper",
                arxiv_id="scholar:abc123",
                source_type="scholar",
            ),
            Citation(url="https://unrelated.example/x", title="Junk"),
        ]
        kept = filter_citations_to_referenced(md, cits)
        assert [c.url for c in kept] == ["https://proceedings.mlr.press/v70/pinto17a/pinto17a.pdf"]

    def test_matches_autolink_vs_bracket_styles(self) -> None:
        cits = [Citation(url="https://a.example/x", title="A")]
        kept = filter_citations_to_referenced("Cites [https://a.example/x].", cits)
        assert len(kept) == 1
