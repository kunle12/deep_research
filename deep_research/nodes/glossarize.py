"""Glossary extraction node (P10.6).

Per-run LLM-call enrichment: appends a glossary extraction prompt to the
synthesis call so the model optionally emits a `glossary` array alongside the
main markdown. Cross-run rule-based dedup is handled by LibraryWriter.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from deep_research.library.storage.rows import GlossaryEntry
from deep_research.library.writer import LibraryWriter

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "glossary_extract.txt"





async def extract_and_save_glossary(
    response_text: str,
    run_id: str,
    writer: LibraryWriter | None,
) -> None:
    """Parse glossary entries from an LLM response and persist them via the library writer."""
    if not isinstance(writer, LibraryWriter) or not run_id:
        return
    glossary_entries = parse_glossary_from_response(response_text, run_id)
    if glossary_entries:
        await writer.upsert_glossary_entries(glossary_entries, run_id)





def parse_glossary_from_response(
    response_text: str,
    run_id: str,
    artifact_id: str | None = None,
) -> list[GlossaryEntry]:
    """Parse glossary entries from an LLM JSON response.

    Expects a JSON object with optional 'glossary' array.
    Returns empty list if no glossary field found.
    """
    if not response_text:
        return []

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return []

    glossary_data = data.get("glossary")
    if not glossary_data or not isinstance(glossary_data, list):
        return []

    entries: list[GlossaryEntry] = []
    now = datetime.now(UTC).isoformat()

    for item in glossary_data:
        if not isinstance(item, dict):
            continue
        term = item.get("term", "").strip()
        if not term:
            continue
        term_canonical = _canonicalize(term)
        kind = item.get("kind", "concept")
        if kind not in ("concept", "acronym", "method", "metric", "dataset", "model", "tool"):
            kind = "concept"

        short_def = item.get("short_def", "")
        long_def = item.get("long_def")
        acronym_expansion = item.get("acronym_expansion")
        related_terms = item.get("related_terms", [])
        domain_tags = item.get("domain_tags", [])

        entries.append(GlossaryEntry(
            term=term,
            term_canonical=term_canonical,
            kind=kind,
            short_def=short_def or None,
            long_def=long_def or None,
            acronym_expansion=acronym_expansion or None,
            related_terms=json.dumps(related_terms) if related_terms else None,
            domain_tags=json.dumps(domain_tags) if domain_tags else None,
            confidence=float(item.get("confidence", 0.5)),
            first_seen_run_id=run_id,
            first_seen_artifact_id=artifact_id,
            last_updated=now,
        ))

    return entries


def _canonicalize(term: str) -> str:
    """Lowercase, strip punctuation for dedup key."""
    s = term.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = s.strip()
    return s





__all__ = [
    "extract_and_save_glossary",
    "parse_glossary_from_response",
]
