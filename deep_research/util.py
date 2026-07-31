"""Small shared helpers for coercing untrusted values into typed ones.

LLM JSON output and third-party search APIs routinely return numbers as
strings, ``None``, or other non-numeric shapes. Keeping the coercion in one
place means every node/tool gets the same tolerant behaviour instead of each
carrying its own copy of a try/except.
"""

from __future__ import annotations

import re
from typing import Any


def coerce_float(value: Any, default: float) -> float:
    """Coerce *value* to ``float``, falling back to *default* on bad input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_ARXIV_VERSION_RX = re.compile(r"v\d+$")


def strip_arxiv_version(arxiv_id: str) -> str:
    """Strip the trailing arxiv version suffix (``2401.12345v3`` -> ``2401.12345``)."""
    return _ARXIV_VERSION_RX.sub("", arxiv_id)


__all__ = ["coerce_float", "strip_arxiv_version"]
