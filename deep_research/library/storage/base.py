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

    async def find_artifact_by_arxiv_id(
        self, arxiv_id: str
    ) -> ArtifactRow | None: ...

    async def artifacts_needing_refresh(
        self, scope_kind: str, scope_value: str, limit: int
    ) -> list[ArtifactRow]: ...

    # -- Report ops --
    async def insert_report(self, report: ReportRow) -> None: ...

    async def get_report(self, run_id: str) -> ReportRow | None: ...

    async def list_reports(self, limit: int) -> list[ReportRow]: ...

    # -- Analysis ops --
    async def insert_analysis(self, analysis: AnalysisRow) -> str: ...

    async def get_analyses_for_artifact(
        self, artifact_id: str
    ) -> list[AnalysisRow]: ...

    # -- Citation edge ops --
    async def insert_citation_edge(self, edge: CitationEdgeRow) -> None: ...

    async def get_citation_edges_for_source(
        self, artifact_id: str
    ) -> list[CitationEdgeRow]: ...

    # -- Tag ops --
    async def upsert_tag(self, tag: TagRow) -> None: ...

    async def get_tags_for_artifact(self, artifact_id: str) -> list[TagRow]: ...

    async def get_artifacts_by_tag(self, tag: str) -> list[str]: ...

    # -- Glossary ops --
    async def upsert_glossary_entry(self, entry: GlossaryEntry) -> None: ...

    async def get_glossary_entry(
        self, term_canonical: str
    ) -> GlossaryEntry | None: ...

    async def list_glossary_entries(self) -> list[GlossaryEntry]: ...

    # -- Refresh foundation --
    async def insert_artifact_version(
        self, old_id: str, new_id: str, reason: str, run_id: str
    ) -> None: ...

    async def start_refresh_job(
        self, scope_kind: str, scope_value: str
    ) -> str: ...

    async def complete_refresh_job(
        self,
        job_id: str,
        considered: int,
        refreshed: int,
        status: str,
        error: str | None = None,
    ) -> None: ...

    # -- FTS --
    async def full_text_search(
        self, query: str, *, kind: str, limit: int
    ) -> list[SearchHit]: ...

    async def glossary_search(
        self, query: str, limit: int
    ) -> list[GlossaryEntry]: ...
