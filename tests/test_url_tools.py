"""URL detection + classification tests — pure-logic, no LLM calls."""

from __future__ import annotations

from deep_research.tools.url_classifier import (
    UrlType,
    classify_url_sync,
    extract_arxiv_id,
)
from deep_research.tools.url_detector import (
    extract_first_url,
    strip_url_from_query,
)


class TestUrlDetector:
    def test_basic_http_url(self) -> None:
        assert extract_first_url("see https://example.com for more") == "https://example.com"

    def test_arxiv_abs_url(self) -> None:
        assert (
            extract_first_url("see https://arxiv.org/abs/2401.12345")
            == "https://arxiv.org/abs/2401.12345"
        )

    def test_url_with_path_query_fragment(self) -> None:
        url = "https://example.com/foo/bar?x=1&y=2#frag"
        assert extract_first_url(f"link {url} after") == url

    def test_no_url_returns_none(self) -> None:
        assert extract_first_url("no link here") is None

    def test_empty_string(self) -> None:
        assert extract_first_url("") is None

    def test_multiple_urls_returns_first(self) -> None:
        text = "https://a.com and https://b.com"
        assert extract_first_url(text) == "https://a.com"

    def test_strip_url_basic(self) -> None:
        assert (
            strip_url_from_query(
                "https://arxiv.org/abs/2401.12345 summarize this",
                "https://arxiv.org/abs/2401.12345",
            )
            == "summarize this"
        )

    def test_strip_url_with_separator(self) -> None:
        assert (
            strip_url_from_query(
                "https://example.com/post what are the gaps?",
                "https://example.com/post",
            )
            == "what are the gaps?"
        )

    def test_strip_url_returns_empty(self) -> None:
        assert strip_url_from_query("https://example.com", "https://example.com") == ""


class TestUrlClassifier:
    def test_arxiv_org(self) -> None:
        assert classify_url_sync("https://arxiv.org/abs/2401.12345") == UrlType.arxiv
        assert classify_url_sync("https://arxiv.org/pdf/2401.12345") == UrlType.arxiv

    def test_pdf_by_extension(self) -> None:
        assert classify_url_sync("https://example.com/paper.pdf") == UrlType.pdf

    def test_html_default(self) -> None:
        assert classify_url_sync("https://blog.example.com/post") == UrlType.html

    def test_unknown_empty(self) -> None:
        assert classify_url_sync("") == UrlType.unknown


class TestArxivIdExtraction:
    def test_abs_url(self) -> None:
        assert extract_arxiv_id("https://arxiv.org/abs/2401.12345") == "2401.12345"

    def test_abs_url_with_version(self) -> None:
        assert extract_arxiv_id("https://arxiv.org/abs/2401.12345v3") == "2401.12345v3"

    def test_pdf_url(self) -> None:
        assert extract_arxiv_id("https://arxiv.org/pdf/2401.12345") == "2401.12345"

    def test_pdf_url_with_extension(self) -> None:
        assert extract_arxiv_id("https://arxiv.org/pdf/2401.12345v3.pdf") == "2401.12345v3"

    def test_non_arxiv_returns_none(self) -> None:
        assert extract_arxiv_id("https://example.com/post") is None
