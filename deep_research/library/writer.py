from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deep_research.library.storage.base import StorageBackend
from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    CitationEdgeRow,
    GlossaryEntry,
    ReportRow,
    TagRow,
)
from deep_research.state import PaperAnalysis, Report, SourceAnalysis

logger = logging.getLogger(__name__)

_DEFAULT_STALE_DAYS: dict[str, int] = {
    "arxiv": 365,
    "blog": 30,
    "html": 14,
    "research_report": 0,
}


def _content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _compute_refresh_after(source_type: str | None, policy: dict[str, int] | None) -> str | None:
    days = (policy or _DEFAULT_STALE_DAYS).get(source_type or "html", 30)
    if days <= 0:
        return None
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


class LibraryWriter:
    """Persists artifacts + metadata at well-defined seam points.
    Backend-agnostic: all SQL goes through `storage` Protocol.
    """

    def __init__(
        self,
        storage: StorageBackend,
        root_dir: str,
        refresh_policy: dict[str, int] | None = None,
    ) -> None:
        self._storage = storage
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._refresh_policy = refresh_policy or _DEFAULT_STALE_DAYS
        self._run_id: str = ""

    @property
    def storage(self) -> StorageBackend:
        return self._storage

    @property
    def root_dir(self) -> Path:
        return self._root

    def set_run_id(self, run_id: str) -> None:
        self._run_id = run_id

    # -- Artifact archival --

    async def archive_pdf(
        self,
        path: Path,
        *,
        arxiv_id: str | None = None,
        source_url: str | None = None,
        title: str | None = None,
        source_type: str | None = None,
    ) -> str:
        if not path.exists():
            logger.warning("archive_pdf: file not found %s", path)
            return ""
        pdf_bytes = path.read_bytes()
        sha = _content_sha256(pdf_bytes)
        rel_dir = self._root / "artifacts" / "pdf"
        rel_dir.mkdir(parents=True, exist_ok=True)
        slug_base = (arxiv_id or sha) + "-" + (title or "untitled").replace("/", "_")[:32]
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug_base).strip() or "unknown"
        dest = rel_dir / f"{slug}.pdf"
        if not dest.exists():
            import shutil

            shutil.copy2(str(path), str(dest))

        artifact = ArtifactRow(
            artifact_id=sha,
            kind="pdf",
            source_url=source_url,
            source_type=source_type or "arxiv",
            title=title,
            discovered_by="arxiv" if arxiv_id else "fetch_page",
            arxiv_id=arxiv_id,
            bytes_path=str(dest.relative_to(self._root)),
            bytes_size=len(pdf_bytes),
            first_seen_at=_now_iso(),
            last_touched_at=_now_iso(),
            refresh_after_at=_compute_refresh_after(source_type, self._refresh_policy),
        )
        await self._storage.upsert_artifact(artifact)
        return sha

    async def archive_html(self, url: str, html: str, pdf_bytes: bytes | None = None) -> str:
        sha = _content_sha256(html.encode("utf-8"))
        if pdf_bytes:
            pdf_sha = _content_sha256(pdf_bytes)
            rel_dir = self._root / "artifacts" / "pdf"
            rel_dir.mkdir(parents=True, exist_ok=True)
            dest = rel_dir / f"{pdf_sha}-blog.pdf"
            if not dest.exists():
                dest.write_bytes(pdf_bytes)

            artifact = ArtifactRow(
                artifact_id=pdf_sha,
                kind="pdf",
                source_url=url,
                source_type="blog",
                bytes_path=str(dest.relative_to(self._root)),
                bytes_size=len(pdf_bytes),
                first_seen_at=_now_iso(),
                last_touched_at=_now_iso(),
                refresh_after_at=_compute_refresh_after("blog", self._refresh_policy),
            )
            await self._storage.upsert_artifact(artifact)
            return pdf_sha
        else:
            rel_dir = self._root / "artifacts" / "html"
            html_dir = rel_dir / sha
            html_dir.mkdir(parents=True, exist_ok=True)
            (html_dir / "page.html").write_text(html)
            (html_dir / "meta.json").write_text(json.dumps({"url": url}, indent=2))

            artifact = ArtifactRow(
                artifact_id=sha,
                kind="html",
                source_url=url,
                source_type="html",
                bytes_path=str(html_dir.relative_to(self._root)),
                bytes_size=len(html.encode("utf-8")),
                first_seen_at=_now_iso(),
                last_touched_at=_now_iso(),
                refresh_after_at=_compute_refresh_after("html", self._refresh_policy),
            )
            await self._storage.upsert_artifact(artifact)
            return sha

    async def archive_report(
        self, report: Report, run_id: str, config_snapshot: dict | None = None
    ) -> str:
        reports_dir = self._root / "reports"
        now = datetime.now(UTC)
        ymd_dir = reports_dir / f"{now.year}" / f"{now.month:02d}" / f"{now.day:02d}"
        ymd_dir.mkdir(parents=True, exist_ok=True)

        md_path = ymd_dir / f"{run_id}.md"
        md_path.write_text(report.markdown)

        pdf_bytes = await _render_pdf(report.markdown, ymd_dir / f"{run_id}.pdf")
        artifact_id = None
        if pdf_bytes:
            sha = _content_sha256(pdf_bytes)
            pdf_dir = self._root / "artifacts" / "pdf"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            dest = pdf_dir / f"{sha}-report.pdf"
            if not dest.exists():
                dest.write_bytes(pdf_bytes)
            artifact_id = sha
            artifact = ArtifactRow(
                artifact_id=sha,
                kind="pdf",
                source_type="research_report",
                title=f"Report {run_id[:12]}",
                bytes_path=str(dest.relative_to(self._root)),
                bytes_size=len(pdf_bytes),
                first_seen_at=_now_iso(),
                last_touched_at=_now_iso(),
                refresh_after_at=None,
            )
            await self._storage.upsert_artifact(artifact)

        if not artifact_id:
            md_sha = _content_sha256(report.markdown.encode("utf-8"))
            artifact = ArtifactRow(
                artifact_id=md_sha,
                kind="report",
                source_type="research_report",
                title=f"Report {run_id[:12]}",
                bytes_path=str(md_path.relative_to(self._root)),
                bytes_size=len(report.markdown.encode("utf-8")),
                first_seen_at=_now_iso(),
                last_touched_at=_now_iso(),
                refresh_after_at=None,
            )
            await self._storage.upsert_artifact(artifact)
            artifact_id = md_sha

        report_row = ReportRow(
            run_id=run_id,
            started_at=report.created_at.isoformat() if report.created_at else _now_iso(),
            completed_at=_now_iso(),
            original_query=report.query or "",
            path_taken=report.path or "",
            classifier_rationale=report.classifier_rationale,
            iterations=report.iterations,
            config_snapshot=json.dumps(config_snapshot) if config_snapshot else None,
            markdown=report.markdown,
            artifact_id=artifact_id,
            citations_json=json.dumps([c.model_dump(mode="json") for c in report.citations])
            if report.citations
            else None,
            classifier_json=report.classifier_rationale if report.classifier_rationale else None,
        )
        await self._storage.insert_report(report_row)
        return artifact_id

    # -- Derived records --

    async def record_analysis(
        self,
        artifact_id: str,
        analysis: PaperAnalysis | SourceAnalysis | dict[str, Any],
        run_id: str,
        analyzer: str,
    ) -> str:
        if isinstance(analysis, dict):
            d = analysis
        else:
            d = analysis.model_dump() if hasattr(analysis, "model_dump") else {}

        row = AnalysisRow(
            analysis_id=str(uuid.uuid4())[:16],
            artifact_id=artifact_id,
            run_id=run_id,
            analyzer=analyzer,
            summary=d.get("summary"),
            key_findings=json.dumps(d.get("key_findings") or []),
            methodology=d.get("methodology"),
            limitations=d.get("limitations"),
            gaps=d.get("gaps"),
            follow_ups=d.get("follow_ups"),
            key_references=json.dumps(d.get("key_references") or []),
            relevance_to_query=d.get("relevance_to_query"),
            analyzed_at=_now_iso(),
        )
        await self._storage.insert_analysis(row)
        return row.analysis_id

    async def record_citation_edge(
        self,
        source_aid: str,
        target_arxiv_id: str,
        weight: float,
        run_id: str,
        rationale: str = "",
    ) -> None:
        edge = CitationEdgeRow(
            source_artifact_id=source_aid,
            target_arxiv_id=target_arxiv_id,
            rationale=rationale or None,
            weight=weight,
            discovered_in_run=run_id,
        )
        await self._storage.insert_citation_edge(edge)

    async def tag(self, artifact_id: str, tags: list[str], run_id: str | None = None) -> None:
        for t in tags:
            tag_row = TagRow(tag=t, artifact_id=artifact_id, applied_in_run=run_id)
            await self._storage.upsert_tag(tag_row)

    async def upsert_glossary_entries(self, entries: list[GlossaryEntry], run_id: str) -> int:
        return await self._storage.upsert_glossary_entries(entries, run_id)

    # -- Refresh foundation (P10.5b) --

    async def refresh_needed(self, scope_kind: str, scope_value: str, limit: int = 100) -> list:
        return await self._storage.artifacts_needing_refresh(scope_kind, scope_value, limit)

    async def probe_upstream(self, artifact_id: str) -> dict[str, Any]:
        logger.info(
            "probe_upstream not implemented yet (artifact_id=%s); marking unchanged", artifact_id
        )
        return {"changed": False, "new_sha": "", "error": None}

    async def run_refresh_job(
        self, scope_kind: str, scope_value: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        job_id = await self._storage.start_refresh_job(scope_kind, scope_value)
        artifacts = await self._storage.artifacts_needing_refresh(
            scope_kind, scope_value, limit=100
        )
        considered = len(artifacts)
        refreshed = 0
        unchanged = 0
        errored = 0
        new_versions = 0

        for art in artifacts:
            if dry_run:
                continue
            try:
                result = await self.probe_upstream(art.artifact_id)
                if result.get("changed") is False:
                    unchanged += 1
                else:
                    refreshed += 1
                    if result.get("new_sha"):
                        new_versions += 1
            except Exception as e:
                logger.warning("refresh error for %s: %s", art.artifact_id, e)
                errored += 1

        status = "completed" if errored == 0 else "partial"
        await self._storage.complete_refresh_job(
            job_id,
            considered,
            refreshed,
            status,
            error=f"{errored} errors" if errored else None,
        )
        return {
            "considered": considered,
            "refreshed": refreshed,
            "unchanged": unchanged,
            "errored": errored,
            "new_versions": new_versions,
            "job_id": job_id,
        }


async def _render_pdf(markdown_text: str, pdf_path: Path) -> bytes | None:
    import markdown

    html = markdown.markdown(markdown_text, extensions=["extra"])
    html = f"<!DOCTYPE html><html><body>{html}</body></html>"

    try:
        from weasyprint import HTML

        HTML(string=html).write_pdf(str(pdf_path))
        return pdf_path.read_bytes()
    except Exception as e:
        logger.warning("weasyprint unavailable (%s); falling back to xhtml2pdf", e)
        try:
            from xhtml2pdf import pisa

            with open(str(pdf_path), "wb") as fh:
                pisa.CreatePDF(html, dest=fh)
            return pdf_path.read_bytes()
        except Exception as e2:
            logger.warning("xhtml2pdf also failed (%s); falling back to markdown-only", e2)
            return None


class NullLibraryWriter:
    """No-op writer that does nothing. Used when pdl.enabled=false."""

    def set_run_id(self, run_id: str) -> None:
        pass

    async def archive_pdf(self, *args, **kwargs) -> str:
        return ""

    async def archive_html(self, *args, **kwargs) -> str:
        return ""

    async def archive_report(self, *args, **kwargs) -> str:
        return ""

    async def record_analysis(self, *args, **kwargs) -> str:
        return ""

    async def record_citation_edge(self, *args, **kwargs) -> None:
        pass

    async def tag(self, *args, **kwargs) -> None:
        pass

    async def upsert_glossary_entries(self, *args, **kwargs) -> int:
        return 0

    async def refresh_needed(self, *args, **kwargs) -> list:
        return []

    async def probe_upstream(self, *args, **kwargs) -> dict:
        return {"changed": False, "new_sha": "", "error": None}

    async def run_refresh_job(self, *args, **kwargs) -> dict:
        return {"considered": 0, "refreshed": 0, "unchanged": 0, "errored": 0, "new_versions": 0}
