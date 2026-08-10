"""Pydantic response models for the library web UI."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReportListItem(BaseModel):
    """Compact report card shown in the list view."""

    run_id: str
    started_at: str
    completed_at: str | None = None
    query: str
    path: str
    iterations: int | None = None
    tags: list[str] = Field(default_factory=list)
    snippet: str = ""
    citation_count: int = 0
    markdown_length: int = 0
    has_pdf: bool = False


class ReportListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ReportListItem] = Field(default_factory=list)


class ReportDetail(BaseModel):
    run_id: str
    started_at: str
    completed_at: str | None = None
    query: str
    path: str
    classifier_rationale: str | None = None
    iterations: int | None = None
    markdown: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    artifact_id: str | None = None
    has_pdf: bool = False
    pdf_url: str | None = None
    markdown_url: str = ""
    glossary: list[GlossaryInfo] = Field(default_factory=list)
    glossary_url: str = ""
    bibliography_url: str = ""
    bibliography_bib_url: str = ""


class TagUpdateResponse(BaseModel):
    ok: bool = True
    tags: list[str] = Field(default_factory=list)


class TagInfo(BaseModel):
    tag: str
    count: int


class AnalysisInfo(BaseModel):
    analysis_id: str
    artifact_id: str
    run_id: str
    analyzer: str
    summary: str | None = None
    key_findings: list[str] = Field(default_factory=list)
    methodology: str | None = None
    limitations: str | None = None
    gaps: str | None = None
    follow_ups: str | None = None
    key_references: list[str] = Field(default_factory=list)
    relevance_to_query: str | None = None
    analyzed_at: str = ""


class GlossaryInfo(BaseModel):
    term: str
    term_canonical: str
    kind: str = "concept"
    short_def: str | None = None
    long_def: str | None = None
    acronym_expansion: str | None = None
    related_terms: list[str] = Field(default_factory=list)
    domain_tags: list[str] = Field(default_factory=list)
    confidence: float | None = None
    first_seen_run_id: str | None = None
    first_seen_artifact_id: str | None = None
    last_updated: str = ""


class CitationEdgeInfo(BaseModel):
    source_artifact_id: str
    target_artifact_id: str | None = None
    target_arxiv_id: str | None = None
    rationale: str | None = None
    weight: float = 0.5
    discovered_in_run: str | None = None


class ArtifactDetail(BaseModel):
    artifact_id: str
    kind: str
    source_url: str | None = None
    source_type: str | None = None
    image_url: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    arxiv_id: str | None = None
    bytes_path: str = ""
    bytes_size: int | None = None
    first_seen_at: str = ""
    last_touched_at: str = ""
    tags: list[str] = Field(default_factory=list)
    analyses: list[AnalysisInfo] = Field(default_factory=list)
    citation_edges: list[CitationEdgeInfo] = Field(default_factory=list)


class SearchHitItem(BaseModel):
    artifact_id: str
    title: str = ""
    authors: str = ""
    summary: str = ""
    score: float = 0.0


class SearchResponse(BaseModel):
    q: str
    reports: list[ReportListItem] = Field(default_factory=list)
    artifacts: list[SearchHitItem] = Field(default_factory=list)


class StatsResponse(BaseModel):
    reports: int = 0
    artifacts: int = 0
    tags: int = 0


class DeleteReportResponse(BaseModel):
    ok: bool = True
    removed_files: list[str] = Field(default_factory=list)


class RenameReportRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)


class MergeReportsRequest(BaseModel):
    other_run_ids: list[str] = Field(min_length=1, max_length=19)
    name: str | None = Field(default=None, max_length=1000)
    delete_sources: bool = False


class MergeReportsResponse(BaseModel):
    run_id: str
    query: str
