"""URL detector — extracts the first URL from a user query string.

Used by `agent.run_research()` to short-circuit to url_source mode when the
user pastes a URL alongside their question.
"""

from __future__ import annotations

import re

# Conservative URL regex. Matches http(s):// URLs with most standard chars.
_URL_RX = re.compile(
    r"https?://"
    r"(?:[A-Za-z0-9\-._~%!$&'()*+,;=:@]+)"
    r"(?:/[A-Za-z0-9\-._~%!$&'()*+,;=:@/]*)?"
    r"(?:\?[^\s)\]\}<>]+)?"
    r"(?:#[^\s)\]\}<>]+)?"
)


def extract_first_url(text: str) -> str | None:
    """Return the first http(s) URL in `text`, or None."""
    if not text:
        return None
    m = _URL_RX.search(text)
    return m.group(0) if m else None


def strip_url_from_query(text: str, url: str) -> str:
    """Remove a URL substring from `text`, return the cleaned remainder.

    Replaces the URL with a single space and removes any dangling punctuation
    / leading hyphens / em-dashes (commonly "URL - question here" style).
    """
    if not url:
        return text.strip()
    remainder = text.replace(url, " ")
    # Strip leading punctuation/dashes/separators left behind.
    # We intentionally match HYPHEN-MINUS, EM DASH, EN DASH, and colon/pipe.
    remainder = re.sub(r"^[\s\-\u2014\u2013:|]+", "", remainder)
    # Collapse whitespace
    remainder = re.sub(r"\s+", " ", remainder).strip()
    return remainder


__all__ = ["extract_first_url", "strip_url_from_query"]
