"""Recall node — query the library for prior research context before hitting the web.

P13: implemented. Uses the existing FTS5 index over analyses.summary and
analyses.key_findings to find prior knowledge matching a query. Injected
into the researcher's system prompt so the LLM knows what we already know.
"""

from __future__ import annotations

import logging

from deep_research.library.storage.base import StorageBackend

logger = logging.getLogger(__name__)

_MAX_RESULTS = 5

# Stored analyses with an explicitly-low relevance score are dropped from
# recall so an archived off-topic document cannot pollute a later researcher's
# prior context. None (pre-feature rows) are kept — lenient.
_MIN_RELEVANCE = 0.3


async def recall(
    query: str,
    storage: StorageBackend | None,
    max_results: int = _MAX_RESULTS,
    min_relevance: float = _MIN_RELEVANCE,
) -> list[dict]:
    """Query the library's FTS5 index for prior analyses matching `query`.

    Returns a list of dicts with keys:
      artifact_id, title, summary, key_findings, source_type, url, relevance_score
    Empty list when storage is None or no matches found. Hits whose stored
    relevance_score is below *min_relevance* are filtered out.
    """
    if storage is None:
        return []

    if not query or not query.strip():
        return []

    try:
        hits = await storage.full_text_search(query, kind="any", limit=max_results)
    except Exception as e:
        logger.warning("recall FTS5 search failed: %s: %s", type(e).__name__, e)
        return []

    if not hits:
        return []

    results: list[dict] = []
    seen: set[str] = set()
    for hit in hits:
        aid = hit.artifact_id
        if aid in seen:
            continue
        seen.add(aid)
        if (
            min_relevance > 0
            and hit.relevance_score is not None
            and hit.relevance_score < min_relevance
        ):
            continue
        results.append(
            {
                "artifact_id": aid,
                "title": hit.title or "",
                "summary": hit.summary or "",
                "key_findings": hit.extracted_text or "",
                "source_type": "",  # not returned by FTS5; caller may enrich
                "url": "",  # not returned by FTS5; caller may enrich
                "relevance_score": hit.relevance_score,
            }
        )

    return results


def format_recall_context(entries: list[dict]) -> str:
    """Format recall entries as a markdown section for the researcher's prompt."""
    if not entries:
        return ""

    lines = [
        "## Prior research from the library",
        "",
        "The following entries match your research question from prior runs:",
        "",
    ]
    for i, e in enumerate(entries, start=1):
        title = e.get("title") or "(no title)"
        summary = (e.get("summary") or "")[:500]
        lines.append(f"**{i}. {title}**")
        if summary:
            lines.append(f"   Summary: {summary}")
        lines.append("")

    lines.append(
        "Use this prior context to avoid re-fetching already-known information. "
        "You may still call web_search / arxiv / etc. for the *delta* — "
        "what the library doesn't already cover."
    )
    lines.append("")

    return "\n".join(lines)


__all__ = ["format_recall_context", "recall"]
