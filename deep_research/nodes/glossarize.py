"""Glossary extraction node (P10.6).

Dedicated post-synthesis LLM call: after the main report is generated, a
separate lightweight LLM call extracts glossary terms from the report text and
persists them to the library database. Cross-run rule-based dedup is handled by
LibraryWriter.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from deep_research.library.storage.rows import GlossaryEntry
from deep_research.library.writer import LibraryWriter
from deep_research.llm.router import LLMClientLike
from deep_research.util import coerce_float

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "glossary_extract.txt"


async def extract_glossary_from_report(
    report_text: str,
    llm: LLMClientLike,
    model: str,
    writer: LibraryWriter | None,
    run_id: str,
) -> list[GlossaryEntry]:
    """Make a dedicated LLM call to extract glossary terms from the report text.

    Uses ``response_format=json_object`` to guarantee valid JSON. Fills the
    ``{context}`` placeholder in the glossary prompt with the report text.
    Returns the parsed ``GlossaryEntry`` list (empty if nothing extracted).
    """
    if not isinstance(writer, LibraryWriter) or not run_id:
        return []

    prompt_text = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    # Fill the {context} placeholder with the report text
    user_msg = prompt_text.replace("{context}", report_text)

    try:
        resp = await llm.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a glossary extraction assistant. Output ONLY valid JSON — no markdown, no code fences.",
                },
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning("Glossary extraction LLM call failed: %s: %s", type(e).__name__, e)
        return []

    glossary_entries = parse_glossary_from_response(raw, run_id)
    if glossary_entries:
        try:
            await writer.upsert_glossary_entries(glossary_entries, run_id)
        except Exception as e:
            # Persistence must never discard a finished report — log and drop.
            logger.warning("glossary persistence failed: %s: %s", type(e).__name__, e)
            return []
    return glossary_entries


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

    if not isinstance(data, dict):
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

        entries.append(
            GlossaryEntry(
                term=term,
                term_canonical=term_canonical,
                kind=kind,
                short_def=short_def or None,
                long_def=long_def or None,
                acronym_expansion=acronym_expansion or None,
                related_terms=json.dumps(related_terms) if related_terms else None,
                domain_tags=json.dumps(domain_tags) if domain_tags else None,
                confidence=coerce_float(item.get("confidence"), 0.5),
                first_seen_run_id=run_id,
                first_seen_artifact_id=artifact_id,
                last_updated=now,
            )
        )

    return entries


def _canonicalize(term: str) -> str:
    """Lowercase, strip punctuation for dedup key."""
    s = term.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = s.strip()
    return s


__all__ = [
    "extract_glossary_from_report",
    "parse_glossary_from_response",
]
