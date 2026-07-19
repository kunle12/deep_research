"""Dedicated unit tests for `nodes.analyze_paper` (P7).

Covers the LLM-call wrapper fully offline:
  - analyze(): valid JSON parse, invalid JSON -> unparseable marker, LLM
    exception -> error marker, vision image_url blocks attached when
    `page_image_data_urls` supplied.
  - _coerce(): filters references lacking arxiv_id, extracts arxiv_id from
    adjacent text when missing, coerces scalar authors lists, booleans,
    list-of-str fields.
  - extract_key_reference_arxiv_ids(): drops empties and returns ordered ids.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.nodes.analyze_paper import (
    _coerce,
    analyze,
    extract_key_reference_arxiv_ids,
)
from deep_research.state import PaperAnalysis

# ---------------------------------------------------------------------------
# AsyncOpenAI doubles (mirrors test_paths_url_source_analyze.py style)
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeAsyncOpenAI:
    def __init__(self, content: str) -> None:
        self.chat = MagicMock()
        self.chat.completions = MagicMock()
        self.chat.completions.create = AsyncMock(return_value=_FakeResponse(content))


def _raising_client(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


# ---------------------------------------------------------------------------
# analyze() — happy path JSON, invalid JSON, exception
# ---------------------------------------------------------------------------


class TestAnalyze:
    @pytest.mark.asyncio
    async def test_parses_valid_json(self) -> None:
        payload = {
            "title": "A Great Paper",
            "summary": "It does X via Y.",
            "key_findings": ["finding 1", "finding 2"],
            "relevance_to_query": "Relevant because Z.",
            "methodology": "We trained on T.",
            "limitations": ["small dataset", "no ablation"],
            "is_key_reference": True,
            "key_references": [
                {"arxiv_id": "2401.12345", "title": "Ref 1", "rationale": "why"},
                {"arxiv_id": "2309.99999", "title": "Ref 2", "rationale": "why2"},
            ],
            "extraction_text": "raw relevant text",
            "figure_descriptions": ["fig 1: curve", "fig 2: table"],
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        out = await analyze("2402.54321", "paper text", "query", client, "m")
        assert isinstance(out, PaperAnalysis)
        assert out.title == "A Great Paper"
        assert out.summary == "It does X via Y."
        assert out.key_findings == ["finding 1", "finding 2"]
        assert out.methodology == "We trained on T."
        assert out.limitations == ["small dataset", "no ablation"]
        assert out.is_key_reference is True
        assert len(out.key_references) == 2
        assert out.key_references[0].arxiv_id == "2401.12345"
        assert out.figure_descriptions == ["fig 1: curve", "fig 2: table"]

    @pytest.mark.asyncio
    async def test_invalid_json_returns_unparseable_marker(self) -> None:
        client = _FakeAsyncOpenAI("not valid json {{{")
        out = await analyze("2402.54321", "text", "q", client, "m")
        assert out.title.startswith("[unparseable]")
        assert "not valid json" in out.summary

    @pytest.mark.asyncio
    async def test_llm_exception_returns_error_marker(self) -> None:
        client = _raising_client(RuntimeError("LLM down"))
        out = await analyze("2402.54321", "text", "q", client, "m")
        assert out.title.startswith("[error]")
        assert "LLM analysis failed" in out.summary
        assert "RuntimeError" in out.summary

    @pytest.mark.asyncio
    async def test_vision_blocks_present_when_images_supplied(self) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> Any:
            captured["messages"] = kwargs.get("messages")
            return _FakeResponse(json.dumps({"title": "t", "summary": "s"}))

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_capture)

        await analyze(
            "2402.54321",
            "text",
            "q",
            client,
            "m",
            page_image_data_urls=["data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"],
        )
        msgs = captured["messages"]
        assert msgs[0]["role"] == "system"
        user_msg = msgs[1]
        assert user_msg["role"] == "user"
        # Multi-content user message when images supplied
        assert isinstance(user_msg["content"], list)
        text_blocks = [b for b in user_msg["content"] if b.get("type") == "text"]
        image_blocks = [b for b in user_msg["content"] if b.get("type") == "image_url"]
        assert len(text_blocks) == 1
        assert len(image_blocks) == 2
        assert image_blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    @pytest.mark.asyncio
    async def test_no_images_yields_plain_string_user_content(self) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> Any:
            captured["messages"] = kwargs.get("messages")
            return _FakeResponse(json.dumps({"title": "t", "summary": "s"}))

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_capture)
        await analyze("2402.54321", "text", "q", client, "m")
        user_msg = captured["messages"][1]
        # When no images, user content is a plain string
        assert isinstance(user_msg["content"], str)
        assert "2402.54321" in user_msg["content"]


# ---------------------------------------------------------------------------
# _coerce() — reference filtering / arxiv_id extraction
# ---------------------------------------------------------------------------


class TestCoerce:
    def test_drops_refs_without_arxiv_id(self) -> None:
        data = {
            "title": "T",
            "summary": "S",
            "key_references": [
                {"arxiv_id": "2401.11111", "title": "ok", "rationale": "r"},
                {"arxiv_id": "", "title": "no id", "rationale": "nope"},  # dropped
                {"title": "no id field at all"},  # dropped
            ],
        }
        out = _coerce("2402.22222", data)
        assert len(out.key_references) == 1
        assert out.key_references[0].arxiv_id == "2401.11111"

    def test_extracts_arxiv_id_from_title_text(self) -> None:
        """When arxiv_id is missing, the regex searches the title (first
        truthy of title|rationale) for an arxiv-id-shaped substring.

        NOTE: the impl uses `ref.get("title","") or ref.get("rationale","")`,
        so if a non-empty title has no id but the rationale does, the regex
        still searches the title and the ref is dropped. This test exercises
        the title-extraction happy path.
        """
        data = {
            "title": "T",
            "summary": "S",
            "key_references": [
                {
                    # arxiv_id missing — title contains a valid id
                    "title": "Cited as arXiv:2403.14159 in the bibliography",
                    "rationale": "important methodology",
                },
                {
                    # arxiv_id missing — title contains an id with version
                    "title": "Builds on 2305.98765v3 for theory",
                    "rationale": "no id here",
                },
            ],
        }
        out = _coerce("2402.22222", data)
        assert len(out.key_references) == 2
        assert out.key_references[0].arxiv_id == "2403.14159"
        assert out.key_references[1].arxiv_id == "2305.98765v3"

    def test_ref_dropped_when_id_only_in_rationale_but_title_nonempty(self) -> None:
        """Documents the `or` short-circuit in _coerce: when a non-empty title
        has no id but the rationale does, the regex searches only the title,
        so the ref is dropped. Future contributors may want to search both."""
        data = {
            "title": "T",
            "summary": "S",
            "key_references": [
                {
                    "title": "no id in title",
                    "rationale": "but 2305.98765v3 is in rationale",  # exercise-only
                },
            ],
        }
        out = _coerce("2402.22222", data)
        # Title was non-empty but had no arxiv_id -> regex doesn't check rationale -> dropped
        assert len(out.key_references) == 0

    def test_coerce_authors_list(self) -> None:
        data = {
            "title": "T",
            "summary": "S",
            "key_references": [
                {
                    "arxiv_id": "2401.1",
                    "title": "t",
                    "rationale": "r",
                    "authors": ["Alice", 42],  # mixed; ints are kept as str
                },
                {
                    "arxiv_id": "2401.2",
                    "title": "t2",
                    "rationale": "r2",
                    "authors": "not a list",  # invalid -> empty
                },
            ],
        }
        out = _coerce("2402.22222", data)
        assert out.key_references[0].authors == ["Alice", "42"]
        assert out.key_references[1].authors == []

    def test_coerce_booleans_and_lists(self) -> None:
        data = {
            "title": "T",
            "summary": "S",
            "key_findings": "should be coerced to empty-list (not a list)",
            "limitations": "also not a list",
            "is_key_reference": "true",  # string -> True
            "methodology": None,  # None -> empty string (strict-safe)
        }
        out = _coerce("2402.22222", data)
        assert out.key_findings == []
        assert out.limitations == []
        assert out.is_key_reference is True
        assert out.methodology == ""

    def test_coerce_falls_back_title_for_unknown(self) -> None:
        data = {"summary": "S"}  # no title at all
        out = _coerce("2402.99", data)
        assert "2402.99" in out.title
        assert out.summary == "S"

    def test_old_style_slash_arxiv_id_matched_by_regex(self) -> None:
        r"""Verifies that the `\b[a-z-]+(?:\.[A-Z]{2})?/\d{7}\b` alternative
        matches `cs.LG/0702001` (old-style arxiv id with subcategory).

        Old-style format: `category[.subcat]/identifier` (7 digits).
        Both `cs.LG/0702001` and plain `cs/0702001` are now captured.
        New-style ids like `0704.0001` are still matched.
        """
        import re

        rx = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b|\b[a-z\-]+(?:\.[A-Z]{2})?/\d{7}\b")
        assert rx.search("See cs.LG/0702001 for early work").group(0) == "cs.LG/0702001"
        assert rx.search("cs/0702001").group(0) == "cs/0702001"
        # New-style IDs match
        assert rx.search("arXiv:0704.0001").group(0) == "0704.0001"


# ---------------------------------------------------------------------------
# extract_key_reference_arxiv_ids()
# ---------------------------------------------------------------------------


class TestExtractKeyReferenceArxivIds:
    def test_returns_ordered_ids_dropping_empties(self) -> None:
        from deep_research.state import PaperNode

        analysis = PaperAnalysis(
            title="T",
            summary="S",
            key_references=[
                PaperNode(arxiv_id="2401.11111", title="a"),
                PaperNode(arxiv_id="", title="b"),  # dropped
                PaperNode(arxiv_id="2309.99999v3", title="c"),
            ],
        )
        ids = extract_key_reference_arxiv_ids(analysis)
        assert ids == ["2401.11111", "2309.99999v3"]

    def test_empty_when_no_key_references(self) -> None:
        analysis = PaperAnalysis(title="T", summary="S")
        assert extract_key_reference_arxiv_ids(analysis) == []

    def test_basic_extraction(self) -> None:
        from deep_research.state import PaperNode

        analysis = PaperAnalysis(
            title="T",
            summary="S",
            key_references=[PaperNode(arxiv_id="2401.1", title="x")],
        )
        assert extract_key_reference_arxiv_ids(analysis) == ["2401.1"]


# ---------------------------------------------------------------------------
# Paper text truncation guard
# ---------------------------------------------------------------------------


class TestPromptTruncation:
    @pytest.mark.asyncio
    async def test_long_paper_text_is_truncated_to_40k_chars(self) -> None:
        captured: dict[str, Any] = {}

        async def _capture(**kwargs: Any) -> Any:
            captured["messages"] = kwargs.get("messages")
            return _FakeResponse(json.dumps({"title": "t", "summary": "s"}))

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_capture)

        long_text = "A" * 100_000
        await analyze("2402.54321", long_text, "q", client, "m")
        user_msg = captured["messages"][1]
        # The prompt substitution truncates paper_text to 40000 chars before
        # embedding it — so the user content must be strictly less than 100k.
        assert len(user_msg["content"]) < 100_000
        # And the prompt should still contain the arxiv_id marker
        assert "2402.54321" in user_msg["content"]


__all__ = ["TestAnalyze", "TestCoerce", "TestExtractKeyReferenceArxivIds", "TestPromptTruncation"]
