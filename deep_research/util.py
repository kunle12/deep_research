"""Small shared helpers for coercing untrusted values into typed ones.

LLM JSON output and third-party search APIs routinely return numbers as
strings, ``None``, or other non-numeric shapes. Keeping the coercion in one
place means every node/tool gets the same tolerant behaviour instead of each
carrying its own copy of a try/except.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def coerce_float(value: Any, default: float) -> float:
    """Coerce *value* to ``float``, falling back to *default* on bad input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def utc_today_str() -> str:
    """Current UTC date as ``YYYY-MM-DD`` — injected into prompts so agents
    can reason about recency ("recent", "state of the art")."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


_ARXIV_VERSION_RX = re.compile(r"v\d+$")


def strip_arxiv_version(arxiv_id: str) -> str:
    """Strip the trailing arxiv version suffix (``2401.12345v3`` -> ``2401.12345``)."""
    return _ARXIV_VERSION_RX.sub("", arxiv_id)


# Canonical arXiv id form (optionally versioned): YYMM.NNNNN(vN). Shared by
# the critic and paper-analysis modules for validating paper candidates.
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")

# Valid researcher tool-hint vocabulary, shared by planner + critic.
VALID_TOOL_HINTS = {"general-web", "arxiv", "reddit", "browser-required"}


# ---------------------------------------------------------------------------
# Prompt template loading (cached, shared across nodes/paths)
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_PROMPT_CACHE: dict[str, str] = {}


def load_prompt_template(name: str) -> str:
    """Load ``prompts/<name>.txt`` once and cache it. Blocking file read, so
    async callers should wrap in ``asyncio.to_thread`` on a cold start."""
    cached = _PROMPT_CACHE.get(name)
    if cached is not None:
        return cached
    template = (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    _PROMPT_CACHE[name] = template
    return template


__all__ = [
    "ARXIV_ID_RE",
    "VALID_TOOL_HINTS",
    "coerce_float",
    "load_prompt_template",
    "strip_arxiv_version",
    "utc_today_str",
]
