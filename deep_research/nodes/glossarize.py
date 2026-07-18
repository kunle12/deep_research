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


# _load_prompt and extract_glossary were removed — they were unused.
# Glossary extraction is done inline via parse_glossary_from_response.


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


# _coerce and _dedup_rule_based were removed — they were unused.
# Glossary normalization happens inline in parse_glossary_from_response.


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


def render_glossary_md(
    entries: list[GlossaryEntry],
    run_id: str | None = None,
) -> str:
    """Render glossary entries as a markdown document."""
    if not entries:
        return "# Personal Research Glossary\n\n_No entries yet._\n"

    from collections import defaultdict
    grouped: dict[str, list[GlossaryEntry]] = defaultdict(list)
    for e in entries:
        domain = "General"
        if e.domain_tags:
            tags = json.loads(e.domain_tags) if isinstance(e.domain_tags, str) else e.domain_tags
            if tags and isinstance(tags, list) and len(tags) > 0 and tags[0]:
                domain = tags[0]
        grouped[domain].append(e)

    lines: list[str] = [
        "# Personal Research Glossary\n",
        f"_Last updated: {datetime.now(UTC).isoformat()}  ·  {len(entries)} terms_\n",
    ]

    for domain, group in sorted(grouped.items()):
        lines.append(f"\n## {domain}\n")
        for e in group:
            expansion = f"  ·  {e.acronym_expansion}" if e.acronym_expansion else ""
            lines.append(f"\n### {e.term}{expansion}")
            lines.append(f"**{e.kind}**")
            if e.short_def:
                lines.append(f"\n{e.short_def}")
            if e.long_def and e.long_def != e.short_def:
                lines.append(f"\n{e.long_def}")
            if e.related_terms:
                related = json.loads(e.related_terms) if isinstance(e.related_terms, str) else e.related_terms
                if related:
                    lines.append(f"\n**Related:** {', '.join(related)}")
            lines.append(f"\n_Confidence: {e.confidence:.2f}_\n")

    lines.append("\n---\n")
    return "\n".join(lines)


def merge_glossary_entries(
    existing: list[GlossaryEntry],
    new_entries: list[GlossaryEntry],
) -> list[GlossaryEntry]:
    """Rule-based cross-run glossary merge.

    Merges by term_canonical. Same acronym with different expansions → keep both (WARN logged).
    Same canonical term with new longer long_def → update.
    """
    merged: dict[str, GlossaryEntry] = {}
    for e in existing:
        merged[e.term_canonical] = e

    for e in new_entries:
        existing_e = merged.get(e.term_canonical)
        if existing_e:
            if e.acronym_expansion and existing_e.acronym_expansion and e.acronym_expansion != existing_e.acronym_expansion:
                logger.warning(
                    "glossary merge: term '%s' has conflicting expansions: '%s' vs '%s'; keeping both (WARN)",
                    e.term, existing_e.acronym_expansion, e.acronym_expansion,
                )
                continue
            if e.long_def and (not existing_e.long_def or len(e.long_def) > len(existing_e.long_def)):
                existing_e.long_def = e.long_def
                existing_e.last_updated = e.last_updated
            if e.confidence > existing_e.confidence:
                existing_e.confidence = e.confidence
        else:
            merged[e.term_canonical] = e

    return list(merged.values())


__all__ = [
    "_canonicalize",
    "extract_and_save_glossary",
    "merge_glossary_entries",
    "parse_glossary_from_response",
    "render_glossary_md",
]
