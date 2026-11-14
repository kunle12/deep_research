"""Typed Row dataclasses — shared between SQLite and Postgres backends.

Each dataclass corresponds to one table in the SQLite schema (P10.5a).
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class ArtifactRow:
    """Row from `artifacts` table."""

    artifact_id: str  # sha256[:16] hex
    kind: str  # "pdf" | "html" | "report"
    source_url: str | None = None
    source_type: str | None = None  # "arxiv" | "blog" | "html" | "research_report"
    title: str | None = None
    authors: str | None = None  # JSON array
    discovered_by: str | None = None  # ToolName.value
    arxiv_id: str | None = None
    parents: str | None = None  # JSON array of artifact_ids
    bytes_path: str = ""  # relative to pdl.root_dir
    bytes_size: int | None = None
    first_seen_at: str = ""  # ISO 8601
    last_touched_at: str = ""  # ISO 8601
    raw_metadata: str | None = None  # JSON blob
    # P10.5b refresh columns
    refresh_after_at: str | None = None
    last_refreshed_at: str | None = None
    upstream_unchanged_since: str | None = None


@dataclass
class ReportRow:
    """Row from `reports` table."""

    run_id: str  # UUID v4
    started_at: str = ""
    completed_at: str | None = None
    original_query: str = ""
    path_taken: str = ""
    classifier_rationale: str | None = None
    iterations: int | None = None
    config_snapshot: str | None = None  # JSON blob
    markdown: str = ""
    artifact_id: str | None = None  # FK -> artifacts
    citations_json: str | None = None
    classifier_json: str | None = None


@dataclass
class AnalysisRow:
    """Row from `analyses` table."""

    analysis_id: str
    artifact_id: str
    run_id: str
    analyzer: str  # "analyze_paper" | "analyze_source" | "analyze_blog"
    summary: str | None = None
    key_findings: str | None = None  # JSON array
    methodology: str | None = None
    limitations: str | None = None
    gaps: str | None = None
    follow_ups: str | None = None
    key_references: str | None = None  # JSON array
    relevance_to_query: str | None = None
    relevance_score: float | None = None  # 0..1, LLM-scored
    analyzed_at: str = ""


@dataclass
class CitationEdgeRow:
    """Row from `citation_edges` table."""

    source_artifact_id: str
    target_artifact_id: str | None = None
    target_arxiv_id: str | None = None
    rationale: str | None = None
    weight: float = 0.5
    discovered_in_run: str | None = None


@dataclass
class TagRow:
    """Row from `tags` table."""

    tag: str
    artifact_id: str
    applied_in_run: str | None = None


@dataclass
class GlossaryEntry:
    """Row from `glossary` table."""

    term_id: int = 0
    term: str = ""
    term_canonical: str = ""
    kind: str = "concept"  # concept|acronym|method|metric|dataset|model|tool
    short_def: str | None = None
    long_def: str | None = None
    acronym_expansion: str | None = None
    related_terms: str | None = None  # JSON array
    domain_tags: str | None = None  # JSON array
    confidence: float | None = None
    first_seen_run_id: str | None = None
    first_seen_artifact_id: str | None = None
    last_updated: str = ""


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return [raw] if isinstance(raw, str) and raw.strip() else []
    if isinstance(data, list):
        return [str(x) for x in data if str(x).strip()]
    return []


def merge_glossary_entries(existing: GlossaryEntry, incoming: GlossaryEntry) -> GlossaryEntry:
    """Merge a new glossary extraction into an existing entry.

    A later extraction must not silently degrade a good earlier definition.
    Keep the higher-confidence definition fields, fill missing fields from the
    other side, and union related terms / domain tags. Provenance
    (``first_seen_*``) always comes from the original row.
    """
    incoming_conf = incoming.confidence or 0.0
    existing_conf = existing.confidence or 0.0
    incoming_wins = incoming_conf >= existing_conf

    def _pick(new: str | None, old: str | None) -> str | None:
        if incoming_wins:
            return new or old
        return old or new

    related = sorted(
        set(_json_list(existing.related_terms)) | set(_json_list(incoming.related_terms))
    )
    domains = sorted(
        set(_json_list(existing.domain_tags)) | set(_json_list(incoming.domain_tags))
    )

    return GlossaryEntry(
        term_id=existing.term_id,
        term=_pick(incoming.term or None, existing.term or None) or existing.term or incoming.term,
        term_canonical=existing.term_canonical or incoming.term_canonical,
        kind=_pick(incoming.kind or None, existing.kind or None) or "concept",
        short_def=_pick(incoming.short_def, existing.short_def),
        long_def=_pick(incoming.long_def, existing.long_def),
        acronym_expansion=_pick(incoming.acronym_expansion, existing.acronym_expansion),
        related_terms=json.dumps(related, ensure_ascii=False) if related else None,
        domain_tags=json.dumps(domains, ensure_ascii=False) if domains else None,
        confidence=max(incoming_conf, existing_conf) or None,
        first_seen_run_id=existing.first_seen_run_id or incoming.first_seen_run_id,
        first_seen_artifact_id=existing.first_seen_artifact_id
        or incoming.first_seen_artifact_id,
        last_updated=incoming.last_updated or existing.last_updated,
    )


@dataclass
class RefreshJobRow:
    """Row from `refresh_jobs` table."""

    job_id: str
    started_at: str = ""
    completed_at: str | None = None
    scope_kind: str = ""
    scope_value: str = ""
    artifacts_considered: int | None = None
    artifacts_refreshed: int | None = None
    status: str = "running"  # "running" | "completed" | "failed" | "partial"
    error: str | None = None


@dataclass
class SearchHit:
    """Result of a full-text search."""

    artifact_id: str
    title: str
    authors: str
    summary: str
    extracted_text: str
    score: float = 0.0
    relevance_score: float | None = None  # stored analysis relevance, if any
    url: str = ""  # artifact source URL, when known
    source_type: str = ""  # arxiv | blog | html | pdf | research_report
