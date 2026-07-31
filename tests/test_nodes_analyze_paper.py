"""Dedicated unit tests for `nodes.analyze_paper` (P7).

Covers the LLM-call wrapper fully offline:
  - analyze(): valid JSON parse, invalid JSON -> unparseable marker, LLM
    exception -> error marker.
  - Text-only path (0 images): single synthesis call.
  - Multi-batch path with images: adaptive batching, per-batch analysis,
    final synthesis with merged results.
  - Tokenization overflow: adaptive batch-size halving.
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

from deep_research.llm.vision import is_context_overflow
from deep_research.nodes.analyze_paper import (
    _analyze_image_batch,
    _coerce,
    _synthesize_final,
    analyze,
    extract_key_reference_arxiv_ids,
)
from deep_research.state import PaperAnalysis

# ---------------------------------------------------------------------------
# Helpers
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


def _raising_client(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


def _capture_client() -> tuple[MagicMock, list[dict[str, Any]]]:
    """Return (client, call_list) where call_list captures each invocation's
    kwargs so the test can inspect messages."""
    captured: list[dict[str, Any]] = []

    async def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return _FakeResponse(json.dumps({"title": "t", "summary": "s"}))

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=_capture)
    return client, captured


# ---------------------------------------------------------------------------
# Text-only path (0 images)
# ---------------------------------------------------------------------------


class TestTextOnly:
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
    async def test_no_images_yields_plain_string_user_content(self) -> None:
        client, calls = _capture_client()
        await analyze("2402.54321", "text", "q", client, "m")
        # Text-only path makes exactly 1 call (synthesis)
        assert len(calls) == 1
        msgs = calls[0]["messages"]
        assert msgs[0]["role"] == "system"
        user_msg = msgs[1]
        assert user_msg["role"] == "user"
        assert isinstance(user_msg["content"], str)
        assert "2402.54321" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_long_paper_text_uses_full_budget(self) -> None:
        """Text-only path uses the full context budget for paper text (no
        hard 40k cap)."""
        client, calls = _capture_client()
        long_text = "A" * 100_000
        await analyze("2402.54321", long_text, "q", client, "m")
        assert len(calls) == 1
        user_msg = calls[0]["messages"][1]
        # Content should include the full paper text (no truncation to 40k)
        content = user_msg["content"]
        assert isinstance(content, str)
        # The full 100k text should be present
        assert len(content) > 90_000
        assert "2402.54321" in content


# ---------------------------------------------------------------------------
# Multi-batch path with images
# ---------------------------------------------------------------------------


class TestMultiBatch:
    @pytest.mark.asyncio
    async def test_images_below_batch_size_use_single_batch(self) -> None:
        """≤5 images → 1 batch call + 1 final synthesis = 2 LLM calls."""
        client, calls = _capture_client()
        images = [f"data:image/jpeg;base64,{i}" for i in range(3)]
        await analyze("2402.54321", "text", "q", client, "m", page_image_data_urls=images)
        # 1 batch + 1 synthesis = 2 calls
        assert len(calls) == 2, f"expected 2 calls, got {len(calls)}"

        # Batch call: should have image_url blocks
        batch_msgs = calls[0]["messages"]
        batch_uc = batch_msgs[1]["content"]
        assert isinstance(batch_uc, list)
        img_blocks = [b for b in batch_uc if b.get("type") == "image_url"]
        assert len(img_blocks) == 3

        # Synthesis call: no images, plain string content
        syn_msgs = calls[1]["messages"]
        syn_uc = syn_msgs[1]["content"]
        assert isinstance(syn_uc, str)
        assert "2402.54321" in syn_uc

    @pytest.mark.asyncio
    async def test_many_images_split_into_multiple_batches(self) -> None:
        """10 images → 2 batches of 5 + 1 synthesis = 3 LLM calls."""
        client, calls = _capture_client()
        images = [f"data:image/jpeg;base64,{i}" for i in range(10)]
        await analyze("2402.54321", "text", "q", client, "m", page_image_data_urls=images)
        # 2 batches + 1 synthesis = 3 calls
        assert len(calls) == 3, f"expected 3 calls, got {len(calls)}"

        # Both batch calls should have 5 images
        for i in range(2):
            msgs = calls[i]["messages"]
            uc = msgs[1]["content"]
            assert isinstance(uc, list)
            img_blocks = [b for b in uc if b.get("type") == "image_url"]
            assert len(img_blocks) == 5, f"batch {i} expected 5 images, got {len(img_blocks)}"

        # Synthesis call: no images
        syn_uc = calls[2]["messages"][1]["content"]
        assert isinstance(syn_uc, str)

    @pytest.mark.asyncio
    async def test_adaptive_halving_on_context_overflow(self) -> None:
        """When a batch fails with a context-length overflow, batch size is
        halved and the same images are retried."""
        call_log: list[int] = []

        class _FakeErr(Exception):
            pass

        async def _overflow_on_5(**kwargs: Any) -> Any:
            msgs = kwargs["messages"]
            uc = msgs[1]["content"]
            nimg = (
                len([b for b in uc if isinstance(b, dict) and b.get("type") == "image_url"])
                if isinstance(uc, list)
                else 0
            )
            call_log.append(nimg)
            if nimg == 5:
                raise _FakeErr("This model's maximum context length is 131072 tokens")
            return _FakeResponse(
                json.dumps({"figure_descriptions": [f"fig_{nimg}"], "extraction_text": f"t{nimg}"})
            )

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_overflow_on_5)

        images = [f"data:image/jpeg;base64,{i}" for i in range(10)]
        out = await analyze("2402.54321", "text", "q", client, "m", page_image_data_urls=images)
        # Should succeed after adaptive halving
        assert isinstance(out, PaperAnalysis)
        # Call log: first batch of 5 fails → halved to 2, then 5 batches of 2
        # + 1 final synthesis
        batch_calls = [n for n in call_log if n > 0]
        assert len(batch_calls) == 6  # 1 failed(5) + 5 succeeded(2)
        assert batch_calls[0] == 5  # first attempt with 5
        assert all(n == 2 for n in batch_calls[1:])  # all retries with 2

    @pytest.mark.asyncio
    async def test_overflow_degradation_then_skip(self) -> None:
        """When every batch size overflows, images are degraded then skipped
        one by one, and the final text-only synthesis still runs."""
        call_log: list[int] = []

        class _FakeErr(Exception):
            pass

        async def _always_overflow(**kwargs: Any) -> Any:
            msgs = kwargs["messages"]
            uc = msgs[1]["content"]
            nimg = (
                len([b for b in uc if isinstance(b, dict) and b.get("type") == "image_url"])
                if isinstance(uc, list)
                else 0
            )
            call_log.append(nimg)
            if nimg > 0:
                raise _FakeErr("This model's maximum context length is 32768 tokens")
            return _FakeResponse(json.dumps({"title": "T", "summary": "S", "relevance_score": 0.9}))

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_always_overflow)

        images = [f"data:image/jpeg;base64,{i}" for i in range(3)]
        out = await analyze("2402.54321", "text", "q", client, "m", page_image_data_urls=images)
        assert isinstance(out, PaperAnalysis)
        # All image batches fail → eventually all images skipped → synthesis runs
        batch_calls = [n for n in call_log if n > 0]
        assert len(batch_calls) > 0
        # Final synthesis call (no images) must succeed
        synth_calls = [n for n in call_log if n == 0]
        assert len(synth_calls) == 1

    @pytest.mark.asyncio
    async def test_batch_non_context_error_is_non_fatal(self) -> None:
        """A batch that fails with a non-context error (e.g. server error)
        returns empty results and advances — the final synthesis still runs."""
        call_log: list[int] = []

        async def _fail_on_5(**kwargs: Any) -> Any:
            msgs = kwargs["messages"]
            uc = msgs[1]["content"]
            nimg = (
                len([b for b in uc if isinstance(b, dict) and b.get("type") == "image_url"])
                if isinstance(uc, list)
                else 0
            )
            call_log.append(nimg)
            if nimg == 5:
                raise RuntimeError("server error")
            return _FakeResponse(
                json.dumps({"figure_descriptions": [f"fig_{nimg}"], "extraction_text": f"t{nimg}"})
            )

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_fail_on_5)

        images = [f"data:image/jpeg;base64,{i}" for i in range(10)]
        out = await analyze("2402.54321", "text", "q", client, "m", page_image_data_urls=images)
        # Should still produce a result (non-fatal error → empty batch → advance)
        assert isinstance(out, PaperAnalysis)


# ---------------------------------------------------------------------------
# _analyze_image_batch
# ---------------------------------------------------------------------------


class TestAnalyzeImageBatch:
    @pytest.mark.asyncio
    async def test_returns_figure_descriptions_and_text(self) -> None:
        payload = {
            "figure_descriptions": ["fig A", "fig B"],
            "extraction_text": "visible text",
        }
        client = _FakeAsyncOpenAI(json.dumps(payload))
        result = await _analyze_image_batch(
            "2402.54321", "text", "q", ["data:img;jpg;b64,AAA"], client, "m", 131072
        )
        assert result["figure_descriptions"] == ["fig A", "fig B"]
        assert result["extraction_text"] == "visible text"

    @pytest.mark.asyncio
    async def test_empty_response_on_invalid_json(self) -> None:
        client = _FakeAsyncOpenAI("not json")
        result = await _analyze_image_batch(
            "2402.54321", "text", "q", ["data:img;jpg;b64,AAA"], client, "m", 131072
        )
        assert result["figure_descriptions"] == []
        assert result["extraction_text"] == ""

    @pytest.mark.asyncio
    async def test_propagates_context_overflow(self) -> None:
        """Context overflow errors must propagate so the caller can halve
        batch size."""
        client = _raising_client(RuntimeError("Failed to tokenize prompt"))
        with pytest.raises(RuntimeError, match="tokenize"):
            await _analyze_image_batch(
                "2402.54321", "text", "q", ["data:img;jpg;b64,AAA"], client, "m", 131072
            )


# ---------------------------------------------------------------------------
# _synthesize_final
# ---------------------------------------------------------------------------


class TestSynthesizeFinal:
    @pytest.mark.asyncio
    async def test_merges_figure_descriptions_into_prompt(self) -> None:
        client, calls = _capture_client()
        await _synthesize_final(
            "2402.54321",
            "text",
            "q",
            ["fig A", "fig B"],
            "extra text",
            client,
            "m",
            131072,
            "pdf",
        )
        assert len(calls) == 1
        content = calls[0]["messages"][1]["content"]
        assert isinstance(content, str)
        assert "fig A" in content
        assert "fig B" in content
        assert "extra text" in content

    @pytest.mark.asyncio
    async def test_retries_with_halved_text_on_context_overflow(self) -> None:
        call_log: list[int] = []

        async def _overflow_then_ok(**kwargs: Any) -> Any:
            call_log.append(1)
            if len(call_log) == 1:
                raise RuntimeError("Failed to tokenize prompt")
            return _FakeResponse(json.dumps({"title": "t", "summary": "s"}))

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_overflow_then_ok)

        out = await _synthesize_final(
            "2402.54321",
            "A" * 100_000,
            "q",
            ["fig"],
            "",
            client,
            "m",
            131072,
            "pdf",
        )
        assert isinstance(out, PaperAnalysis)
        assert len(call_log) == 2  # 1 overflow + 1 retry

    @pytest.mark.asyncio
    async def test_fails_cleanly_on_persistent_overflow(self) -> None:
        async def _always_overflow(**kwargs: Any) -> Any:
            raise RuntimeError("Failed to tokenize prompt")

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=_always_overflow)

        out = await _synthesize_final(
            "2402.54321",
            "text",
            "q",
            [],
            "",
            client,
            "m",
            131072,
            "pdf",
        )
        assert out.title.startswith("[error]")


# ---------------------------------------------------------------------------
# is_context_overflow
# ---------------------------------------------------------------------------


class TestIsContextOverflow:
    def test_matches_tokenize_error(self) -> None:
        assert is_context_overflow(RuntimeError("Failed to tokenize prompt"))

    def test_matches_context_length(self) -> None:
        assert is_context_overflow(RuntimeError("context length exceeded"))

    def test_does_not_match_other_errors(self) -> None:
        assert not is_context_overflow(RuntimeError("connection refused"))
        assert not is_context_overflow(RuntimeError("rate limit"))


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
        data = {
            "title": "T",
            "summary": "S",
            "key_references": [
                {
                    "title": "Cited as arXiv:2403.14159 in the bibliography",
                    "rationale": "important methodology",
                },
                {
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
        data = {
            "title": "T",
            "summary": "S",
            "key_references": [
                {
                    "title": "no id in title",
                    "rationale": "but 2305.98765v3 is in rationale",
                },
            ],
        }
        out = _coerce("2402.22222", data)
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
                    "authors": ["Alice", 42],
                },
                {
                    "arxiv_id": "2401.2",
                    "title": "t2",
                    "rationale": "r2",
                    "authors": "not a list",
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
            "is_key_reference": "true",
            "methodology": None,
        }
        out = _coerce("2402.22222", data)
        assert out.key_findings == []
        assert out.limitations == []
        assert out.is_key_reference is True
        assert out.methodology == ""

    def test_coerce_falls_back_title_for_unknown(self) -> None:
        data = {"summary": "S"}
        out = _coerce("2402.99", data)
        assert "2402.99" in out.title
        assert out.summary == "S"

    def test_old_style_slash_arxiv_id_matched_by_regex(self) -> None:
        import re

        rx = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b|\b[a-z\-]+(?:\.[A-Z]{2})?/\d{7}\b")
        assert rx.search("See cs.LG/0702001 for early work").group(0) == "cs.LG/0702001"
        assert rx.search("cs/0702001").group(0) == "cs/0702001"
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
                PaperNode(arxiv_id="", title="b"),
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
# _FakeAsyncOpenAI helper (used by multiple tests)
# ---------------------------------------------------------------------------


class _FakeAsyncOpenAI:
    def __init__(self, content: str) -> None:
        self.chat = MagicMock()
        self.chat.completions = MagicMock()
        self.chat.completions.create = AsyncMock(return_value=_FakeResponse(content))


__all__ = [
    "TestAnalyzeImageBatch",
    "TestCoerce",
    "TestExtractKeyReferenceArxivIds",
    "TestIsContextOverflow",
    "TestMultiBatch",
    "TestSynthesizeFinal",
    "TestTextOnly",
]
