"""Tools base — defines the Tool protocol and a registry factory.

Each tool is an *async* callable: `await tool(**kwargs) -> ToolResult`.
Tools don't talk to the LLM directly; the agent loop in `llm/tool_loop.py`
will dispatch them. Tools may register citations alongside their textual
return so the agent's state absorbs them.

Tools also expose a JSON schema dict consumed by the LLM tool_calls
contract.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol

from deep_research.llm.tool_loop import ToolResult

# Type alias for an async tool callable.
ToolCallable = Awaitable[ToolResult]


class Tool(Protocol):
    name: str
    schema: dict[str, Any]

    async def __call__(self, **kwargs: Any) -> ToolResult: ...


__all__ = ["Tool", "ToolCallable"]
