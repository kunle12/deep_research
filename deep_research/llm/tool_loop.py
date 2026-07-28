"""Async tool-calling loop — raw, no LangChain.

Implements:
1. A tool registry: `name -> async_callable(**kwargs) -> ToolResult`
2. A run loop: dispatches tool calls requested by the LLM, concurrently
   via `asyncio.gather`, up to `max_turns`.
3. Context management: when total tokens exceed 75% of `max_context_tokens`,
   older turns are summarised into a single compressed message to stay
   within the model's context window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable
from typing import Any, Protocol

import tiktoken

from deep_research.state import Citation

logger = logging.getLogger(__name__)

_DEFAULT_TOOL_TIMEOUT_S: float = 120.0


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
        # Per-tool-call hard-kill timeout. Prevents a single slow tool
        # (e.g. fetch_page on a hung server, browser_navigate on a JS-heavy
        # page) from monopolising the researcher's overall time budget.
        # Override via `set_tool_timeout()`; default is conservative.
        self._tool_timeout_s: float = _DEFAULT_TOOL_TIMEOUT_S

    def set_tool_timeout(self, seconds: float) -> None:
        """Override the per-tool-call hard timeout. Use `float("inf")` to disable."""
        self._tool_timeout_s = seconds

    async def close(self) -> None:
        """Close any async resources held by tools (e.g., browser MCP)."""
        for hook in self._close_hooks:
            try:
                await hook()
            except Exception as e:
                logger.debug("tool close hook raised: %s: %s", type(e).__name__, e)

    def add_close_hook(self, hook) -> None:
        """Register an async callable to run during close()."""
        self._close_hooks.append(hook)

    def register(
        self,
        name: str,
        func: ToolFunc,
        schema: dict,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        # OpenAI API expects `type` at top level and `function` wrapper
        # containing `name`, `description`, `parameters`.
        wrapped = {
            "type": "function",
            "function": {
                "name": name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {}),
            },
        }
        self._tools[name] = func
        self._schemas.append(wrapped)

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

        async def _guarded() -> ToolResult:
            # Inner timeout protects a single tool call from infinite hangs.
            # We do not shield the coroutine: the goal is to propagate
            # CancelledError into the tool so it can abort pending I/O
            # promptly, even if the tool wraps sync work in run_in_executor
            # (the executor task won't be cancelled, but the awaiting
            # coroutine unblocks immediately and the timeout is reported).
            if self._tool_timeout_s == float("inf"):
                return await _run()
            try:
                async with asyncio.timeout(self._tool_timeout_s):
                    return await _run()
            except TimeoutError:
                logger.warning(
                    "tool %s exceeded per-call timeout %.1fs",
                    name,
                    self._tool_timeout_s,
                )
                return ToolResult(
                    content="",
                    error=f"tool {name} timed out after {self._tool_timeout_s:.1f}s",
                )

        if self._semaphore is not None:
            async with self._semaphore:
                return await _guarded()
        return await _guarded()


class ScopedToolRegistry:
    """Wraps a parent ToolRegistry, adding per-scope tools.

    Used by the researcher to inject the `refine` tool without mutating
    the shared registry (which would raise on duplicate registration
    when multiple researchers run in parallel).
    """

    def __init__(self, parent: ToolRegistry) -> None:
        self._parent = parent
        self._extra_tools: dict[str, ToolFunc] = {}
        self._extra_schemas: list[dict] = []

    def register(self, name: str, func: ToolFunc, schema: dict) -> None:
        wrapped = {
            "type": "function",
            "function": {
                "name": name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {}),
            },
        }
        self._extra_tools[name] = func
        self._extra_schemas.append(wrapped)

    def names(self) -> list[str]:
        return self._parent.names() + list(self._extra_tools)

    def schemas(self) -> list[dict]:
        return self._parent.schemas() + self._extra_schemas

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name in self._extra_tools:
            try:
                return await self._extra_tools[name](**arguments)
            except Exception as e:
                logger.exception("scoped tool %s raised", name)
                return ToolResult(content="", error=f"{type(e).__name__}: {e}")
        return await self._parent.call(name, arguments)


# ---------------------------------------------------------------------------
# Context management helpers
# ---------------------------------------------------------------------------


def _encoding_for_model(model: str):
    """Return a tiktoken Encoding object roughly matching *model*."""
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def _token_count(messages: list[dict], model: str) -> int:
    """Rough token count of the message list using tiktoken."""
    enc = _encoding_for_model(model)
    total = 2  # <|start|> overhead
    for m in messages:
        total += 4  # per-message framing overhead
        for _, v in m.items():
            if isinstance(v, str):
                total += len(enc.encode(v))
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        for sv in item.values():
                            if isinstance(sv, str):
                                total += len(enc.encode(sv))
    return total


async def _summarize_turns(
    client,
    model: str,
    messages_to_summarize: list[dict],
    max_summary_tokens: int = 2048,
) -> str:
    """Ask the LLM to compress a sequence of conversation turns into a short
    summary paragraph that preserves all key findings, evidence, and sources."""
    summary_messages = [
        {
            "role": "system",
            "content": (
                "You are a conversation summariser. Condense the following "
                "research conversation turns into a single concise paragraph "
                "that preserves every important finding, citation URL, "
                "source title, and data point. Omit tool-call mechanics; "
                "keep only the substance."
            ),
        },
        {
            "role": "user",
            "content": (
                "Summarise the following conversation turns:\n\n"
                + "\n\n".join(
                    f"--- {m.get('role', '?')} ---\n{m.get('content', '(no text)')}"
                    for m in messages_to_summarize
                )
            ),
        },
    ]
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=summary_messages,
            max_tokens=max_summary_tokens,
            temperature=0.3,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning("context summarization failed: %s — keeping original messages", e)
        return ""


async def _maybe_summarise(
    client,
    messages: list[dict],
    model: str,
    max_context_tokens: int,
) -> list[dict]:
    """If total token usage exceeds 75 % of *max_context_tokens*, compress
    older turns into a single summary message injected as a 'user' role.

    The system message (index 0) is always preserved as-is. The *last 3*
    conversational exchanges (assistant + tool messages) are kept intact.
    Everything before that is summarised.
    """
    if max_context_tokens <= 0:
        return messages
    total = _token_count(messages, model)
    threshold = int(max_context_tokens * 0.75)
    if total <= threshold:
        return messages  # no summarization needed

    logger.info(
        "context %.1f%% of %d — summarising older turns",
        total / max_context_tokens * 100,
        max_context_tokens,
    )

    # Find boundaries:
    #   [system, user, assistant, tool, assistant, tool, ...]
    # Keep system, then keep last 3 exchanges, summarise the rest.
    system = messages[:1] if messages and messages[0]["role"] == "system" else []
    body = messages[len(system) :]

    # Walk backwards collecting up to 3 exchanges. An "exchange" is:
    #   user | assistant  +  all subsequent tool messages that follow it.
    # We keep the last 3 *complete* exchanges (assistant + its tool chain).
    kept: list[dict] = []
    i = len(body) - 1
    exchange_count = 0
    while i >= 0 and exchange_count < 3:
        # Skip standalone tool messages (they belong to the exchange
        # already captured by the assistant message ahead of them).
        if body[i]["role"] == "tool":
            i -= 1
            continue
        # Found an assistant or user message — this starts an exchange.
        # Include it and any tool messages immediately before it.
        end = i + 1
        start = i
        # Include preceding tool messages that belong to this exchange
        while start > 0 and body[start - 1]["role"] == "tool":
            start -= 1
        # Insert this exchange block at the front of kept
        kept[0:0] = body[start:end]
        exchange_count += 1
        i = start - 1

    to_summarise = body[: len(body) - len(kept)]
    if not to_summarise:
        return messages

    summary_text = await _summarize_turns(client, model, to_summarise)
    if not summary_text:
        # Summarisation failed — keep original messages unchanged
        return messages

    # Rebuild: system + summary (user) + kept messages
    rebuilt = list(system)
    rebuilt.append(
        {
            "role": "user",
            "content": (
                "[EARLIER RESEARCH SUMMARY]\n"
                + summary_text
                + "\n\n(Continue research using the full conversation below.)"
            ),
        }
    )
    rebuilt.extend(kept)
    return rebuilt


async def run_with_tools(
    client,  # openai.AsyncOpenAI
    messages: list[dict],
    tools: ToolRegistry,
    model: str,
    max_turns: int = 10,
    extra: dict | None = None,
    max_context_tokens: int = 131072,
) -> tuple[list[dict], list[Citation]]:
    """Run a chat-completions loop, dispatching any tool calls in parallel.

    *max_context_tokens* — maximum model context window; when total tokens
    reach 75 % of this value, older turns are summarised into a single
    compressed message.

    Returns the final message list (including assistant + tool messages)
    and the union of all citations surfaced by tool calls.
    """
    citations: list[Citation] = []
    messages = list(messages)
    extra = extra or {}

    # Apply context management before the first turn too, in case the
    # initial system + user prompt already fills a large portion of the
    # context window.
    messages = await _maybe_summarise(client, messages, model, max_context_tokens)

    for _turn in range(max_turns):
        turn_t0 = time.monotonic()
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools.schemas() or None,
            **extra,
        )
        msg = resp.choices[0].message
        llm_ms = (time.monotonic() - turn_t0) * 1000.0

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
            logger.debug(
                "tool_loop turn %d/%d: llm=%.0fms; no tool calls — finishing",
                _turn + 1,
                max_turns,
                llm_ms,
            )
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
            if not isinstance(args, dict):
                logger.warning("tool %s: arguments are not a dict: %r", name, type(args).__name__)
                args = {}
            tasks.append((tc, args, tools.call(name, args)))

        if tasks:
            tool_t0 = time.monotonic()
            results = await asyncio.gather(*[t[2] for t in tasks])
            tool_ms = (time.monotonic() - tool_t0) * 1000.0
            tool_names = ", ".join(t[0].function.name for t in tasks)
            logger.info(
                "tool_loop turn %d/%d: llm=%.0fms tools=%.0fms (%d call(s): %s)",
                _turn + 1,
                max_turns,
                llm_ms,
                tool_ms,
                len(tasks),
                tool_names,
            )
        else:
            results = []

        for (tc, _args, _), result in zip(tasks, results):
            citations.extend(result.citations)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result.to_json(),
                }
            )

        # Check context after tool results are appended
        messages = await _maybe_summarise(client, messages, model, max_context_tokens)

    logger.warning("tool loop exceeded max_turns=%d", max_turns)
    return messages, citations


__all__ = ["ScopedToolRegistry", "ToolFunc", "ToolRegistry", "ToolResult", "run_with_tools"]
