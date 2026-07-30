"""Shared tiktoken helpers for estimating prompt token usage.

Centralised so nodes and the tool loop do not each carry their own copy of
the encoding-lookup / counting logic.
"""

from __future__ import annotations

from typing import Any

import tiktoken


def encoding_for_model(model: str):
    """Return a tiktoken Encoding object roughly matching *model*."""
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def count_text_tokens(text: str, model: str) -> int:
    """Token count of a single string using *model*'s encoding."""
    return len(encoding_for_model(model).encode(text))


def count_message_tokens(messages: list[dict[str, Any]], model: str) -> int:
    """Rough token count of a chat message list using tiktoken."""
    enc = encoding_for_model(model)
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


__all__ = ["count_message_tokens", "count_text_tokens", "encoding_for_model"]
