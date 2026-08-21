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
from deep_research.library.storage.sqlite_backend import SqliteStorageBackend

logger = logging.getLogger(__name__)

_MIGRATION_FILE = Path(__file__).resolve().parent / "migrations" / "postgres" / "0001_initial.sql"


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PostgresStorageBackend:
    """Postgres implementation of StorageBackend.

    Uses asyncpg for async PostgreSQL access.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None

    # -- Lifecycle --

    async def connect(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=8)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- Schema management --

    async def ensure_schema(self) -> None:
        """Create all tables if they don't exist. Single consolidated migration."""
        if self._pool is None:
            raise RuntimeError("Postgres backend not connected")
        sql = _MIGRATION_FILE.read_text(encoding="utf-8")
        # asyncpg's Connection.execute() only runs the first statement of a
        # multi-statement script, so we split first. This split is safe for
        # the current 0001_initial.sql (no semicolons inside string literals).
        # If a future migration ever contains semicolons inside string
        # literals, use a proper SQL parser instead.
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        async with self._pool.acquire() as conn, conn.transaction():
            for stmt in statements:
                await conn.execute(stmt)
            # One-time migration: databases created before the 'image' artifact
            # kind existed have a CHECK on `kind` that rejects it. Drop only the
            # stale kind check(s) and re-add one that allows 'image'; never touch
            # unrelated CHECK constraints (e.g. on bytes_size).
            await conn.execute(
                """
                DO $$
                DECLARE r record;
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid = 'artifacts'::regclass
                          AND contype = 'c'
                          AND pg_get_constraintdef(oid) LIKE '%image%'
                    ) THEN
                        FOR r IN SELECT conname FROM pg_constraint
                                 WHERE conrelid = 'artifacts'::regclass
                                   AND contype = 'c'
                                   AND pg_get_constraintdef(oid) LIKE '%kind%'
                                   AND NOT pg_get_constraintdef(oid) LIKE '%image%'
                        LOOP
                            EXECUTE format(
                                'ALTER TABLE artifacts DROP CONSTRAINT %I',
                                r.conname
                            );
                        END LOOP;
                        ALTER TABLE artifacts ADD CONSTRAINT artifacts_kind_check
                            CHECK (kind IN ('pdf','html','report','image'));
                    END IF;
                END $$;
                """
            )
            # One-time migration: databases created before the relevance-ranking
            # feature lack `analyses.relevance_score`. Idempotent.
            await conn.execute("ALTER TABLE analyses ADD COLUMN IF NOT EXISTS relevance_score REAL")
        logger.info(
            "schema initialized from %s (%d statements)", _MIGRATION_FILE.name, len(statements)
        )

    # -- Helpers --

    # asyncpg connections are not safe for concurrent use, so every operation
    # acquires a dedicated connection from the pool. The pool keeps concurrent
    # researchers (academic path) from stepping on each other.

    async def _fetchone(self, sql: str, *args: Any) -> tuple | None:
        if self._pool is None:
            raise RuntimeError("Postgres backend not connected")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, *args)
            return tuple(row) if row else None

    async def _fetchall(self, sql: str, *args: Any) -> list[tuple]:
        if self._pool is None:
            raise RuntimeError("Postgres backend not connected")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
            return [tuple(r) for r in rows]

    async def _execute(self, sql: str, *args: Any) -> str | None:
        if self._pool is None:
            raise RuntimeError("Postgres backend not connected")
        async with self._pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def _ensure_conn(self) -> None:
        if self._pool is None:
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
        await self._execute(
            sql,
            artifact.artifact_id,
            artifact.kind,
            artifact.source_url,
            artifact.source_type,
            artifact.title,
            artifact.authors,
            artifact.discovered_by,
            artifact.arxiv_id,
            artifact.parents,
            artifact.bytes_path,
            artifact.bytes_size,
            artifact.first_seen_at,
            artifact.last_touched_at,
            artifact.raw_metadata,
            artifact.refresh_after_at,
            artifact.last_refreshed_at,
            artifact.upstream_unchanged_since,
        )
        return artifact.artifact_id

    async def get_artifact(self, artifact_id: str) -> ArtifactRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM artifacts WHERE artifact_id = $1", artifact_id)
        if row is None:
            return None
        return self._row_to_artifact(row)

    async def get_artifacts(self, artifact_ids: list[str]) -> dict[str, ArtifactRow]:
        """Batch artifact fetch, keyed by artifact_id (missing ids omitted)."""
        if not artifact_ids:
            return {}
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM artifacts WHERE artifact_id = ANY($1)",
            (artifact_ids,),
        )
        return {r[0]: self._row_to_artifact(r) for r in rows}

    async def find_artifact_by_url(self, url: str) -> ArtifactRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM artifacts WHERE source_url = $1", url)
        if row is None:
            return None
        return self._row_to_artifact(row)

    async def find_artifact_by_arxiv_id(self, arxiv_id: str) -> ArtifactRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM artifacts WHERE arxiv_id = $1", arxiv_id)
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
                scope_value,
                limit,
            )
        elif scope_kind == "tag":
            rows = await self._fetchall(
                """SELECT a.* FROM artifacts a
                JOIN tags t ON t.artifact_id = a.artifact_id
                WHERE t.tag = $1
                  AND a.refresh_after_at IS NOT NULL
                  AND (a.last_refreshed_at IS NULL OR a.last_refreshed_at < a.refresh_after_at)
                LIMIT $2""",
                scope_value,
                limit,
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
                original_query = EXCLUDED.original_query,
                path_taken = EXCLUDED.path_taken,
                classifier_rationale = EXCLUDED.classifier_rationale,
                iterations = EXCLUDED.iterations,
                config_snapshot = EXCLUDED.config_snapshot,
                markdown = EXCLUDED.markdown,
                artifact_id = EXCLUDED.artifact_id,
                citations_json = EXCLUDED.citations_json,
                classifier_json = EXCLUDED.classifier_json
        """
        await self._execute(
            sql,
            report.run_id,
            report.started_at,
            report.completed_at,
            report.original_query,
            report.path_taken,
            report.classifier_rationale,
            report.iterations,
            report.config_snapshot,
            report.markdown,
            report.artifact_id,
            report.citations_json,
            report.classifier_json,
        )

    async def get_report(self, run_id: str) -> ReportRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM reports WHERE run_id = $1", run_id)
        if row is None:
            return None
        return self._row_to_report(row)

    async def rename_report(self, run_id: str, new_query: str) -> None:
        """Update `reports.original_query` (the research's display name)."""
        await self._ensure_conn()
        await self._execute(
            "UPDATE reports SET original_query = $1 WHERE run_id = $2",
            new_query,
            run_id,
        )

    async def reassign_run(self, old_run_id: str, new_run_id: str) -> None:
        """Repoint analyses/citation_edges/glossary/artifact_versions from
        `old_run_id` to `new_run_id`. Run BEFORE `delete_report(old_run_id)`.
        No-op when `old == new`. Multi-statement update runs in one
        transaction so the merged report either owns all results or none."""
        await self._ensure_conn()
        if old_run_id == new_run_id:
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE analyses SET run_id = $1 WHERE run_id = $2",
                new_run_id,
                old_run_id,
            )
            await conn.execute(
                "UPDATE citation_edges SET discovered_in_run = $1 WHERE discovered_in_run = $2",
                new_run_id,
                old_run_id,
            )
            await conn.execute(
                "UPDATE glossary SET first_seen_run_id = $1 WHERE first_seen_run_id = $2",
                new_run_id,
                old_run_id,
            )
            await conn.execute(
                "UPDATE artifact_versions SET discovered_in_run = $1 WHERE discovered_in_run = $2",
                new_run_id,
                old_run_id,
            )

    def _report_filter_sql(
        self,
        *,
        q: str | None = None,
        tag: str | None = None,
        path: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Shared WHERE clause for report list/search/count queries."""
        clauses: list[str] = []
        params: list[Any] = []
        if tag:
            clauses.append(
                "EXISTS (SELECT 1 FROM tags t WHERE t.artifact_id = r.artifact_id AND t.tag = $1)"
            )
            params.append(tag)
        if path:
            clauses.append(f"r.path_taken = ${len(params) + 1}")
            params.append(path)
        if q:
            like = f"%{_escape_like(q)}%"
            n = len(params) + 1
            clauses.append(
                f"(LOWER(r.original_query) LIKE LOWER(${n}) ESCAPE '\\' "
                f"OR LOWER(r.markdown) LIKE LOWER(${n + 1}) ESCAPE '\\')"
            )
            params.extend([like, like])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    async def list_reports(
        self,
        limit: int,
        offset: int = 0,
        *,
        tag: str | None = None,
        path: str | None = None,
    ) -> list[ReportRow]:
        await self._ensure_conn()
        where, params = self._report_filter_sql(tag=tag, path=path)
        n = len(params)
        rows = await self._fetchall(
            f"SELECT * FROM reports r{where} ORDER BY r.started_at DESC "
            f"LIMIT ${n + 1} OFFSET ${n + 2}",
            *params,
            limit,
            offset,
        )
        return [self._row_to_report(r) for r in rows]

    async def search_reports(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        tag: str | None = None,
        path: str | None = None,
    ) -> list[ReportRow]:
        await self._ensure_conn()
        where, params = self._report_filter_sql(q=query, tag=tag, path=path)
        n = len(params)
        rows = await self._fetchall(
            f"SELECT * FROM reports r{where} ORDER BY r.started_at DESC "
            f"LIMIT ${n + 1} OFFSET ${n + 2}",
            *params,
            limit,
            offset,
        )
        return [self._row_to_report(r) for r in rows]

    async def count_reports(
        self,
        *,
        q: str | None = None,
        tag: str | None = None,
        path: str | None = None,
    ) -> int:
        await self._ensure_conn()
        where, params = self._report_filter_sql(q=q, tag=tag, path=path)
        row = await self._fetchone(f"SELECT count(*) FROM reports r{where}", *params)
        return int(row[0]) if row else 0

    async def count_artifacts(self, *, q: str | None = None, kind: str | None = None) -> int:
        await self._ensure_conn()
        where, params = self._artifact_filter_sql(q=q, kind=kind)
        row = await self._fetchone(f"SELECT count(*) FROM artifacts a{where}", *params)
        return int(row[0]) if row else 0

    def _artifact_filter_sql(
        self,
        *,
        q: str | None = None,
        kind: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Shared WHERE clause for artifact list/count queries."""
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append(f"a.kind = ${len(params) + 1}")
            params.append(kind)
        if q:
            like = f"%{_escape_like(q)}%"
            n = len(params) + 1
            clauses.append(
                f"(COALESCE(a.title, '') ILIKE ${n} ESCAPE '\\' "
                f"OR COALESCE(a.source_url, '') ILIKE ${n + 1} ESCAPE '\\' "
                f"OR COALESCE(a.arxiv_id, '') ILIKE ${n + 2} ESCAPE '\\')"
            )
            params.extend([like, like, like])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    async def list_artifacts(
        self,
        limit: int,
        offset: int = 0,
        *,
        q: str | None = None,
        kind: str | None = None,
    ) -> list[ArtifactRow]:
        await self._ensure_conn()
        where, params = self._artifact_filter_sql(q=q, kind=kind)
        n = len(params)
        rows = await self._fetchall(
            f"SELECT a.* FROM artifacts a{where} "
            f"ORDER BY a.first_seen_at DESC LIMIT ${n + 1} OFFSET ${n + 2}",
            *params,
            limit,
            offset,
        )
        return [self._row_to_artifact(r) for r in rows]

    def _row_to_report(self, row: tuple) -> ReportRow:
        return ReportRow(
            run_id=row[0],
            started_at=row[1],
            completed_at=row[2],
            original_query=row[3],
            path_taken=row[4],
            classifier_rationale=row[5],
            iterations=row[6],
            config_snapshot=row[7],
            markdown=row[8],
            artifact_id=row[9],
            citations_json=row[10],
            classifier_json=row[11],
        )

    # -- Analysis ops --

    async def insert_analysis(self, analysis: AnalysisRow) -> str:
        await self._ensure_conn()
        sql = """
            INSERT INTO analyses (
                analysis_id, artifact_id, run_id, analyzer, summary,
                key_findings, methodology, limitations, gaps, follow_ups,
                key_references, relevance_to_query, relevance_score, analyzed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (analysis_id) DO UPDATE SET
                summary = EXCLUDED.summary, key_findings = EXCLUDED.key_findings,
                methodology = EXCLUDED.methodology, limitations = EXCLUDED.limitations,
                gaps = EXCLUDED.gaps, follow_ups = EXCLUDED.follow_ups,
                key_references = EXCLUDED.key_references,
                relevance_to_query = EXCLUDED.relevance_to_query,
                relevance_score = EXCLUDED.relevance_score
        """
        await self._execute(
            sql,
            analysis.analysis_id,
            analysis.artifact_id,
            analysis.run_id,
            analysis.analyzer,
            analysis.summary,
            analysis.key_findings,
            analysis.methodology,
            analysis.limitations,
            analysis.gaps,
            analysis.follow_ups,
            analysis.key_references,
            analysis.relevance_to_query,
            analysis.relevance_score,
            analysis.analyzed_at,
        )
        return analysis.analysis_id

    async def get_analysis(self, analysis_id: str) -> AnalysisRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM analyses WHERE analysis_id = $1", analysis_id)
        if row is None:
            return None
        return AnalysisRow(
            analysis_id=row[0],
            artifact_id=row[1],
            run_id=row[2],
            analyzer=row[3],
            summary=row[4],
            key_findings=row[5],
            methodology=row[6],
            limitations=row[7],
            gaps=row[8],
            follow_ups=row[9],
            key_references=row[10],
            relevance_to_query=row[11],
            relevance_score=row[13],
            analyzed_at=row[12],
        )

    async def get_analyses_for_artifact(self, artifact_id: str) -> list[AnalysisRow]:
        await self._ensure_conn()
        rows = await self._fetchall("SELECT * FROM analyses WHERE artifact_id = $1", artifact_id)
        results: list[AnalysisRow] = []
        for r in rows:
            results.append(
                AnalysisRow(
                    analysis_id=r[0],
                    artifact_id=r[1],
                    run_id=r[2],
                    analyzer=r[3],
                    summary=r[4],
                    key_findings=r[5],
                    methodology=r[6],
                    limitations=r[7],
                    gaps=r[8],
                    follow_ups=r[9],
                    key_references=r[10],
                    relevance_to_query=r[11],
                    relevance_score=r[13],
                    analyzed_at=r[12],
                )
            )
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
        await self._execute(
            sql,
            edge.source_artifact_id,
            edge.target_artifact_id,
            edge.target_arxiv_id,
            edge.rationale,
            edge.weight,
            edge.discovered_in_run,
        )

    async def get_citation_edges_for_source(self, artifact_id: str) -> list[CitationEdgeRow]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM citation_edges WHERE source_artifact_id = $1",
            artifact_id,
        )
        results: list[CitationEdgeRow] = []
        for r in rows:
            results.append(
                CitationEdgeRow(
                    source_artifact_id=r[0],
                    target_artifact_id=r[1],
                    target_arxiv_id=r[2],
                    rationale=r[3],
                    weight=r[4],
                    discovered_in_run=r[5],
                )
            )
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
        rows = await self._fetchall("SELECT * FROM tags WHERE artifact_id = $1", artifact_id)
        results: list[TagRow] = []
        for r in rows:
            results.append(TagRow(tag=r[0], artifact_id=r[1], applied_in_run=r[2]))
        return results

    async def get_tags_for_artifacts(self, artifact_ids: list[str]) -> dict[str, list[TagRow]]:
        if not artifact_ids:
            return {}
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM tags WHERE artifact_id = ANY($1)",
            (artifact_ids,),
        )
        result: dict[str, list[TagRow]] = {a: [] for a in artifact_ids}
        for r in rows:
            tag = TagRow(tag=r[0], artifact_id=r[1], applied_in_run=r[2])
            result.setdefault(tag.artifact_id, []).append(tag)
        return result

    async def list_tags(self, limit: int = 200) -> list[tuple[str, int]]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT tag, count(*) AS n FROM tags GROUP BY tag ORDER BY n DESC, tag LIMIT $1",
            limit,
        )
        return [(r[0], int(r[1])) for r in rows]

    async def delete_tag(self, tag: str, artifact_id: str) -> None:
        await self._ensure_conn()
        await self._execute(
            "DELETE FROM tags WHERE tag = $1 AND artifact_id = $2",
            tag,
            artifact_id,
        )

    async def rename_tag(self, old_tag: str, new_tag: str) -> None:
        await self._ensure_conn()
        await self._execute(
            "UPDATE tags SET tag = $1 WHERE tag = $2",
            new_tag,
            old_tag,
        )

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
                last_updated = EXCLUDED.last_updated
        """
        await self._execute(
            sql,
            entry.term,
            entry.term_canonical,
            entry.kind,
            entry.short_def,
            entry.long_def,
            entry.acronym_expansion,
            entry.related_terms,
            entry.domain_tags,
            entry.confidence,
            entry.first_seen_run_id,
            entry.first_seen_artifact_id,
            entry.last_updated,
        )

    async def upsert_glossary_entries(self, entries: list[GlossaryEntry], run_id: str) -> int:
        count = 0
        for e in entries:
            await self.upsert_glossary_entry(e)
            count += 1
        return count

    async def get_glossary_entry(self, term_canonical: str) -> GlossaryEntry | None:
        await self._ensure_conn()
        row = await self._fetchone(
            "SELECT * FROM glossary WHERE term_canonical = $1", term_canonical
        )
        if row is None:
            return None
        return GlossaryEntry(
            term_id=row[0],
            term=row[1],
            term_canonical=row[2],
            kind=row[3],
            short_def=row[4],
            long_def=row[5],
            acronym_expansion=row[6],
            related_terms=row[7],
            domain_tags=row[8],
            confidence=row[9],
            first_seen_run_id=row[10],
            first_seen_artifact_id=row[11],
            last_updated=row[12],
        )

    async def list_glossary_entries(self) -> list[GlossaryEntry]:
        await self._ensure_conn()
        rows = await self._fetchall("SELECT * FROM glossary ORDER BY term_canonical")
        results: list[GlossaryEntry] = []
        for r in rows:
            results.append(
                GlossaryEntry(
                    term_id=r[0],
                    term=r[1],
                    term_canonical=r[2],
                    kind=r[3],
                    short_def=r[4],
                    long_def=r[5],
                    acronym_expansion=r[6],
                    related_terms=r[7],
                    domain_tags=r[8],
                    confidence=r[9],
                    first_seen_run_id=r[10],
                    first_seen_artifact_id=r[11],
                    last_updated=r[12],
                )
            )
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
        await self._execute(
            sql,
            old_id,
            new_id,
            reason,
            datetime.now(UTC).isoformat(),
            run_id,
        )

    async def start_refresh_job(self, scope_kind: str, scope_value: str) -> str:
        await self._ensure_conn()
        import uuid
        from datetime import UTC, datetime

        job_id = str(uuid.uuid4())
        sql = """
            INSERT INTO refresh_jobs (job_id, started_at, scope_kind, scope_value, status)
            VALUES ($1, $2, $3, $4, 'running')
        """
        await self._execute(
            sql,
            job_id,
            datetime.now(UTC).isoformat(),
            scope_kind,
            scope_value,
        )
        return job_id

    async def get_refresh_job(self, job_id: str) -> Any | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM refresh_jobs WHERE job_id = $1", job_id)
        if row is None:
            return None
        from dataclasses import dataclass

        @dataclass
        class RefreshJob:
            job_id: str
            started_at: str
            completed_at: str | None
            scope_kind: str
            scope_value: str
            artifacts_considered: int | None
            artifacts_refreshed: int | None
            status: str
            error: str | None

        return RefreshJob(
            job_id=row[0],
            started_at=row[1],
            completed_at=row[2],
            scope_kind=row[3],
            scope_value=row[4],
            artifacts_considered=row[5],
            artifacts_refreshed=row[6],
            status=row[7],
            error=row[8],
        )

    async def complete_refresh_job(
        self,
        job_id: str,
        considered: int,
        refreshed: int,
        status: str,
        error: str | None = None,
    ) -> None:
        await self._ensure_conn()
        from datetime import UTC, datetime

        sql = """
            UPDATE refresh_jobs SET
                completed_at = $1, artifacts_considered = $2,
                artifacts_refreshed = $3, status = $4, error = $5
            WHERE job_id = $6
        """
        await self._execute(
            sql,
            datetime.now(UTC).isoformat(),
            considered,
            refreshed,
            status,
            error,
            job_id,
        )

    # -- Deletion --

    async def delete_report(self, run_id: str) -> None:
        """Delete a report and its dependent rows (analyses, tags,
        citation_edges). Does NOT delete artifacts. Postgres uses GIN
        indexes directly on the analyses table — no separate FTS table
        to clean up."""
        await self._ensure_conn()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM analyses WHERE run_id = $1", run_id)
            await conn.execute("DELETE FROM tags WHERE applied_in_run = $1", run_id)
            await conn.execute("DELETE FROM citation_edges WHERE discovered_in_run = $1", run_id)
            # Nullify glossary entries and artifact_versions that reference
            # this run (preserve the data itself — it may be relevant to
            # other runs).
            await conn.execute(
                "UPDATE glossary SET first_seen_run_id = NULL WHERE first_seen_run_id = $1",
                run_id,
            )
            await conn.execute(
                "UPDATE artifact_versions SET discovered_in_run = NULL "
                "WHERE discovered_in_run = $1",
                run_id,
            )
            await conn.execute("DELETE FROM reports WHERE run_id = $1", run_id)

    async def delete_artifact(self, artifact_id: str) -> None:
        """Delete an artifact and its dependent rows. Refuses (FK violation)
        if a report still references it via reports.artifact_id — caller
        should nullify or delete that report first."""
        await self._ensure_conn()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM analyses WHERE artifact_id = $1", artifact_id)
            await conn.execute("DELETE FROM tags WHERE artifact_id = $1", artifact_id)
            await conn.execute(
                "DELETE FROM citation_edges WHERE source_artifact_id = $1 "
                "OR target_artifact_id = $1",
                artifact_id,
            )
            await conn.execute(
                "DELETE FROM artifact_versions WHERE artifact_id_old = $1 OR artifact_id_new = $1",
                artifact_id,
            )
            await conn.execute("DELETE FROM artifacts WHERE artifact_id = $1", artifact_id)

    # -- FTS --

    async def full_text_search(self, query: str, *, kind: str, limit: int) -> list[SearchHit]:
        await self._ensure_conn()
        # plainto_tsquery parses boolean operators (& | !) and can raise a
        # tsquery syntax error on arbitrary user input — sanitize the query
        # the same way the SQLite backend does before passing it through.
        safe_query = SqliteStorageBackend._sanitize_fts_query(query)
        if kind == "any":
            sql = """
                SELECT a.artifact_id, a.title, a.authors, an.summary, an.key_findings,
                       an.relevance_score
                FROM analyses an
                JOIN artifacts a ON a.artifact_id = an.artifact_id
                WHERE (
                    to_tsvector('english', coalesce(an.summary, '')) @@ plainto_tsquery('english', $1)
                    OR to_tsvector('english', coalesce(an.key_findings, '')) @@ plainto_tsquery('english', $1)
                )
                LIMIT $2
            """
            rows = await self._fetchall(sql, safe_query, limit)
        else:
            sql = """
                SELECT a.artifact_id, a.title, a.authors, an.summary, an.key_findings,
                       an.relevance_score
                FROM analyses an
                JOIN artifacts a ON a.artifact_id = an.artifact_id
                WHERE a.kind = $1
                  AND (
                    to_tsvector('english', coalesce(an.summary, '')) @@ plainto_tsquery('english', $2)
                    OR to_tsvector('english', coalesce(an.key_findings, '')) @@ plainto_tsquery('english', $2)
                  )
                LIMIT $3
            """
            rows = await self._fetchall(sql, kind, safe_query, limit)
        results: list[SearchHit] = []
        for r in rows:
            results.append(
                SearchHit(
                    artifact_id=r[0],
                    title=r[1] or "",
                    authors=r[2] or "",
                    summary=r[3] or "",
                    extracted_text=r[4] or "",
                    score=1.0,
                    relevance_score=r[5],
                )
            )
        return results

    async def glossary_search(self, query: str, limit: int) -> list[GlossaryEntry]:
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
            results.append(
                GlossaryEntry(
                    term_id=r[0],
                    term=r[1],
                    term_canonical=r[2],
                    kind=r[3],
                    short_def=r[4],
                    long_def=r[5],
                    acronym_expansion=r[6],
                    related_terms=r[7],
                    domain_tags=r[8],
                    confidence=r[9],
                    first_seen_run_id=r[10],
                    first_seen_artifact_id=r[11],
                    last_updated=r[12],
                )
            )
        return results
