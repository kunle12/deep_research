"""URL type classifier — decides which fetch path to use.

Given a URL, returns one of: "arxiv", "pdf", "html".

Strategy:
1. If host contains "arxiv.org", return "arxiv".
   (Even .pdf URLs on arxiv should use the arxiv tool, which can resolve
   /abs/ vs /pdf/ forms and produce proper metadata.)
2. If path ends with `.pdf`, return "pdf".
3. Otherwise async HEAD-probe Content-Type; fall back to "html" on
   indeterminate results or transport errors.

P2.5 improved: added `head_probe_content_type` async helper.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class UrlType(str, Enum):
    arxiv = "arxiv"
    pdf = "pdf"
    html = "html"
    unknown = "unknown"


def classify_url_sync(url: str) -> UrlType:
    """Synchronous heuristic classification (no HTTP probe)."""
    if not url:
        return UrlType.unknown
    try:
        parsed = urlparse(url)
    except ValueError:
        return UrlType.unknown

    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()

    if "arxiv.org" in host:
        return UrlType.arxiv
    if path.endswith(".pdf"):
        return UrlType.pdf
    return UrlType.html


async def head_probe_content_type(
    url: str,
    *,
    user_agent: str = "DeepResearchBot/0.1",
    timeout_s: float = 8.0,
) -> str | None:
    """HEAD-probe a URL and return its Content-Type (lowercased, bare). None on error.

    Useful to disambiguate PDF-vs-HTML for URLs that lack a `.pdf` suffix
    (e.g., signed CDN links). Returns None on any transport / HTTP error so
    the caller can fall back to the sync heuristic.
    """
    if not url or not url.startswith(("http://", "https://")):
        return None
    headers = {"User-Agent": user_agent}
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, follow_redirects=True, max_redirects=5
        ) as client:
            resp = await client.head(url, headers=headers)
            ctype = resp.headers.get("content-type", "") or ""
            return ctype.split(";")[0].strip().lower() or None
    except httpx.HTTPError as e:
        logger.debug("head_probe %s failed: %s", url, e)
        return None
    except Exception as e:
        logger.debug("head_probe %s raised: %s", url, e)
        return None


async def classify_url(
    url: str,
    *,
    user_agent: str = "DeepResearchBot/0.1",
    head_probe_timeout_s: float = 8.0,
) -> UrlType:
    """Async classifier with HEAD-probe fallback for PDF-vs-HTML disambiguation.

    Cheaper than the full fetch: HEAD only, returns immediately on a definitive
    sync heuristic; only probes when the sync result is `html` (ambiguous).
    """
    sync = classify_url_sync(url)
    if sync in (UrlType.arxiv, UrlType.pdf, UrlType.unknown):
        return sync
    # Sync said "html" — confirm via HEAD-probe Content-Type.
    ctype = await head_probe_content_type(
        url, user_agent=user_agent, timeout_s=head_probe_timeout_s
    )
    if ctype == "application/pdf":
        return UrlType.pdf
    return UrlType.html


_ARXIV_PATH_RX = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([^\s/?#]+?)(v\d+)?(?:\.pdf)?(?:[/?#].*)?$",
    re.IGNORECASE,
)
_ARXIV_VERSION_RX = re.compile(r"(v\d+)$")


def extract_arxiv_id(url: str) -> str | None:
    """Extract arxiv id from an arxiv.org URL.

    Handles these forms:
      https://arxiv.org/abs/2401.12345           -> 2401.12345
      https://arxiv.org/abs/2401.12345v3          -> 2401.12345v3
      https://arxiv.org/pdf/2401.12345            -> 2401.12345
      https://arxiv.org/pdf/2401.12345v3.pdf      -> 2401.12345v3
      https://arxiv.org/pdf/cs.LG/0702001         -> cs.LG/0702001
    """
    if not url:
        return None
    m = _ARXIV_PATH_RX.search(url)
    if not m:
        return None
    base, version = m.group(1), m.group(2) or ""
    return base + version


__all__ = [
    "UrlType",
    "classify_url",
    "classify_url_sync",
    "extract_arxiv_id",
    "head_probe_content_type",
]
