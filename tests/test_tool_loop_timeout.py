"""Tests for the per-tool-call timeout and per-turn wall-time logging in
`ToolRegistry.call` and `run_with_tools`.

These isolation tests verify that:
- A hung tool call surfaces as a clean ToolResult.error after the configured
  per-call timeout, instead of blocking forever.
- `set_tool_timeout(float("inf"))` disables the guard (legacy escape hatch).
- `run_with_tools` does not regress when tools return quickly.
- The LLM turn path is unaffected when no tool calls are produced.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from deep_research.llm.tool_loop import _DEFAULT_TOOL_TIMEOUT_S, ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# ToolRegistry.call per-call timeout
# ---------------------------------------------------------------------------


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    return reg


async def _hung_tool(**_: Any) -> ToolResult:
    # Sleep long enough that any reasonable per-call timeout fires first.
    await asyncio.sleep(60)
    return ToolResult(content="should not be reached")


async def _fast_tool(**_: Any) -> ToolResult:
    return ToolResult(content="fast")


def test_tool_call_timeout_fires_within_budget(caplog: pytest.LogCaptureFixture) -> None:
    reg = _make_registry()
    reg.register("hung", _hung_tool, {"description": "hung", "parameters": {}})
    reg.set_tool_timeout(0.1)

    with caplog.at_level(logging.WARNING, logger="deep_research.llm.tool_loop"):
        result = asyncio.run(reg.call("hung", {}))

    assert result.content == ""
    assert result.error is not None
    assert "timed out" in result.error
    assert "hung" in result.error
    assert any("exceeded per-call timeout" in r.message for r in caplog.records)


def test_tool_call_fast_passes_unchanged() -> None:
    reg = _make_registry()
    reg.register("fast", _fast_tool, {"description": "fast", "parameters": {}})
    reg.set_tool_timeout(1.0)

    result = asyncio.run(reg.call("fast", {}))

    assert result.error is None
    assert result.content == "fast"


def test_tool_call_timeout_disabled_when_inf() -> None:
    reg = _make_registry()
    # Use a tool that returns quickly — the point is to verify the inf branch
    # doesn't trip a timeout on a fast call. (A truly hung tool under inf would
    # block; that's the documented user-selected behaviour.)
    reg.register("fast", _fast_tool, {"description": "fast", "parameters": {}})
    reg.set_tool_timeout(float("inf"))

    result = asyncio.run(reg.call("fast", {}))

    assert result.error is None
    assert result.content == "fast"


def test_default_tool_timeout_constant_is_reasonable() -> None:
    # The constant is referenced by ToolRegistry.__init__; pin its order of
    # magnitude so a future refactor doesn't accidentally set it to 1s and
    # start timing out legitimate tool I/O.
    assert 30.0 <= _DEFAULT_TOOL_TIMEOUT_S <= 600.0


# ---------------------------------------------------------------------------
# run_with_tools — confirm the loop doesn't regress with the new timing code.
# ---------------------------------------------------------------------------


class _FakeToolCall:
    def __init__(self, name: str, args: str = "{}", tc_id: str = "tc1") -> None:
        self.id = tc_id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": args})()


class _FakeMessage:
    def __init__(self, content: str | None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, msg: _FakeMessage) -> None:
        self.message = msg


class _FakeResponse:
    def __init__(self, msg: _FakeMessage) -> None:
        self.choices = [_FakeChoice(msg)]


class _FakeAsyncCompletions:
    """Minimal async OpenAI-compatible stub."""
    def __init__(self, responses: list[_FakeMessage]) -> None:
        self._responses = list(responses)
        self.create = self._create  # type: ignore[assignment]

    async def _create(self, **_: Any) -> _FakeResponse:
        return _FakeResponse(self._responses.pop(0))


class _FakeAsyncOpenAI:
    def __init__(self, responses: list[_FakeMessage]) -> None:
        self.chat = type("C", (), {"completions": _FakeAsyncCompletions(responses)})()


def test_run_with_tools_logs_turn_timing_no_regression(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One turn, no tool calls → finishes in the first iteration with a debug log."""
    client = _FakeAsyncOpenAI([_FakeMessage("final answer", tool_calls=None)])
    reg = _make_registry()

    with caplog.at_level(logging.DEBUG, logger="deep_research.llm.tool_loop"):
        msgs, cites = asyncio.run(
            # import lazily to avoid touching the module-import-time state
            _run_with_tools(client, reg, max_turns=4)
        )

    assert len(msgs) == 2  # original user + assistant
    assert cites == []
    assert any("no tool calls" in r.message for r in caplog.records)


async def _run_with_tools(client: Any, reg: ToolRegistry, max_turns: int) -> Any:
    from deep_research.llm.tool_loop import run_with_tools
    return await run_with_tools(
        client=client,
        messages=[{"role": "user", "content": "hi"}],
        tools=reg,
        model="m",
        max_turns=max_turns,
    )


def test_run_with_tools_with_one_tool_call_times_out_via_per_call() -> None:
    """Per-call timeout inside ToolRegistry surfaces a ToolResult.error
    the LLM sees as the tool response, without aborting run_with_tools."""
    reg = _make_registry()
    reg.register("hung", _hung_tool, {"description": "hung", "parameters": {}})
    reg.set_tool_timeout(0.1)

    # First turn: assistant asks for a tool call
    # Second turn: tool result with error, then assistant finishes
    first = _FakeMessage("", tool_calls=[_FakeToolCall("hung")])
    second = _FakeMessage("done", tool_calls=None)
    client = _FakeAsyncOpenAI([first, second])

    msgs, _cites = asyncio.run(_run_with_tools(client, reg, max_turns=4))

    assert any(m.get("role") == "tool" and "timed out" in (m.get("content") or "") for m in msgs)
    assert any(m.get("role") == "assistant" and m.get("content") == "done" for m in msgs)


# ---------------------------------------------------------------------------
# Deep path — asyncio.wait_for cancellation interplays cleanly with our per-call
# timeouts. Smoke-test that a hung researcher surfaces as a timeout result,
# not as a system crash.
# ---------------------------------------------------------------------------


def test_researcher_timeout_returns_timeout_error_not_hang() -> None:
    """Simulate `paths/deep.py` outer wait_for cancelling a hung sub-coroutine.
    The test verifies that the result collected via gather(return_exceptions=True)
    is a TimeoutError — i.e. the production pattern in deep.py classifies it
    correctly as "timeout" rather than a generic error, so long as the inner
    coroutine lets CancelledError propagate (the default behaviour when no
    try/except absorbs it)."""
    async def hung_researcher() -> str:
        # No try/except: lets CancelledError propagate (production behaviour).
        await asyncio.sleep(60)
        return "should-not-reach"

    async def main() -> list[Any]:
        t = asyncio.create_task(hung_researcher())
        wrapped = [asyncio.wait_for(t, timeout=0.1)]
        return await asyncio.gather(*wrapped, return_exceptions=True)

    results = asyncio.run(main())
    assert len(results) == 1
    assert isinstance(results[0], TimeoutError)
