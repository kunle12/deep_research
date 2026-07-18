"""Postgres storage backend — P12.0.

Uses asyncpg for non-blocking PostgreSQL access.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    CitationEdgeRow,
    GlossaryEntry,
    ReportRow,
    SearchHit,
    TagRow,
)

logger = logging.getLogger(__name__)

_MIGRATION_DIR = Path(__file__).resolve().parent / "migrations" / "postgres"

_MIGRATION_FILES = [
    "0001_initial.sql",
    "0002_add_glossary.sql",
    "0003_add_refresh_foundation.sql",
]


def _parse_migration_version(filename: str) -> int:
    parts = filename.split("_", 1)
    try:
        return int(parts[0])
    except ValueError:
        return 0


class PostgresStorageBackend:
    """Postgres implementation of StorageBackend.

    Uses asyncpg for async PostgreSQL access.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None

    # -- Lifecycle --

    async def connect(self) -> None:
        import asyncpg
        self._conn = await asyncpg.connect(self._dsn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- Schema management --

    async def current_schema_version(self) -> int:
        if self._conn is None:
            return 0
        try:
            row = await self._conn.fetchval(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            )
        except Exception:
            return 0
        if row is None:
            return 0
        try:
            return int(row)
        except (ValueError, TypeError):
            return 0

    async def apply_migration(self, version: int) -> None:
        if self._conn is None:
            raise RuntimeError("Postgres backend not connected")
        for fname in _MIGRATION_FILES:
            if _parse_migration_version(fname) == version:
                sql_path = _MIGRATION_DIR / fname
                sql = sql_path.read_text(encoding="utf-8")
                await self._conn.execute(sql)
                logger.info("applied migration v%d: %s", version, fname)
                return
        raise ValueError(f"No migration found for version {version}")

    async def ensure_schema(self) -> None:
        current = await self.current_schema_version()
        latest = len(_MIGRATION_FILES)
        for version in range(current + 1, latest + 1):
            await self.apply_migration(version)
        if current < latest:
            logger.info("schema migrated from v%d to v%d", current, latest)

    # -- Helpers --

    async def _fetchone(self, sql: str, *args: Any) -> tuple | None:
        if self._conn is None:
            raise RuntimeError("Postgres backend not connected")
        row = await self._conn.fetchrow(sql, *args)
        return tuple(row) if row else None

    async def _fetchall(self, sql: str, *args: Any) -> list[tuple]:
        if self._conn is None:
            raise RuntimeError("Postgres backend not connected")
        rows = await self._conn.fetch(sql, *args)
        return [tuple(r) for r in rows]

    async def _execute(self, sql: str, *args: Any) -> str | None:
        if self._conn is None:
            raise RuntimeError("Postgres backend not connected")
        return await self._conn.execute(sql, *args)

    async def _ensure_conn(self) -> None:
        if self._conn is None:
            await self.connect()

    # -- Artifact ops --

    async def upsert_artifact(self, artifact: ArtifactRow) -> str:
        await self._ensure_conn()
        sql = """
            INSERT INTO artifacts (
                artifact_id, kind, source_url, source_type, title, authors,
                discovered_by, arxiv_id, parents, bytes_path, bytes_size,
                first_seen_at, last_touched_at, raw_metadata,
                refresh_after_at, last_refreshed_at, upstream_unchanged_since
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                      $12, $13, $14, $15, $16, $17)
            ON CONFLICT (artifact_id) DO UPDATE SET
                kind = EXCLUDED.kind, source_url = EXCLUDED.source_url,
                source_type = EXCLUDED.source_type, title = EXCLUDED.title,
                authors = EXCLUDED.authors, discovered_by = EXCLUDED.discovered_by,
                arxiv_id = EXCLUDED.arxiv_id, parents = EXCLUDED.parents,
                bytes_path = EXCLUDED.bytes_path, bytes_size = EXCLUDED.bytes_size,
                last_touched_at = EXCLUDED.last_touched_at,
                raw_metadata = EXCLUDED.raw_metadata,
                refresh_after_at = EXCLUDED.refresh_after_at,
                last_refreshed_at = EXCLUDED.last_refreshed_at,
                upstream_unchanged_since = EXCLUDED.upstream_unchanged_since
        """
        await self._execute(sql,
            artifact.artifact_id, artifact.kind, artifact.source_url,
            artifact.source_type, artifact.title, artifact.authors,
            artifact.discovered_by, artifact.arxiv_id, artifact.parents,
            artifact.bytes_path, artifact.bytes_size,
            artifact.first_seen_at, artifact.last_touched_at,
            artifact.raw_metadata,
            artifact.refresh_after_at, artifact.last_refreshed_at,
            artifact.upstream_unchanged_since,
        )
        return artifact.artifact_id

    async def get_artifact(self, artifact_id: str) -> ArtifactRow | None:
        await self._ensure_conn()
        row = await self._fetchone(
            "SELECT * FROM artifacts WHERE artifact_id = $1", artifact_id
        )
        if row is None:
            return None
        return self._row_to_artifact(row)

    async def find_artifact_by_url(self, url: str) -> ArtifactRow | None:
        await self._ensure_conn()
        row = await self._fetchone(
            "SELECT * FROM artifacts WHERE source_url = $1", url
        )
        if row is None:
            return None
        return self._row_to_artifact(row)

    async def find_artifact_by_arxiv_id(self, arxiv_id: str) -> ArtifactRow | None:
        await self._ensure_conn()
        row = await self._fetchone(
            "SELECT * FROM artifacts WHERE arxiv_id = $1", arxiv_id
        )
        if row is None:
            return None
        return self._row_to_artifact(row)

    async def artifacts_needing_refresh(
        self, scope_kind: str, scope_value: str, limit: int
    ) -> list[ArtifactRow]:
        await self._ensure_conn()
        if scope_kind == "source_type":
            rows = await self._fetchall(
                """SELECT * FROM artifacts
                WHERE source_type = $1
                  AND refresh_after_at IS NOT NULL
                  AND (last_refreshed_at IS NULL OR last_refreshed_at < refresh_after_at)
                LIMIT $2""",
                scope_value, limit,
            )
        elif scope_kind == "tag":
            rows = await self._fetchall(
                """SELECT a.* FROM artifacts a
                JOIN tags t ON t.artifact_id = a.artifact_id
                WHERE t.tag = $1
                  AND a.refresh_after_at IS NOT NULL
                  AND (a.last_refreshed_at IS NULL OR a.last_refreshed_at < a.refresh_after_at)
                LIMIT $2""",
                scope_value, limit,
            )
        elif scope_kind == "artifact_id":
            rows = await self._fetchall(
                "SELECT * FROM artifacts WHERE artifact_id = $1 AND refresh_after_at IS NOT NULL",
                scope_value,
            )
        else:
            return []
        return [self._row_to_artifact(r) for r in rows]

    def _row_to_artifact(self, row: tuple) -> ArtifactRow:
        return ArtifactRow(
            artifact_id=row[0],
            kind=row[1],
            source_url=row[2],
            source_type=row[3],
            title=row[4],
            authors=row[5],
            discovered_by=row[6],
            arxiv_id=row[7],
            parents=row[8],
            bytes_path=row[9],
            bytes_size=row[10],
            first_seen_at=row[11],
            last_touched_at=row[12],
            raw_metadata=row[13],
            refresh_after_at=row[14],
            last_refreshed_at=row[15],
            upstream_unchanged_since=row[16],
        )

    # -- Report ops --

    async def insert_report(self, report: ReportRow) -> None:
        await self._ensure_conn()
        sql = """
            INSERT INTO reports (
                run_id, started_at, completed_at, original_query, path_taken,
                classifier_rationale, iterations, config_snapshot, markdown,
                artifact_id, citations_json, classifier_json
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (run_id) DO UPDATE SET
                completed_at = EXCLUDED.completed_at,
                markdown = EXCLUDED.markdown,
                artifact_id = EXCLUDED.artifact_id,
                citations_json = EXCLUDED.citations_json,
                classifier_json = EXCLUDED.classifier_json
        """
        await self._execute(sql,
            report.run_id, report.started_at, report.completed_at,
            report.original_query, report.path_taken,
            report.classifier_rationale, report.iterations,
            report.config_snapshot, report.markdown,
            report.artifact_id, report.citations_json, report.classifier_json,
        )

    async def get_report(self, run_id: str) -> ReportRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM reports WHERE run_id = $1", run_id)
        if row is None:
            return None
        return ReportRow(
            run_id=row[0], started_at=row[1], completed_at=row[2],
            original_query=row[3], path_taken=row[4],
            classifier_rationale=row[5], iterations=row[6],
            config_snapshot=row[7], markdown=row[8],
            artifact_id=row[9], citations_json=row[10],
            classifier_json=row[11],
        )

    async def list_reports(self, limit: int) -> list[ReportRow]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM reports ORDER BY started_at DESC LIMIT $1", limit
        )
        results: list[ReportRow] = []
        for r in rows:
            results.append(ReportRow(
                run_id=r[0], started_at=r[1], completed_at=r[2],
                original_query=r[3], path_taken=r[4],
                classifier_rationale=r[5], iterations=r[6],
                config_snapshot=r[7], markdown=r[8],
                artifact_id=r[9], citations_json=r[10],
                classifier_json=r[11],
            ))
        return results

    # -- Analysis ops --

    async def insert_analysis(self, analysis: AnalysisRow) -> None:
        await self._ensure_conn()
        sql = """
            INSERT INTO analyses (
                analysis_id, artifact_id, run_id, analyzer, summary,
                key_findings, methodology, limitations, gaps, follow_ups,
                key_references, relevance_to_query, analyzed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (analysis_id) DO UPDATE SET
                summary = EXCLUDED.summary, key_findings = EXCLUDED.key_findings,
                methodology = EXCLUDED.methodology, limitations = EXCLUDED.limitations,
                gaps = EXCLUDED.gaps, follow_ups = EXCLUDED.follow_ups,
                key_references = EXCLUDED.key_references,
                relevance_to_query = EXCLUDED.relevance_to_query
        """
        await self._execute(sql,
            analysis.analysis_id, analysis.artifact_id, analysis.run_id,
            analysis.analyzer, analysis.summary, analysis.key_findings,
            analysis.methodology, analysis.limitations, analysis.gaps,
            analysis.follow_ups, analysis.key_references,
            analysis.relevance_to_query, analysis.analyzed_at,
        )

    async def get_analyses_for_artifact(self, artifact_id: str) -> list[AnalysisRow]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM analyses WHERE artifact_id = $1", artifact_id
        )
        results: list[AnalysisRow] = []
        for r in rows:
            results.append(AnalysisRow(
                analysis_id=r[0], artifact_id=r[1], run_id=r[2],
                analyzer=r[3], summary=r[4], key_findings=r[5],
                methodology=r[6], limitations=r[7], gaps=r[8],
                follow_ups=r[9], key_references=r[10],
                relevance_to_query=r[11], analyzed_at=r[12],
            ))
        return results

    # -- Citation edge ops --

    async def insert_citation_edge(self, edge: CitationEdgeRow) -> None:
        await self._ensure_conn()
        sql = """
            INSERT INTO citation_edges (
                source_artifact_id, target_artifact_id, target_arxiv_id,
                rationale, weight, discovered_in_run
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (source_artifact_id, target_arxiv_id) DO UPDATE SET
                target_artifact_id = EXCLUDED.target_artifact_id,
                rationale = EXCLUDED.rationale,
                weight = EXCLUDED.weight,
                discovered_in_run = EXCLUDED.discovered_in_run
        """
        await self._execute(sql,
            edge.source_artifact_id, edge.target_artifact_id,
            edge.target_arxiv_id, edge.rationale, edge.weight,
            edge.discovered_in_run,
        )

    async def get_citation_edges_for_source(
        self, artifact_id: str
    ) -> list[CitationEdgeRow]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM citation_edges WHERE source_artifact_id = $1",
            artifact_id,
        )
        results: list[CitationEdgeRow] = []
        for r in rows:
            results.append(CitationEdgeRow(
                source_artifact_id=r[0], target_artifact_id=r[1],
                target_arxiv_id=r[2], rationale=r[3], weight=r[4],
                discovered_in_run=r[5],
            ))
        return results

    # -- Tag ops --

    async def upsert_tag(self, tag: TagRow) -> None:
        await self._ensure_conn()
        sql = """
            INSERT INTO tags (tag, artifact_id, applied_in_run)
            VALUES ($1, $2, $3)
            ON CONFLICT (tag, artifact_id) DO UPDATE SET
                applied_in_run = EXCLUDED.applied_in_run
        """
        await self._execute(sql, tag.tag, tag.artifact_id, tag.applied_in_run)

    async def get_tags_for_artifact(self, artifact_id: str) -> list[TagRow]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM tags WHERE artifact_id = $1", artifact_id
        )
        results: list[TagRow] = []
        for r in rows:
            results.append(TagRow(tag=r[0], artifact_id=r[1], applied_in_run=r[2]))
        return results

    async def get_artifacts_by_tag(self, tag: str) -> list[str]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT artifact_id FROM tags WHERE tag = $1", tag
        )
        return [r[0] for r in rows]

    # -- Glossary ops --

    async def upsert_glossary_entry(self, entry: GlossaryEntry) -> None:
        await self._ensure_conn()
        sql = """
            INSERT INTO glossary (
                term, term_canonical, kind, short_def, long_def,
                acronym_expansion, related_terms, domain_tags, confidence,
                first_seen_run_id, first_seen_artifact_id, last_updated
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (term_canonical) DO UPDATE SET
                term = EXCLUDED.term, kind = EXCLUDED.kind,
                short_def = EXCLUDED.short_def, long_def = EXCLUDED.long_def,
                acronym_expansion = EXCLUDED.acronym_expansion,
                related_terms = EXCLUDED.related_terms,
                domain_tags = EXCLUDED.domain_tags,
                confidence = EXCLUDED.confidence,
                first_seen_run_id = EXCLUDED.first_seen_run_id,
                first_seen_artifact_id = EXCLUDED.first_seen_artifact_id,
                last_updated = EXCLUDED.last_updated
        """
        await self._execute(sql,
            entry.term, entry.term_canonical, entry.kind, entry.short_def,
            entry.long_def, entry.acronym_expansion, entry.related_terms,
            entry.domain_tags, entry.confidence,
            entry.first_seen_run_id, entry.first_seen_artifact_id,
            entry.last_updated,
        )

    async def get_glossary_entry(
        self, term_canonical: str
    ) -> GlossaryEntry | None:
        await self._ensure_conn()
        row = await self._fetchone(
            "SELECT * FROM glossary WHERE term_canonical = $1", term_canonical
        )
        if row is None:
            return None
        return GlossaryEntry(
            term_id=row[0], term=row[1], term_canonical=row[2],
            kind=row[3], short_def=row[4], long_def=row[5],
            acronym_expansion=row[6], related_terms=row[7],
            domain_tags=row[8], confidence=row[9],
            first_seen_run_id=row[10], first_seen_artifact_id=row[11],
            last_updated=row[12],
        )

    async def list_glossary_entries(self) -> list[GlossaryEntry]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM glossary ORDER BY term_canonical"
        )
        results: list[GlossaryEntry] = []
        for r in rows:
            results.append(GlossaryEntry(
                term_id=r[0], term=r[1], term_canonical=r[2],
                kind=r[3], short_def=r[4], long_def=r[5],
                acronym_expansion=r[6], related_terms=r[7],
                domain_tags=r[8], confidence=r[9],
                first_seen_run_id=r[10], first_seen_artifact_id=r[11],
                last_updated=r[12],
            ))
        return results

    # -- Refresh foundation --

    async def insert_artifact_version(
        self, old_id: str, new_id: str, reason: str, run_id: str
    ) -> None:
        await self._ensure_conn()
        from datetime import UTC, datetime

        sql = """
            INSERT INTO artifact_versions (
                artifact_id_old, artifact_id_new, reason, discovered_at, discovered_in_run
            ) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (artifact_id_old, artifact_id_new) DO UPDATE SET
                reason = EXCLUDED.reason
        """
        await self._execute(sql,
            old_id, new_id, reason,
            datetime.now(UTC).isoformat(), run_id,
        )

    async def start_refresh_job(
        self, scope_kind: str, scope_value: str
    ) -> str:
        await self._ensure_conn()
        import uuid
        from datetime import UTC, datetime

        job_id = str(uuid.uuid4())
        sql = """
            INSERT INTO refresh_jobs (job_id, started_at, scope_kind, scope_value, status)
            VALUES ($1, $2, $3, $4, 'running')
        """
        await self._execute(sql,
            job_id, datetime.now(UTC).isoformat(), scope_kind, scope_value,
        )
        return job_id

    async def complete_refresh_job(
        self, job_id: str, considered: int, refreshed: int,
        status: str, error: str | None = None,
    ) -> None:
        await self._ensure_conn()
        from datetime import UTC, datetime

        sql = """
            UPDATE refresh_jobs SET
                completed_at = $1, artifacts_considered = $2,
                artifacts_refreshed = $3, status = $4, error = $5
            WHERE job_id = $6
        """
        await self._execute(sql,
            datetime.now(UTC).isoformat(), considered, refreshed,
            status, error, job_id,
        )

    # -- FTS --

    async def full_text_search(
        self, query: str, *, kind: str, limit: int
    ) -> list[SearchHit]:
        await self._ensure_conn()
        # Postgres uses tsvector instead of FTS5
        sql = """
            SELECT a.artifact_id, a.title, a.authors, an.summary, an.key_findings
            FROM analyses an
            JOIN artifacts a ON a.artifact_id = an.artifact_id
            WHERE a.kind = $1
              AND (
                to_tsvector('english', coalesce(an.summary, '')) @@ plainto_tsquery('english', $2)
                OR to_tsvector('english', coalesce(an.key_findings, '')) @@ plainto_tsquery('english', $2)
              )
            LIMIT $3
        """
        rows = await self._fetchall(sql, kind, query, limit)
        results: list[SearchHit] = []
        for r in rows:
            results.append(SearchHit(
                artifact_id=r[0], title=r[1] or "", authors=r[2] or "",
                summary=r[3] or "", extracted_text=r[4] or "",
                score=1.0,
            ))
        return results

    async def glossary_search(
        self, query: str, limit: int
    ) -> list[GlossaryEntry]:
        await self._ensure_conn()
        sql = """
            SELECT * FROM glossary
            WHERE to_tsvector('english', coalesce(term, '') || ' ' || coalesce(short_def, ''))
                  @@ plainto_tsquery('english', $1)
            LIMIT $2
        """
        rows = await self._fetchall(sql, query, limit)
        results: list[GlossaryEntry] = []
        for r in rows:
            results.append(GlossaryEntry(
                term_id=r[0], term=r[1], term_canonical=r[2],
                kind=r[3], short_def=r[4], long_def=r[5],
                acronym_expansion=r[6], related_terms=r[7],
                domain_tags=r[8], confidence=r[9],
                first_seen_run_id=r[10], first_seen_artifact_id=r[11],
                last_updated=r[12],
            ))
        return results
