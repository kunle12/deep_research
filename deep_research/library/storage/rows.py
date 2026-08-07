"""Typed Row dataclasses — shared between SQLite and Postgres backends.

Each dataclass corresponds to one table in the SQLite schema (P10.5a).
"""

from __future__ import annotations

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
