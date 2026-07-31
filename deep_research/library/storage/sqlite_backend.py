"""SQLite storage backend — P10.5a default.

Uses aiosqlite + stdlib sqlite3. WAL mode + busy_timeout for concurrent safety.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiosqlite

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

logger = logging.getLogger(__name__)

_MIGRATION_FILE = Path(__file__).resolve().parent / "migrations" / "sqlite" / "0001_initial.sql"


class SqliteStorageBackend:
    """SQLite implementation of StorageBackend.

    All methods are async. Uses aiosqlite for non-blocking SQL operations.
    WAL mode + busy_timeout=5000 for concurrent-write safety.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    # -- Lifecycle --

    async def connect(self) -> None:
        """Open the SQLite connection with WAL mode and busy timeout."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(
            self._db_path,
            timeout=5000,  # busy_timeout_ms
        )
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self.ensure_schema()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.commit()
            await self._conn.close()
            self._conn = None

    # -- Schema management --

    async def ensure_schema(self) -> None:
        """Create all tables if they don't exist. Single consolidated migration."""
        if self._conn is None:
            raise RuntimeError("SQLite backend not connected")
        sql = _MIGRATION_FILE.read_text(encoding="utf-8")
        await self._conn.executescript(sql)
        # One-time backfill for databases created before the glossary_fts sync
        # triggers existed: rebuild the content-backed index when it is empty
        # but the glossary table has rows (the triggers keep it in sync from
        # now on).
        try:
            row = await self._fetchone("SELECT count(*) FROM glossary")
            row_fts = await self._fetchone("SELECT count(*) FROM glossary_fts")
            if row and row_fts and row[0] > 0 and row_fts[0] == 0:
                await self._conn.execute("INSERT INTO glossary_fts(glossary_fts) VALUES('rebuild')")
        except Exception as e:
            logger.debug("glossary_fts rebuild skipped: %s: %s", type(e).__name__, e)
        await self._conn.commit()
        logger.info("schema initialized from %s", _MIGRATION_FILE.name)

    # -- Helpers --

    async def _execute(self, sql: str, params: tuple = ()) -> Any:
        if self._conn is None:
            raise RuntimeError("SQLite backend not connected")
        return await self._conn.execute(sql, params)

    async def _fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        cursor = await self._execute(sql, params)
        return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        cursor = await self._execute(sql, params)
        return await cursor.fetchall()

    async def _ensure_conn(self) -> None:
        if self._conn is None:
            await self.connect()

    # -- Artifact ops --

    async def upsert_artifact(self, artifact: ArtifactRow) -> str:
        await self._ensure_conn()
        # ON CONFLICT DO UPDATE (not INSERT OR REPLACE): a REPLACE would
        # DELETE + re-INSERT the row, tripping the FK checks from dependent
        # rows (analyses, citation_edges, tags, reports) that reference it.
        # Updating in place keeps those links intact when an artifact that has
        # already been analyzed is re-archived.
        sql = """
            INSERT INTO artifacts (
                artifact_id, kind, source_url, source_type, title, authors,
                discovered_by, arxiv_id, parents, bytes_path, bytes_size,
                first_seen_at, last_touched_at, raw_metadata,
                refresh_after_at, last_refreshed_at, upstream_unchanged_since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                kind = excluded.kind,
                source_url = excluded.source_url,
                source_type = excluded.source_type,
                title = excluded.title,
                authors = excluded.authors,
                discovered_by = excluded.discovered_by,
                arxiv_id = excluded.arxiv_id,
                parents = excluded.parents,
                bytes_path = excluded.bytes_path,
                bytes_size = excluded.bytes_size,
                last_touched_at = excluded.last_touched_at,
                raw_metadata = excluded.raw_metadata,
                refresh_after_at = excluded.refresh_after_at,
                last_refreshed_at = excluded.last_refreshed_at,
                upstream_unchanged_since = excluded.upstream_unchanged_since
        """
        await self._execute(
            sql,
            (
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
            ),
        )
        await self._conn.commit()
        return artifact.artifact_id

    async def get_artifact(self, artifact_id: str) -> ArtifactRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,))
        if row is None:
            return None
        return self._row_to_artifact(row)

    async def find_artifact_by_url(self, url: str) -> ArtifactRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM artifacts WHERE source_url = ?", (url,))
        if row is None:
            return None
        return self._row_to_artifact(row)

    async def find_artifact_by_arxiv_id(self, arxiv_id: str) -> ArtifactRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM artifacts WHERE arxiv_id = ?", (arxiv_id,))
        if row is None:
            return None
        return self._row_to_artifact(row)

    async def artifacts_needing_refresh(
        self, scope_kind: str, scope_value: str, limit: int
    ) -> list[ArtifactRow]:
        await self._ensure_conn()
        # scope_kind can be "source_type" | "tag" | "artifact_id"
        if scope_kind == "source_type":
            sql = """
                SELECT * FROM artifacts
                WHERE source_type = ?
                  AND refresh_after_at IS NOT NULL
                  AND (last_refreshed_at IS NULL OR last_refreshed_at < refresh_after_at)
                LIMIT ?
            """
            rows = await self._fetchall(sql, (scope_value, limit))
        elif scope_kind == "tag":
            sql = """
                SELECT a.* FROM artifacts a
                JOIN tags t ON t.artifact_id = a.artifact_id
                WHERE t.tag = ?
                  AND a.refresh_after_at IS NOT NULL
                  AND (a.last_refreshed_at IS NULL OR a.last_refreshed_at < a.refresh_after_at)
                LIMIT ?
            """
            rows = await self._fetchall(sql, (scope_value, limit))
        elif scope_kind == "artifact_id":
            sql = """
                SELECT * FROM artifacts
                WHERE artifact_id = ?
                  AND refresh_after_at IS NOT NULL
            """
            rows = await self._fetchall(sql, (scope_value,))
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
        # ON CONFLICT DO UPDATE (not INSERT OR REPLACE): a resumed run reuses
        # run_id, and REPLACE would DELETE the existing report row — violating
        # the FK from analyses.run_id that previous runs' analyses carry.
        sql = """
            INSERT INTO reports (
                run_id, started_at, completed_at, original_query, path_taken,
                classifier_rationale, iterations, config_snapshot, markdown,
                artifact_id, citations_json, classifier_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                completed_at = excluded.completed_at,
                original_query = excluded.original_query,
                path_taken = excluded.path_taken,
                classifier_rationale = excluded.classifier_rationale,
                iterations = excluded.iterations,
                config_snapshot = excluded.config_snapshot,
                markdown = excluded.markdown,
                artifact_id = excluded.artifact_id,
                citations_json = excluded.citations_json,
                classifier_json = excluded.classifier_json
        """
        await self._execute(
            sql,
            (
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
            ),
        )
        await self._conn.commit()

    async def get_report(self, run_id: str) -> ReportRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM reports WHERE run_id = ?", (run_id,))
        if row is None:
            return None
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

    async def list_reports(self, limit: int) -> list[ReportRow]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM reports ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        results: list[ReportRow] = []
        for r in rows:
            results.append(
                ReportRow(
                    run_id=r[0],
                    started_at=r[1],
                    completed_at=r[2],
                    original_query=r[3],
                    path_taken=r[4],
                    classifier_rationale=r[5],
                    iterations=r[6],
                    config_snapshot=r[7],
                    markdown=r[8],
                    artifact_id=r[9],
                    citations_json=r[10],
                    classifier_json=r[11],
                )
            )
        return results

    # -- Analysis ops --

    async def insert_analysis(self, analysis: AnalysisRow) -> str:
        await self._ensure_conn()
        # ON CONFLICT DO UPDATE (not INSERT OR REPLACE): a REPLACE would
        # DELETE the existing analysis row, tripping FKs from search_index /
        # citation_edges that reference it.
        sql = """
            INSERT INTO analyses (
                analysis_id, artifact_id, run_id, analyzer, summary,
                key_findings, methodology, limitations, gaps, follow_ups,
                key_references, relevance_to_query, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(analysis_id) DO UPDATE SET
                summary = excluded.summary,
                key_findings = excluded.key_findings,
                methodology = excluded.methodology,
                limitations = excluded.limitations,
                gaps = excluded.gaps,
                follow_ups = excluded.follow_ups,
                key_references = excluded.key_references,
                relevance_to_query = excluded.relevance_to_query,
                analyzed_at = excluded.analyzed_at
        """
        await self._execute(
            sql,
            (
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
                analysis.analyzed_at,
            ),
        )
        # Rebuild FTS5 row for this analysis_id: delete any prior FTS rows
        # with the same analysis_id (UNINDEXED — full scan but bounded by
        # how many stale duplicates exist; usually 0), then insert fresh.
        cursor = await self._execute(
            "DELETE FROM search_index WHERE analysis_id = ?",
            (analysis.analysis_id,),
        )
        await cursor.close()
        await self._execute(
            "INSERT INTO search_index (analysis_id, summary, key_findings) VALUES (?, ?, ?)",
            (analysis.analysis_id, analysis.summary or "", analysis.key_findings or ""),
        )
        await self._conn.commit()
        return analysis.analysis_id

    async def get_analysis(self, analysis_id: str) -> AnalysisRow | None:
        await self._ensure_conn()
        row = await self._fetchone("SELECT * FROM analyses WHERE analysis_id = ?", (analysis_id,))
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
            analyzed_at=row[12],
        )

    async def get_analyses_for_artifact(self, artifact_id: str) -> list[AnalysisRow]:
        await self._ensure_conn()
        rows = await self._fetchall("SELECT * FROM analyses WHERE artifact_id = ?", (artifact_id,))
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
                    analyzed_at=r[12],
                )
            )
        return results

    # -- Citation edge ops --

    async def insert_citation_edge(self, edge: CitationEdgeRow) -> None:
        await self._ensure_conn()
        sql = """
            INSERT OR REPLACE INTO citation_edges (
                source_artifact_id, target_artifact_id, target_arxiv_id,
                rationale, weight, discovered_in_run
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        await self._execute(
            sql,
            (
                edge.source_artifact_id,
                edge.target_artifact_id,
                edge.target_arxiv_id,
                edge.rationale,
                edge.weight,
                edge.discovered_in_run,
            ),
        )
        await self._conn.commit()

    async def get_citation_edges_for_source(self, artifact_id: str) -> list[CitationEdgeRow]:
        await self._ensure_conn()
        rows = await self._fetchall(
            "SELECT * FROM citation_edges WHERE source_artifact_id = ?", (artifact_id,)
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
            INSERT OR REPLACE INTO tags (tag, artifact_id, applied_in_run)
            VALUES (?, ?, ?)
        """
        await self._execute(
            sql,
            (
                tag.tag,
                tag.artifact_id,
                tag.applied_in_run,
            ),
        )
        await self._conn.commit()

    async def get_tags_for_artifact(self, artifact_id: str) -> list[TagRow]:
        await self._ensure_conn()
        rows = await self._fetchall("SELECT * FROM tags WHERE artifact_id = ?", (artifact_id,))
        results: list[TagRow] = []
        for r in rows:
            results.append(TagRow(tag=r[0], artifact_id=r[1], applied_in_run=r[2]))
        return results

    async def get_tags_for_artifacts(self, artifact_ids: list[str]) -> dict[str, list[TagRow]]:
        if not artifact_ids:
            return {}
        await self._ensure_conn()
        placeholders = ",".join("?" * len(artifact_ids))
        rows = await self._fetchall(
            f"SELECT * FROM tags WHERE artifact_id IN ({placeholders})",
            tuple(artifact_ids),
        )
        result: dict[str, list[TagRow]] = {a: [] for a in artifact_ids}
        for r in rows:
            tag = TagRow(tag=r[0], artifact_id=r[1], applied_in_run=r[2])
            result.setdefault(tag.artifact_id, []).append(tag)
        return result

    async def get_artifacts_by_tag(self, tag: str) -> list[str]:
        await self._ensure_conn()
        rows = await self._fetchall("SELECT artifact_id FROM tags WHERE tag = ?", (tag,))
        return [r[0] for r in rows]

    async def delete_tag(self, tag: str, artifact_id: str) -> None:
        await self._ensure_conn()
        await self._execute(
            "DELETE FROM tags WHERE tag = ? AND artifact_id = ?",
            (tag, artifact_id),
        )
        await self._conn.commit()

    async def rename_tag(self, old_tag: str, new_tag: str) -> None:
        await self._ensure_conn()
        await self._execute(
            "UPDATE tags SET tag = ? WHERE tag = ?",
            (new_tag, old_tag),
        )
        await self._conn.commit()

    # -- Glossary ops --

    async def upsert_glossary_entries(self, entries: list[GlossaryEntry], run_id: str) -> int:
        count = 0
        for e in entries:
            await self.upsert_glossary_entry(e)
            count += 1
        return count

    async def upsert_glossary_entry(self, entry: GlossaryEntry) -> None:
        await self._ensure_conn()
        # ON CONFLICT DO UPDATE (not INSERT OR REPLACE): preserves term_id so
        # the content-backed glossary_fts rowid linkage stays valid, and keeps
        # first_seen_* provenance from the original insertion.
        sql = """
            INSERT INTO glossary (
                term, term_canonical, kind, short_def, long_def,
                acronym_expansion, related_terms, domain_tags, confidence,
                first_seen_run_id, first_seen_artifact_id, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(term_canonical) DO UPDATE SET
                term = excluded.term,
                kind = excluded.kind,
                short_def = excluded.short_def,
                long_def = excluded.long_def,
                acronym_expansion = excluded.acronym_expansion,
                related_terms = excluded.related_terms,
                domain_tags = excluded.domain_tags,
                confidence = excluded.confidence,
                last_updated = excluded.last_updated
        """
        await self._execute(
            sql,
            (
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
            ),
        )
        await self._conn.commit()

    async def get_glossary_entry(self, term_canonical: str) -> GlossaryEntry | None:
        await self._ensure_conn()
        row = await self._fetchone(
            "SELECT * FROM glossary WHERE term_canonical = ?", (term_canonical,)
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
            INSERT OR REPLACE INTO artifact_versions (
                artifact_id_old, artifact_id_new, reason, discovered_at, discovered_in_run
            ) VALUES (?, ?, ?, ?, ?)
        """
        await self._execute(
            sql,
            (
                old_id,
                new_id,
                reason,
                datetime.now(UTC).isoformat(),
                run_id,
            ),
        )
        await self._conn.commit()

    async def start_refresh_job(self, scope_kind: str, scope_value: str) -> str:
        await self._ensure_conn()
        import uuid
        from datetime import UTC, datetime

        job_id = str(uuid.uuid4())
        sql = """
            INSERT INTO refresh_jobs (
                job_id, started_at, scope_kind, scope_value, status
            ) VALUES (?, ?, ?, ?, 'running')
        """
        await self._execute(
            sql,
            (
                job_id,
                datetime.now(UTC).isoformat(),
                scope_kind,
                scope_value,
            ),
        )
        await self._conn.commit()
        return job_id

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
                completed_at = ?,
                artifacts_considered = ?,
                artifacts_refreshed = ?,
                status = ?,
                error = ?
            WHERE job_id = ?
        """
        await self._execute(
            sql,
            (
                datetime.now(UTC).isoformat(),
                considered,
                refreshed,
                status,
                error,
                job_id,
            ),
        )
        await self._conn.commit()

    async def get_refresh_job(self, job_id: str) -> RefreshJobRow | None:
        await self._ensure_conn()
        sql = "SELECT * FROM refresh_jobs WHERE job_id = ?"
        cursor = await self._execute(sql, (job_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return RefreshJobRow(
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

    # -- Deletion --

    async def delete_report(self, run_id: str) -> None:
        """Delete a report and all its dependent rows (analyses, tags,
        citation_edges, search_index entries) — but NOT artifacts, since
        artifacts may outlive their originating report (e.g. cited by
        later reports)."""
        await self._ensure_conn()
        # Cascade-delete analyses (and their FTS rows) created by this run;
        # tags applied_in_run & citation_edges discovered_in_run likewise.
        # Order matters: analyses → tags → citation_edges → reports itself.
        fts_cursor = await self._execute(
            "DELETE FROM search_index WHERE analysis_id IN "
            "(SELECT analysis_id FROM analyses WHERE run_id = ?)",
            (run_id,),
        )
        await fts_cursor.close()
        await self._execute("DELETE FROM analyses WHERE run_id = ?", (run_id,))
        await self._execute("DELETE FROM tags WHERE applied_in_run = ?", (run_id,))
        await self._execute("DELETE FROM citation_edges WHERE discovered_in_run = ?", (run_id,))
        # Nullify glossary entries and artifact_versions that reference this
        # run (preserve the data itself — it may be relevant to other runs).
        await self._execute(
            "UPDATE glossary SET first_seen_run_id = NULL WHERE first_seen_run_id = ?",
            (run_id,),
        )
        await self._execute(
            "UPDATE artifact_versions SET discovered_in_run = NULL WHERE discovered_in_run = ?",
            (run_id,),
        )
        await self._execute("DELETE FROM reports WHERE run_id = ?", (run_id,))
        await self._conn.commit()

    async def delete_artifact(self, artifact_id: str) -> None:
        """Delete an artifact and its dependent rows (analyses + FTS rows,
        tags, citation_edges, artifact_versions).

        REFUSES (with IntegrityError) if any report still has
        `reports.artifact_id = ?` — caller must `UPDATE reports SET
        artifact_id = NULL` (or `delete_report`) first. This strictness
        protects the implicit "this artifact is the final output of
        report X" linkage."""
        await self._ensure_conn()
        # Delete FTS rows for analyses of this artifact
        fts_cursor = await self._execute(
            "DELETE FROM search_index WHERE analysis_id IN "
            "(SELECT analysis_id FROM analyses WHERE artifact_id = ?)",
            (artifact_id,),
        )
        await fts_cursor.close()
        await self._execute("DELETE FROM analyses WHERE artifact_id = ?", (artifact_id,))
        await self._execute("DELETE FROM tags WHERE artifact_id = ?", (artifact_id,))
        await self._execute(
            "DELETE FROM citation_edges WHERE source_artifact_id = ? OR target_artifact_id = ?",
            (artifact_id, artifact_id),
        )
        await self._execute(
            "DELETE FROM artifact_versions WHERE artifact_id_old = ? OR artifact_id_new = ?",
            (artifact_id, artifact_id),
        )
        # If a report still references this artifact via reports.artifact_id,
        # the DELETE on artifacts will fail with FK violation. That's the
        # desired behavior — caller should handle that linkage explicitly.
        await self._execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
        await self._conn.commit()

    # -- FTS --

    @staticmethod
    def _sanitize_fts_query(raw: str) -> str:
        """Strip FTS5 special characters so arbitrary user queries don't cause syntax errors.

        FTS5 treats punctuation like , ( ) " * : . - ? as operators.
        We keep only alphanumeric and whitespace, then join as an implicit AND.
        """
        import re

        cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", raw)
        tokens = cleaned.split()
        tokens = [t for t in tokens if t]
        return " ".join(tokens) if tokens else raw

    async def full_text_search(self, query: str, *, kind: str, limit: int) -> list[SearchHit]:
        await self._ensure_conn()
        safe_query = self._sanitize_fts_query(query)
        if kind == "any":
            sql = """
                SELECT a.artifact_id, a.title, a.authors, an.summary, an.key_findings
                FROM search_index
                JOIN analyses an ON an.analysis_id = search_index.analysis_id
                JOIN artifacts a ON a.artifact_id = an.artifact_id
                WHERE search_index MATCH ?
                LIMIT ?
            """
            rows = await self._fetchall(sql, (safe_query, limit))
        else:
            sql = """
                SELECT a.artifact_id, a.title, a.authors, an.summary, an.key_findings
                FROM search_index
                JOIN analyses an ON an.analysis_id = search_index.analysis_id
                JOIN artifacts a ON a.artifact_id = an.artifact_id
                WHERE search_index MATCH ? AND a.kind = ?
                LIMIT ?
            """
            rows = await self._fetchall(sql, (safe_query, kind, limit))
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
                )
            )
        return results

    async def glossary_search(self, query: str, limit: int) -> list[GlossaryEntry]:
        await self._ensure_conn()
        safe_query = self._sanitize_fts_query(query)
        sql = """
            SELECT g.* FROM glossary_fts
            JOIN glossary g ON g.term_id = glossary_fts.rowid
            WHERE glossary_fts MATCH ?
            LIMIT ?
        """
        rows = await self._fetchall(sql, (safe_query, limit))
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
