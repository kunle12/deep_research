from __future__ import annotations

import asyncio
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

    # -- Report lifecycle --

    async def begin_report(self, run_id: str, original_query: str) -> None:
        """Create the `reports` row for a run up-front so run-scoped rows
        (analyses, citation_edges, tags, glossary) that reference
        `reports(run_id)` satisfy their FK before research starts.

        No-op if the row already exists (e.g. resuming a run that was
        already archived) — never clobber a completed report. The
        placeholder fields are overwritten by `archive_report` at the end.
        """
        if not run_id or not original_query:
            return
        if await self._storage.get_report(run_id) is not None:
            return
        await self._storage.insert_report(
            ReportRow(
                run_id=run_id,
                started_at=_now_iso(),
                original_query=original_query,
                path_taken="",  # placeholder; set by archive_report
                markdown="",    # placeholder; set by archive_report
            )
        )

    async def delete_report(self, run_id: str) -> None:
        """Best-effort removal of a report row and its run-scoped rows.

        Used to clean up a placeholder report row when a run fails before
        archiving (so a broken run leaves no trace in the library).
        """
        if not run_id:
            return
        await self._storage.delete_report(run_id)


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
        rel_dir = self._root / "artifacts" / "pdf"
        dest, size, sha = await asyncio.to_thread(
            _copy_pdf_to_store, path, rel_dir, arxiv_id, title
        )

        artifact = ArtifactRow(
            artifact_id=sha,
            kind="pdf",
            source_url=source_url,
            source_type=source_type or "arxiv",
            title=title,
            discovered_by="arxiv" if arxiv_id else "fetch_page",
            arxiv_id=arxiv_id,
            bytes_path=str(dest.relative_to(self._root)),
            bytes_size=size,
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
            dest, size = await asyncio.to_thread(
                _write_blog_pdf_to_store, self._root, pdf_sha, pdf_bytes
            )

            artifact = ArtifactRow(
                artifact_id=pdf_sha,
                kind="pdf",
                source_url=url,
                source_type="html",
                bytes_path=str(dest.relative_to(self._root)),
                bytes_size=size,
                first_seen_at=_now_iso(),
                last_touched_at=_now_iso(),
                refresh_after_at=_compute_refresh_after("html", self._refresh_policy),
            )
            await self._storage.upsert_artifact(artifact)
            return pdf_sha
        else:
            html_dir, size = await asyncio.to_thread(
                _write_html_to_store, self._root, sha, url, html
            )

            artifact = ArtifactRow(
                artifact_id=sha,
                kind="html",
                source_url=url,
                source_type="html",
                bytes_path=str(html_dir.relative_to(self._root)),
                bytes_size=size,
                first_seen_at=_now_iso(),
                last_touched_at=_now_iso(),
                refresh_after_at=_compute_refresh_after("html", self._refresh_policy),
            )
            await self._storage.upsert_artifact(artifact)
            return sha

    async def archive_image(self, url: str, image_bytes: bytes) -> str:
        """Archive a webpage screenshot as an image artifact (kind="image")."""
        sha = _content_sha256(image_bytes)
        dest, size = await asyncio.to_thread(
            _write_image_to_store, self._root, sha, image_bytes
        )

        artifact = ArtifactRow(
            artifact_id=sha,
            kind="image",
            source_url=url,
            source_type="html",
            bytes_path=str(dest.relative_to(self._root)),
            bytes_size=size,
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

        md_path, pdf_bytes = await asyncio.to_thread(
            _write_report_files, ymd_dir, run_id, report.markdown
        )
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


def _copy_pdf_to_store(
    src: Path, dest_dir: Path, arxiv_id: str | None, title: str | None
) -> tuple[Path, int, str]:
    """Sync: copy a PDF into the artifact store, returning (dest, size, sha).

    The destination slug is derived from the content sha (plus arxiv_id when
    present) so distinct PDFs can never collide on the same file even when
    they share a title. This mirrors the pre-refactor behavior.
    """
    import shutil

    pdf_bytes = src.read_bytes()
    sha = _content_sha256(pdf_bytes)
    dest_dir.mkdir(parents=True, exist_ok=True)
    slug_base = (arxiv_id or sha) + "-" + (title or "untitled").replace("/", "_")[:32]
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug_base).strip() or "unknown"
    dest = dest_dir / f"{slug}.pdf"
    if not dest.exists():
        shutil.copy2(str(src), str(dest))
    return dest, len(pdf_bytes), sha


def _write_blog_pdf_to_store(root: Path, pdf_sha: str, pdf_bytes: bytes) -> tuple[Path, int]:
    """Sync: write a blog PDF artifact, returning (dest, size)."""
    dest_dir = root / "artifacts" / "pdf"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{pdf_sha}-blog.pdf"
    if not dest.exists():
        dest.write_bytes(pdf_bytes)
    return dest, len(pdf_bytes)


def _write_html_to_store(root: Path, sha: str, url: str, html: str) -> tuple[Path, int]:
    """Sync: write an HTML artifact directory, returning (html_dir, byte size)."""
    html_dir = root / "artifacts" / "html" / sha
    html_dir.mkdir(parents=True, exist_ok=True)
    (html_dir / "page.html").write_text(html)
    (html_dir / "meta.json").write_text(json.dumps({"url": url}, indent=2))
    return html_dir, len(html.encode("utf-8"))


def _write_image_to_store(root: Path, sha: str, image_bytes: bytes) -> tuple[Path, int]:
    """Sync: write a webpage-screenshot PNG artifact, returning (dest, size)."""
    dest_dir = root / "artifacts" / "image"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{sha}.png"
    if not dest.exists():
        dest.write_bytes(image_bytes)
    return dest, len(image_bytes)


def _write_report_files(
    ymd_dir: Path, run_id: str, markdown_text: str
) -> tuple[Path, bytes | None]:
    """Sync: write the markdown file and render the PDF, returning (md_path, pdf_bytes).

    PDF rendering (weasyprint/xhtml2pdf) is CPU-heavy and blocking, so this
    whole unit runs via asyncio.to_thread from archive_report.
    """
    md_path = ymd_dir / f"{run_id}.md"
    md_path.write_text(markdown_text)
    pdf_bytes = _render_pdf(markdown_text, ymd_dir / f"{run_id}.pdf")
    return md_path, pdf_bytes


def _render_pdf(markdown_text: str, pdf_path: Path) -> bytes | None:
    import markdown

    html = markdown.markdown(markdown_text, extensions=["extra"])
    # Explicitly left-align text: keeps weasyprint/xhtml2pdf output readable
    # even if a future template introduces centered defaults.
    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        "<style>body { text-align: left; } "
        "h1, h2, h3, h4, h5, h6, p, li, blockquote, td, th { text-align: left; }</style>"
        f"</head><body>{html}</body></html>"
    )

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


def remove_report_files(root: Path, run_id: str, date_str: str) -> None:
    """Remove the on-disk `{run_id}.md`/`.pdf` under the report date dir.

    The report files live under `reports/{year}/{month}/{day}/` derived from
    the report's `completed_at` (or `started_at`). Best-effort; never raises.
    """
    try:
        dt = datetime.fromisoformat(date_str)
    except (ValueError, TypeError):
        return
    day_dir = root / "reports" / f"{dt.year}" / f"{dt.month:02d}" / f"{dt.day:02d}"
    for suffix in (".md", ".pdf"):
        candidate = day_dir / f"{run_id}{suffix}"
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError as e:
            logger.warning("could not remove %s: %s", candidate, e)


class NullLibraryWriter:
    """No-op writer that does nothing. Used when pdl.enabled=false."""

    def set_run_id(self, run_id: str) -> None:
        pass

    async def begin_report(self, *args, **kwargs) -> None:
        pass

    async def delete_report(self, *args, **kwargs) -> None:
        pass


    async def archive_pdf(self, *args, **kwargs) -> str:
        return ""

    async def archive_html(self, *args, **kwargs) -> str:
        return ""

    async def archive_image(self, *args, **kwargs) -> str:
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
