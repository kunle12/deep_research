"""StorageBackend Protocol — pluggable library backend.

SQLite and Postgres backends both comply with this Protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    CitationEdgeRow,
    GlossaryEntry,
    RefreshJobRow,
    ReportRow,
    SearchHit,
    TagRow,
)


@runtime_checkable
class StorageBackend(Protocol):
    """Pluggable library backend. SQLite + Postgres both comply."""

    # -- Lifecycle --
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    # -- Schema management --
    async def ensure_schema(self) -> None:
        """Create all tables if they don't exist."""
        ...

    # -- Artifact ops --
    async def upsert_artifact(self, artifact: ArtifactRow) -> str: ...

    async def get_artifact(self, artifact_id: str) -> ArtifactRow | None: ...

    async def find_artifact_by_url(self, url: str) -> ArtifactRow | None: ...

    async def find_artifact_by_arxiv_id(self, arxiv_id: str) -> ArtifactRow | None: ...

    async def artifacts_needing_refresh(
        self, scope_kind: str, scope_value: str, limit: int
    ) -> list[ArtifactRow]: ...

    # -- Report ops --
    async def insert_report(self, report: ReportRow) -> None: ...

    async def get_report(self, run_id: str) -> ReportRow | None: ...

    async def rename_report(self, run_id: str, new_query: str) -> None:
        """Update `reports.original_query` (the display name of a research)."""
        ...

    async def reassign_run(self, old_run_id: str, new_run_id: str) -> None:
        """Repoint all run-scoped references from one run to another.

        Updates `analyses.run_id`, `citation_edges.discovered_in_run`,
        `glossary.first_seen_run_id`, and `artifact_versions.discovered_in_run`.
        Used before deleting a source report whose results should survive in a
        merged report.
        """
        ...

    async def list_reports(
        self,
        limit: int,
        offset: int = 0,
        *,
        tag: str | None = None,
        path: str | None = None,
    ) -> list[ReportRow]:
        """List reports ordered by started_at DESC, with optional tag/path
        filtering applied in SQL so pagination totals stay correct."""
        ...

    async def search_reports(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        tag: str | None = None,
        path: str | None = None,
    ) -> list[ReportRow]:
        """Full-text-ish search over report queries + markdown bodies."""
        ...

    async def count_reports(
        self,
        *,
        q: str | None = None,
        tag: str | None = None,
        path: str | None = None,
    ) -> int:
        """Count reports matching the same filters as list/search."""
        ...

    async def count_artifacts(self) -> int: ...

    async def list_tags(self, limit: int = 200) -> list[tuple[str, int]]:
        """All distinct tags with counts, ordered by count DESC then tag."""
        ...

    async def get_artifacts(self, artifact_ids: list[str]) -> dict[str, ArtifactRow]:
        """Batch artifact fetch, keyed by artifact_id."""
        ...

    # -- Analysis ops --
    async def insert_analysis(self, analysis: AnalysisRow) -> str: ...

    async def get_analysis(self, analysis_id: str) -> AnalysisRow | None: ...

    async def get_analyses_for_artifact(self, artifact_id: str) -> list[AnalysisRow]: ...

    # -- Citation edge ops --
    async def insert_citation_edge(self, edge: CitationEdgeRow) -> None: ...

    async def get_citation_edges_for_source(self, artifact_id: str) -> list[CitationEdgeRow]: ...

    # -- Tag ops --
    async def upsert_tag(self, tag: TagRow) -> None: ...

    async def get_tags_for_artifact(self, artifact_id: str) -> list[TagRow]: ...

    async def get_tags_for_artifacts(self, artifact_ids: list[str]) -> dict[str, list[TagRow]]: ...

    async def delete_tag(self, tag: str, artifact_id: str) -> None: ...

    async def rename_tag(self, old_tag: str, new_tag: str) -> None: ...

    # -- Glossary ops --
    async def upsert_glossary_entry(self, entry: GlossaryEntry) -> None: ...

    async def get_glossary_entry(self, term_canonical: str) -> GlossaryEntry | None: ...

    async def list_glossary_entries(self) -> list[GlossaryEntry]: ...

    # -- Refresh foundation --
    async def insert_artifact_version(
        self, old_id: str, new_id: str, reason: str, run_id: str
    ) -> None: ...

    async def start_refresh_job(self, scope_kind: str, scope_value: str) -> str: ...

    async def get_refresh_job(self, job_id: str) -> RefreshJobRow | None: ...

    async def complete_refresh_job(
        self,
        job_id: str,
        considered: int,
        refreshed: int,
        status: str,
        error: str | None = None,
    ) -> None: ...

    # -- FTS --
    async def full_text_search(self, query: str, *, kind: str, limit: int) -> list[SearchHit]: ...

    async def glossary_search(self, query: str, limit: int) -> list[GlossaryEntry]: ...

    # -- Deletion --
    async def delete_report(self, run_id: str) -> None: ...

    async def delete_artifact(self, artifact_id: str) -> None: ...
