"""Async OpenAI-compatible LLM client factory.

Constructs an `AsyncOpenAI` instance bound to whatever base_url the user's
running service exposes (e.g., their Qwen3.5-122B-A10B via llama.cpp, vLLM,
SGLang, Ollama, DashScope, etc.).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from openai import AsyncOpenAI

from deep_research.config import LLMConfig


class LLMClient:
    """Thin async wrapper around the AsyncOpenAI client.

    Use as an async context manager so caller controls lifetime:

        async with LLMClient(config.llm) as llm:
            resp = await llm.chat.completions.create(...)
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._client: AsyncOpenAI | None = None

    def get(self) -> AsyncOpenAI:
        if self._client is None:
            raise RuntimeError("LLMClient used outside of `async with` context")
        return self._client

    async def __aenter__(self) -> AsyncOpenAI:
        self._client = AsyncOpenAI(
            base_url=self._cfg.base_url,
            api_key=self._cfg.api_key,
            timeout=self._cfg.timeout_s,
            max_retries=2,
        )
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


@asynccontextmanager
async def open_llm(cfg: LLMConfig) -> AsyncIterator[AsyncOpenAI]:
    """Standalone context manager version for ad-hoc use."""
    async with LLMClient(cfg) as client:
        yield client


__all__ = ["LLMClient", "open_llm"]
