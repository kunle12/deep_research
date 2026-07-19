"""Async tool-calling loop — raw, no LangChain.

Implements:
1. A tool registry: `name -> async_callable(**kwargs) -> ToolResult`
2. A run loop: dispatches tool calls requested by the LLM, concurrently
   via `asyncio.gather`, up to `max_turns`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from typing import Any, Protocol

from deep_research.state import Citation

logger = logging.getLogger(__name__)


class ToolResult:
    """Standard return shape for tool calls.

    - `content` becomes the string the LLM sees in the tool role message.
    - `citations` are absorbed by the agent's state.
    """

    def __init__(
        self,
        content: str,
        citations: list[Citation] | None = None,
        error: str | None = None,
    ) -> None:
        self.content = content
        self.citations = citations or []
        self.error = error

    def to_json(self) -> str:
        # The OpenAI tool-message role expects a string content; we send JSON.
        payload: dict[str, Any] = {"content": self.content}
        if self.error:
            payload["error"] = self.error
        return json.dumps(payload, ensure_ascii=False)


class ToolFunc(Protocol):
    def __call__(self, **kwargs: Any) -> Awaitable[ToolResult]: ...


class ToolRegistry:
    """Async registry of named tools + their JSON schemas."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolFunc] = {}
        self._schemas: list[dict] = []
        self._semaphore: asyncio.Semaphore | None = None  # set by agent
        self.writer: Any | None = None  # optional LibraryWriter for tool-side archival
        self._close_hooks: list = []  # async callables to run during close()

    async def close(self) -> None:
        """Close any async resources held by tools (e.g., browser MCP)."""
        for hook in self._close_hooks:
            try:
                await hook()
            except Exception as e:
                logger.debug("tool close hook raised: %s: %s", type(e).__name__, e)

    def register(
        self,
        name: str,
        func: ToolFunc,
        schema: dict,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        schema = {**schema, "name": name}
        self._tools[name] = func
        self._schemas.append(schema)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def schemas(self) -> list[dict]:
        return list(self._schemas)

    def set_concurrency(self, n: int) -> None:
        self._semaphore = asyncio.Semaphore(n)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        func = self._tools.get(name)
        if func is None:
            return ToolResult(content="", error=f"unknown tool: {name}")
        # Run under the registry-wide semaphore if configured
        async def _run() -> ToolResult:
            try:
                return await func(**arguments)
            except Exception as e:
                logger.exception("tool %s raised", name)
                return ToolResult(content="", error=f"{type(e).__name__}: {e}")

        if self._semaphore is not None:
            async with self._semaphore:
                return await _run()
        return await _run()


async def run_with_tools(
    client,  # openai.AsyncOpenAI
    messages: list[dict],
    tools: ToolRegistry,
    model: str,
    max_turns: int = 10,
    extra: dict | None = None,
) -> tuple[list[dict], list[Citation]]:
    """Run a chat-completions loop, dispatching any tool calls in parallel.

    Returns the final message list (including assistant + tool messages)
    and the union of all citations surfaced by tool calls.
    """
    citations: list[Citation] = []
    messages = list(messages)
    extra = extra or {}

    for _turn in range(max_turns):
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools.schemas() or None,
            **extra,
        )
        msg = resp.choices[0].message

        # Append assistant message — must be reconstructed as a dict
        # to preserve tool_calls for subsequent rounds.
        assistant_record: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_record["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_record)

        if not msg.tool_calls:
            return messages, citations

        # Dispatch all tool_calls concurrently via asyncio.gather.
        tasks = []
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                logger.warning("tool %s: malformed JSON arguments: %r", name, tc.function.arguments)
                args = {}
            tasks.append((tc, args, tools.call(name, args)))

        results = await asyncio.gather(*[t[2] for t in tasks])

        for (tc, _args, _), result in zip(tasks, results):
            citations.extend(result.citations)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result.to_json(),
                }
            )

    logger.warning("tool loop exceeded max_turns=%d", max_turns)
    return messages, citations


__all__ = ["ToolFunc", "ToolRegistry", "ToolResult", "run_with_tools"]
