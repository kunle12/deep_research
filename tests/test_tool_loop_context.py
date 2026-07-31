"""Tests for context management in `tool_loop.py`.

Covers:
  - _token_count: estimates tokens for various message shapes
  - _encoding_for_model: falls back to cl100k_base for custom models
  - _summarize_turns: calls LLM and returns summary text
  - _maybe_summarise: triggers summarisation at 75% threshold
  - _maybe_summarise: preserves system message + last 3 exchanges
  - _maybe_summarise: returns messages unchanged when below threshold
  - _maybe_summarise: handles max_context_tokens <= 0 gracefully
  - Integration: run_with_tools calls _maybe_summarise before first turn
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from deep_research.llm.tool_loop import (
    ToolRegistry,
    _encoding_for_model,
    _maybe_summarise,
    _summarize_turns,
    _token_count,
    run_with_tools,
)

# ---------------------------------------------------------------------------
# _encoding_for_model
# ---------------------------------------------------------------------------


class TestEncodingForModel:
    def test_known_model_returns_encoding(self) -> None:
        enc = _encoding_for_model("gpt-4")
        assert hasattr(enc, "encode")

    def test_custom_model_falls_back(self) -> None:
        enc = _encoding_for_model("qwen3.5-122b")
        assert enc.name == "cl100k_base"


# ---------------------------------------------------------------------------
# _token_count
# ---------------------------------------------------------------------------


class TestTokenCount:
    def test_empty_messages(self) -> None:
        count = _token_count([], "gpt-4")
        assert count == 2  # just the <|start|> overhead

    def test_single_text_message(self) -> None:
        msgs = [{"role": "user", "content": "hello world"}]
        count = _token_count(msgs, "gpt-4")
        # 4 framing + content tokens + 2 overhead
        assert count > 4

    def test_messages_with_tool_calls(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"q":"test"}'},
                    }
                ],
            }
        ]
        count = _token_count(msgs, "gpt-4")
        assert count > 4

    def test_messages_with_tool_result(self) -> None:
        msgs = [{"role": "tool", "tool_call_id": "call_1", "content": '{"content":"result"}'}]
        count = _token_count(msgs, "gpt-4")
        assert count > 4

    def test_multiple_messages_accumulate(self) -> None:
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"},
            {"role": "assistant", "content": "The capital of France is Paris."},
        ]
        single = _token_count(msgs[:1], "gpt-4")
        multi = _token_count(msgs, "gpt-4")
        assert multi > single


# ---------------------------------------------------------------------------
# _summarize_turns
# ---------------------------------------------------------------------------


class TestSummarizeTurns:
    @pytest.mark.asyncio
    async def test_returns_summary_text(self) -> None:
        client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.choices = [MagicMock()]
        fake_resp.choices[0].message = MagicMock()
        fake_resp.choices[0].message.content = "summary text here"
        client.chat.completions.create = AsyncMock(return_value=fake_resp)

        result = await _summarize_turns(
            client,
            "gpt-4",
            [{"role": "user", "content": "some conversation"}],
        )
        assert result == "summary text here"

    @pytest.mark.asyncio
    async def test_returns_empty_on_llm_error(self) -> None:
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("fail"))

        result = await _summarize_turns(
            client,
            "gpt-4",
            [{"role": "user", "content": "some conversation"}],
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_empty_response(self) -> None:
        client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.choices = [MagicMock()]
        fake_resp.choices[0].message = MagicMock()
        fake_resp.choices[0].message.content = None
        client.chat.completions.create = AsyncMock(return_value=fake_resp)

        result = await _summarize_turns(
            client,
            "gpt-4",
            [{"role": "user", "content": "some conversation"}],
        )
        assert result == ""


# ---------------------------------------------------------------------------
# _maybe_summarise
# ---------------------------------------------------------------------------


class TestMaybeSummarise:
    def test_below_threshold_returns_unchanged(self) -> None:
        msgs = [{"role": "user", "content": "short text"}]
        result = asyncio.run(_maybe_summarise(None, msgs, "gpt-4", max_context_tokens=131072))
        # Should not summarise — tokens are tiny relative to 75% of 131072
        assert result is msgs  # same list object if unchanged

    def test_zero_max_context_returns_unchanged(self) -> None:
        msgs = [{"role": "user", "content": "some text"}]
        result = asyncio.run(_maybe_summarise(None, msgs, "gpt-4", max_context_tokens=0))
        assert result is msgs

    def test_negative_max_context_returns_unchanged(self) -> None:
        msgs = [{"role": "user", "content": "some text"}]
        result = asyncio.run(_maybe_summarise(None, msgs, "gpt-4", max_context_tokens=-1))
        assert result is msgs

    def test_preserves_system_and_last_exchanges(self) -> None:
        """When summarising, system message and last 3 exchanges survive."""
        # Build a long message list that exceeds 75% of a tiny context window
        system = {"role": "system", "content": "system prompt"}
        exchange = [
            {"role": "user", "content": "X" * 500},
            {"role": "assistant", "content": "Y" * 500},
        ]
        msgs = [system]
        for _ in range(20):
            msgs.extend(exchange)

        # Mock the LLM summarizer to return a fixed summary
        import deep_research.llm.tool_loop as mod

        original = mod._summarize_turns
        mod._summarize_turns = AsyncMock(return_value="summarised content")

        result = asyncio.run(_maybe_summarise(None, msgs, "gpt-4", max_context_tokens=500))

        # System message should still be first
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "system prompt"
        # There should be a summary message
        summary_msgs = [
            m for m in result if m.get("content", "").startswith("[EARLIER RESEARCH SUMMARY]")
        ]
        assert len(summary_msgs) >= 1
        # Last exchanges should be preserved (not summarised)
        # The last assistant message should still be present
        assistant_roles = [m["role"] for m in result]
        assert assistant_roles.count("assistant") >= 1

        mod._summarize_turns = original

    def test_summary_failure_keeps_originals(self) -> None:
        """When _summarize_turns returns empty, original messages are kept."""
        system = {"role": "system", "content": "system prompt"}
        msgs = [system, {"role": "user", "content": "X" * 1000}]

        import deep_research.llm.tool_loop as mod

        original = mod._summarize_turns
        mod._summarize_turns = AsyncMock(return_value="")

        result = asyncio.run(_maybe_summarise(None, msgs, "gpt-4", max_context_tokens=500))
        # Should return original messages unchanged
        assert len(result) == len(msgs)
        mod._summarize_turns = original

    @staticmethod
    def _assert_api_valid(messages: list[dict]) -> None:
        """A rebuilt history must be sendable to the chat API:
        - every tool message's tool_call_id was declared by a *preceding*
          assistant message (no orphaned tool responses);
        - every declared tool_call_id is satisfied by exactly one following
          tool message (no assistant tool_calls left unanswered).
        """
        declared: dict[str, int] = {}
        answered: dict[str, int] = {}
        for m in messages:
            if m["role"] == "assistant" and "tool_calls" in m:
                for tc in m["tool_calls"]:
                    declared[tc["id"]] = declared.get(tc["id"], 0) + 1
            elif m["role"] == "tool":
                tid = m.get("tool_call_id", "")
                assert tid in declared, f"tool response {tid!r} precedes its requesting assistant"
                answered[tid] = answered.get(tid, 0) + 1
        assert declared == answered, f"tool_calls mismatch: declared={declared} answered={answered}"

    @pytest.mark.asyncio
    async def test_rebuilt_history_is_api_valid_mid_loop(self) -> None:
        """Mid-loop summarisation (messages end with a tool response) must not
        orphan a tool message or drop an assistant's tool responses."""
        import deep_research.llm.tool_loop as mod

        original = mod._summarize_turns
        mod._summarize_turns = AsyncMock(return_value="summarised content")

        big = "x" * 2000
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u1 " + big}]
        for k in range(6):
            msgs.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"c{k}",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"},
                        }
                    ],
                }
            )
            msgs.append({"role": "tool", "tool_call_id": f"c{k}", "content": f"r{k} " + big})

        result = await _maybe_summarise(None, msgs, "gpt-4", max_context_tokens=1000)
        self._assert_api_valid(result)
        mod._summarize_turns = original

    @pytest.mark.asyncio
    async def test_rebuilt_history_is_api_valid_post_exchange(self) -> None:
        """Summarisation after a completed exchange (messages end with a final
        assistant answer) must keep each assistant together with its tools."""
        import deep_research.llm.tool_loop as mod

        original = mod._summarize_turns
        mod._summarize_turns = AsyncMock(return_value="summarised content")

        big = "x" * 2000
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u1 " + big}]
        for k in range(6):
            msgs.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"c{k}",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"},
                        }
                    ],
                }
            )
            msgs.append({"role": "tool", "tool_call_id": f"c{k}", "content": f"r{k} " + big})
        msgs.append({"role": "assistant", "content": "final answer " + big})

        result = await _maybe_summarise(None, msgs, "gpt-4", max_context_tokens=1000)
        self._assert_api_valid(result)
        mod._summarize_turns = original


# ---------------------------------------------------------------------------
# Integration: run_with_tools calls _maybe_summarise
# ---------------------------------------------------------------------------


class TestRunWithToolsContextManagement:
    @pytest.mark.asyncio
    async def test_calls_maybe_summarise_before_first_turn(self) -> None:
        """Verify run_with_tools invokes _maybe_summarise at the start."""
        import deep_research.llm.tool_loop as mod

        original = mod._maybe_summarise
        called = False

        async def tracking_maybe_summarise(client, msgs, model, max_context_tokens):
            nonlocal called
            called = True
            return await original(client, msgs, model, max_context_tokens)

        mod._maybe_summarise = tracking_maybe_summarise

        # Mock client to return a message without tool calls
        client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.choices = [MagicMock()]
        fake_resp.choices[0].message = MagicMock()
        fake_resp.choices[0].message.content = "final answer"
        fake_resp.choices[0].message.tool_calls = None
        client.chat.completions.create = AsyncMock(return_value=fake_resp)

        reg = ToolRegistry()
        msgs = [{"role": "user", "content": "hello"}]
        await run_with_tools(
            client=client,
            messages=msgs,
            tools=reg,
            model="gpt-4",
            max_turns=2,
        )
        assert called, "_maybe_summarise was not called before the first turn"

        mod._maybe_summarise = original
