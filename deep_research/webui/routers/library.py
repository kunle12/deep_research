"""Library browsing API — reports, tags, artifacts, search, stats."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from deep_research.citations import normalize_url, render_bibliography_markdown
from deep_research.config import AgentTopConfig
from deep_research.library.citation_archive import archive_cited_pdf
from deep_research.library.storage.base import StorageBackend
from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    CitationEdgeRow,
    GlossaryEntry,
    TagRow,
)
from deep_research.library.writer import LibraryWriter, remove_artifact_files
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.state import Citation
from deep_research.tools import arxiv as arxiv_tool
from deep_research.util import strip_arxiv_version
from deep_research.webui.deps import get_config, get_root_dir, get_storage
from deep_research.webui.format import citation_count, clean_inline, make_snippet, parse_citations
from deep_research.webui.models import (
    AnalysisInfo,
    ArtifactDetail,
    ArtifactListItem,
    ArtifactListResponse,
    CitationEdgeInfo,
    DeleteArtifactResponse,
    DeleteReportReferenceRequest,
    DeleteReportResponse,
    GlossaryInfo,
    MergeReportsRequest,
    MergeReportsResponse,
    RenameReportRequest,
    ReportDetail,
    ReportListItem,
    ReportListResponse,
    SearchHitItem,
    SearchResponse,
    StatsResponse,
    TagInfo,
    TagUpdateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["library"])

# First heading of any level (matches the web UI's title extraction).
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")

# arXiv id like 2401.12345 or 2401.12345v2 (scholar: ids are rejected later).
_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")


class TagBody(BaseModel):
    tag: str = Field(min_length=1, max_length=64)


class ArxivPdfBody(BaseModel):
    arxiv_id: str = Field(min_length=3, max_length=64)


class ArxivPdfResponse(BaseModel):
    local_pdf_url: str | None = None
    archived: bool = False
    error: str | None = None


def _report_title(markdown: str) -> str:
    """Extract the report's display title from the markdown's first heading.

    Mirrors the web UI's title extraction (first heading of any level, inline
    formatting stripped), so list cards match what the detail view shows.
    """
    for line in (markdown or "").splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            return clean_inline(m.group(1))
    return ""


def _safe_resolve(root: Path, bytes_path: str | None) -> Path | None:
    """Resolve *bytes_path* under *root* and return the file path only when it
    is inside *root* and exists on disk (path-traversal + staleness guard)."""
    if not bytes_path:
        return None
    root_resolved = root.resolve()
    file_path = (root_resolved / bytes_path).resolve()
    if not file_path.is_relative_to(root_resolved) or not file_path.is_file():
        return None
    return file_path


def _artifact_has_pdf(root: Path, artifact: ArtifactRow | None) -> bool:
    """True when *artifact* is a PDF whose bytes are actually on disk (so a
    list/detail `has_pdf` flag never advertises a file that would 404)."""
    if artifact is None or artifact.kind != "pdf":
        return False
    return _safe_resolve(root, artifact.bytes_path) is not None


def _safe_filename(name: str) -> str:
    """Sanitize a user-influenced value for a quoted Content-Disposition
    filename, so CR/LF or `"` can't break out of the header (response split)."""
    return re.sub(r'[\r\n"\\]', "_", name or "")


async def _report_items(backend: StorageBackend, root: Path, reports) -> list[ReportListItem]:
    """Enrich report rows with tags, title, snippets, and PDF availability."""
    art_ids = [r.artifact_id for r in reports if r.artifact_id]
    tags_map = await backend.get_tags_for_artifacts(art_ids)
    artifacts_map = await backend.get_artifacts(art_ids)
    items: list[ReportListItem] = []
    for r in reports:
        art = artifacts_map.get(r.artifact_id or "")
        items.append(
            ReportListItem(
                run_id=r.run_id,
                started_at=r.started_at,
                completed_at=r.completed_at,
                query=r.original_query,
                title=_report_title(r.markdown) or r.original_query,
                path=r.path_taken,
                iterations=r.iterations,
                tags=sorted(t.tag for t in tags_map.get(r.artifact_id, [])),
                snippet=make_snippet(r.markdown),
                citation_count=citation_count(r.citations_json),
                markdown_length=len(r.markdown),
                has_pdf=_artifact_has_pdf(root, art),
            )
        )
    return items


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _glossary_info(e: GlossaryEntry) -> GlossaryInfo:
    return GlossaryInfo(
        term=e.term,
        term_canonical=e.term_canonical,
        kind=e.kind,
        short_def=e.short_def,
        long_def=e.long_def,
        acronym_expansion=e.acronym_expansion,
        related_terms=_parse_json_list(e.related_terms),
        domain_tags=_parse_json_list(e.domain_tags),
        confidence=e.confidence,
        first_seen_run_id=e.first_seen_run_id,
        first_seen_artifact_id=e.first_seen_artifact_id,
        last_updated=e.last_updated,
    )


async def _report_glossary(backend: StorageBackend, run_id: str) -> list[GlossaryInfo]:
    """Glossary entries attributed to a report (terms first seen in its run)."""
    entries = await backend.list_glossary_entries(run_id=run_id)
    return [_glossary_info(e) for e in entries]


def _render_citations_bib(citations: list[Citation]) -> str:
    """Emit a .bib file for the report's citations."""
    entries: list[str] = []
    for i, c in enumerate(citations, start=1):
        title = _bib_escape(c.title or c.url or "Untitled")
        authors = " and ".join(c.authors) if c.authors else "Anonymous"
        url = _bib_escape(c.url or "")
        doi = _bib_escape(c.doi or "") if c.doi else ""
        lines = [f"@misc{{cite{i},\n", f"  title = {{{title}}},\n", f"  author = {{{authors}}},\n"]
        if doi:
            lines.append(f"  doi = {{{doi}}},\n")
        lines.append(f"  url = {{{url}}},\n")
        lines.append(f"  year = {{{c.year or 'unknown'}}}\n")
        lines.append("}\n")
        entries.append("".join(lines))
    return "% Generated by deep-research web UI\n" + "".join(entries)


def _bib_escape(s: str) -> str:
    return s.replace("{", "\\{").replace("}", "\\}")


def _citations_objects(citations: list[dict[str, Any]]) -> list[Citation]:
    """Map stored citation dicts to Citation objects for rendering."""
    out: list[Citation] = []
    for c in citations:
        try:
            out.append(Citation.model_validate(c))
        except Exception:
            continue
    return out


def _analysis_info(a: AnalysisRow) -> AnalysisInfo:
    return AnalysisInfo(
        analysis_id=a.analysis_id,
        artifact_id=a.artifact_id,
        run_id=a.run_id,
        analyzer=a.analyzer,
        summary=a.summary,
        key_findings=_parse_json_list(a.key_findings),
        methodology=a.methodology,
        limitations=a.limitations,
        gaps=a.gaps,
        follow_ups=a.follow_ups,
        key_references=_parse_json_list(a.key_references),
        relevance_to_query=a.relevance_to_query,
        relevance_score=a.relevance_score,
        analyzed_at=a.analyzed_at,
    )


def _edge_info(e: CitationEdgeRow) -> CitationEdgeInfo:
    return CitationEdgeInfo(
        source_artifact_id=e.source_artifact_id,
        target_artifact_id=e.target_artifact_id,
        target_arxiv_id=e.target_arxiv_id,
        rationale=e.rationale,
        weight=e.weight,
        discovered_in_run=e.discovered_in_run,
    )


async def _citation_artifact(
    backend: StorageBackend,
    citation: dict[str, Any],
) -> ArtifactRow | None:
    """Find the locally archived artifact for a citation, if any.

    Looks up by arxiv_id first (academic path keys PDFs that way), then by the
    citation's URL (the HTML/blog archive seam stores source_url on every
    artifact). Returns the artifact regardless of kind so callers can decide
    what to expose.
    """
    aid = citation.get("arxiv_id")
    artifact: ArtifactRow | None = None
    if isinstance(aid, str) and aid and not aid.startswith("scholar:"):
        artifact = await backend.find_artifact_by_arxiv_id(strip_arxiv_version(aid))
    if artifact is None:
        url = citation.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            artifact = await backend.find_artifact_by_url(url)
    return artifact


def _artifact_file_route(root: Path, artifact: ArtifactRow, route: str) -> str | None:
    """Return the API route for *artifact*'s file if it exists on disk, else None."""
    if _safe_resolve(root, artifact.bytes_path) is None:
        return None
    return route


async def _enrich_citations(
    backend: StorageBackend,
    root: Path,
    citations_json: str | None,
) -> list[dict[str, Any]]:
    """Attach `local_pdf_url` / `local_image_url` (and `local_artifact_id` /
    `local_artifact_kind` for the Remove-from-library action) to citations that
    have an archived PDF or screenshot copy. Each citation's artifact is looked
    up once (not re-fetched per URL kind)."""
    citations = parse_citations(citations_json)
    for c in citations:
        artifact = await _citation_artifact(backend, c)
        if artifact is not None and artifact.bytes_path:
            if artifact.kind == "pdf":
                c["local_pdf_url"] = _artifact_file_route(
                    root, artifact, f"/api/artifacts/{artifact.artifact_id}/pdf"
                )
            elif artifact.kind == "image":
                c["local_image_url"] = _artifact_file_route(
                    root, artifact, f"/api/artifacts/{artifact.artifact_id}/image"
                )
            c["local_artifact_id"] = artifact.artifact_id
            c["local_artifact_kind"] = artifact.kind
    return citations


async def _require_report_artifact(backend: StorageBackend, run_id: str) -> str:
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    if not report.artifact_id:
        raise HTTPException(status_code=400, detail="report has no artifact; cannot manage tags")
    return report.artifact_id


def _citation_matches(c: dict[str, Any], url: str, arxiv_id: str) -> bool:
    """True when a stored citation matches the reference-delete key (URL or
    arXiv id). URLs compare via `normalize_url` so trailing slashes and
    punctuation don't defeat the match."""
    if url:
        if c.get("url") == url:
            return True
        if normalize_url(c.get("url") or "") == normalize_url(url):
            return True
    if arxiv_id:
        a = (c.get("arxiv_id") or "").strip()
        if a and a == arxiv_id:
            return True
    return False


def _replace_bibliography_section(markdown: str, citations: list[dict[str, Any]]) -> str:
    """Regenerate the markdown `## Bibliography` section from *citations*.

    When the report has a `## Bibliography` heading, its block (up to the next
    `#` heading or EOF) is replaced with the freshly rendered bibliography —
    so the `.md` download reflects the deleted reference. Reports without that
    heading are returned unchanged (`citations_json` still drives the `.bib`
    and bibliography exports)."""
    lines = markdown.split("\n")
    idx = None
    for i, line in enumerate(lines):
        if line.strip().lower() == "## bibliography":
            idx = i
            break
    if idx is None:
        return markdown
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if _HEADING_RE.match(lines[j].strip()):
            end = j
            break
    bib = render_bibliography_markdown(_citations_objects(citations))
    new_block = bib.split("\n") if bib else []
    return "\n".join(lines[:idx] + new_block + lines[end:])


async def _other_cited_artifact_ids(
    backend: StorageBackend, run_id: str
) -> set[str]:
    """Set of artifact_ids cited by reports other than *run_id* (shared copies
    that must survive when a reference is removed). Computed once per request
    so the delete loop never re-scans the report table per citation."""
    cited: set[str] = set()
    reports = await backend.list_reports(limit=100000)
    for r in reports:
        if r.run_id == run_id:
            continue
        for c in parse_citations(r.citations_json):
            other = await _citation_artifact(backend, c)
            if other is not None:
                cited.add(other.artifact_id)
    return cited


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/reports", response_model=ReportListResponse)
async def list_reports(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, min_length=1, max_length=200),
    tag: str | None = Query(None, max_length=64),
    path: str | None = Query(None, max_length=32),
) -> ReportListResponse:
    backend = get_storage(request)
    if q:
        reports = await backend.search_reports(q, limit=limit, offset=offset, tag=tag, path=path)
        total = await backend.count_reports(q=q, tag=tag, path=path)
    else:
        reports = await backend.list_reports(limit=limit, offset=offset, tag=tag, path=path)
        total = await backend.count_reports(tag=tag, path=path)
    items = await _report_items(backend, get_root_dir(request), reports)
    return ReportListResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/reports/{run_id}", response_model=ReportDetail)
async def get_report_detail(run_id: str, request: Request) -> ReportDetail:
    backend = get_storage(request)
    root = get_root_dir(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    tags: list[str] = []
    artifact: ArtifactRow | None = None
    if report.artifact_id:
        artifact = await backend.get_artifact(report.artifact_id)
        tag_rows = await backend.get_tags_for_artifact(report.artifact_id)
        tags = sorted(t.tag for t in tag_rows)
    has_pdf = _artifact_has_pdf(root, artifact)
    return ReportDetail(
        run_id=report.run_id,
        started_at=report.started_at,
        completed_at=report.completed_at,
        query=report.original_query,
        path=report.path_taken,
        classifier_rationale=report.classifier_rationale,
        iterations=report.iterations,
        markdown=report.markdown,
        citations=await _enrich_citations(backend, root, report.citations_json),
        tags=tags,
        artifact_id=report.artifact_id,
        has_pdf=has_pdf,
        pdf_url=f"/api/reports/{run_id}/pdf" if has_pdf else None,
        markdown_url=f"/api/reports/{run_id}/markdown",
        glossary=await _report_glossary(backend, run_id),
        glossary_url=f"/api/reports/{run_id}/glossary/markdown",
        bibliography_url=f"/api/reports/{run_id}/bibliography",
        bibliography_bib_url=f"/api/reports/{run_id}/bibliography/bib",
    )


@router.get("/reports/{run_id}/markdown")
async def get_report_markdown(run_id: str, request: Request) -> PlainTextResponse:
    backend = get_storage(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    return PlainTextResponse(
        report.markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{_safe_filename(run_id)}.md"'},
    )


def _render_glossary_markdown(glossary: list[GlossaryInfo]) -> str:
    """Render the glossary entries as a markdown section (downloadable)."""
    if not glossary:
        return "# Glossary\n\n_No glossary terms recorded for this report._\n"
    lines = ["# Glossary", ""]
    for g in sorted(glossary, key=lambda x: x.term_canonical):
        lines.append(f"## {g.term}")
        if g.acronym_expansion:
            lines.append(f"_({g.acronym_expansion})_")
        if g.short_def:
            lines.append("")
            lines.append(g.short_def)
        if g.long_def:
            lines.append("")
            lines.append(g.long_def)
        if g.related_terms:
            lines.append("")
            lines.append(f"Related: {', '.join(g.related_terms)}")
        if g.domain_tags:
            lines.append(f"Tags: {', '.join(g.domain_tags)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@router.get("/reports/{run_id}/glossary/markdown")
async def get_report_glossary_markdown(run_id: str, request: Request) -> PlainTextResponse:
    backend = get_storage(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    md = _render_glossary_markdown(await _report_glossary(backend, run_id))
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(run_id)}-glossary.md"'},
    )


@router.get("/reports/{run_id}/bibliography")
async def get_report_bibliography(run_id: str, request: Request) -> PlainTextResponse:
    backend = get_storage(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    bib_md = render_bibliography_markdown(
        _citations_objects(parse_citations(report.citations_json))
    )
    if not bib_md:
        bib_md = "## Bibliography\n\n_No cited sources recorded for this report._\n"
    return PlainTextResponse(
        bib_md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(run_id)}-bibliography.md"'},
    )


@router.get("/reports/{run_id}/bibliography/bib")
async def get_report_bibliography_bib(run_id: str, request: Request) -> PlainTextResponse:
    backend = get_storage(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    bib = _render_citations_bib(_citations_objects(parse_citations(report.citations_json)))
    if not bib.strip():
        bib = "% No cited sources recorded for this report.\n"
    return PlainTextResponse(
        bib,
        media_type="application/x-bibtex; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(run_id)}.bib"'},
    )


def _serve_artifact_pdf(root: Path, artifact: ArtifactRow, filename: str) -> FileResponse:
    """Serve an archived PDF artifact's bytes as a FileResponse."""
    file_path = _safe_resolve(root, artifact.bytes_path)
    if file_path is None:
        raise HTTPException(status_code=404, detail="archived PDF file missing")
    return FileResponse(file_path, media_type="application/pdf", filename=filename)


@router.get("/reports/{run_id}/pdf")
async def get_report_pdf(run_id: str, request: Request) -> FileResponse:
    backend = get_storage(request)
    root = get_root_dir(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    artifact = await backend.get_artifact(report.artifact_id) if report.artifact_id else None
    if artifact is None or artifact.kind != "pdf":
        raise HTTPException(status_code=404, detail="no archived PDF for this report")
    return _serve_artifact_pdf(root, artifact, f"{_safe_filename(run_id)}.pdf")


@router.get("/artifacts/{artifact_id}/pdf")
async def get_artifact_pdf(artifact_id: str, request: Request) -> FileResponse:
    """Serve an archived PDF artifact (e.g. a paper PDF stored by the academic path)."""
    backend = get_storage(request)
    root = get_root_dir(request)
    artifact = await backend.get_artifact(artifact_id)
    if artifact is None or artifact.kind != "pdf":
        raise HTTPException(status_code=404, detail="no archived PDF for this artifact")
    return _serve_artifact_pdf(root, artifact, f"{artifact.artifact_id}.pdf")


@router.get("/artifacts/{artifact_id}/image")
async def get_artifact_image(artifact_id: str, request: Request) -> FileResponse:
    """Serve an archived webpage-screenshot image artifact (kind="image")."""
    backend = get_storage(request)
    root = get_root_dir(request)
    artifact = await backend.get_artifact(artifact_id)
    if artifact is None or artifact.kind != "image":
        raise HTTPException(status_code=404, detail="no archived image for this artifact")
    file_path = _safe_resolve(root, artifact.bytes_path)
    if file_path is None:
        raise HTTPException(status_code=404, detail="archived image file missing")
    media_type = mimetypes.guess_type(file_path.name)[0] or "image/png"
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=f"{artifact.artifact_id}{file_path.suffix or '.png'}",
    )


@router.post("/arxiv/pdf", response_model=ArxivPdfResponse)
async def archive_cited_arxiv_pdf(body: ArxivPdfBody, request: Request) -> ArxivPdfResponse:
    """On-demand: download + archive one arXiv paper PDF from a reference card."""
    backend = get_storage(request)
    root = get_root_dir(request)
    cfg: AgentTopConfig = get_config(request)
    aid = body.arxiv_id.strip()
    base = strip_arxiv_version(aid)
    if not base or base.startswith("scholar:") or not _ARXIV_ID_RE.fullmatch(base):
        raise HTTPException(status_code=422, detail="invalid arxiv id")

    existing = await backend.find_artifact_by_arxiv_id(base)
    if existing is not None and existing.kind == "pdf" and existing.bytes_path:
        return ArxivPdfResponse(local_pdf_url=f"/api/artifacts/{existing.artifact_id}/pdf")

    if not cfg.arxiv.enabled or not cfg.arxiv.download_pdfs:
        return ArxivPdfResponse(error="arxiv pdf downloads are disabled in config")

    reg = ToolRegistry()
    try:
        await arxiv_tool.register(reg, cfg)
        writer = LibraryWriter(backend, str(root))
        artifact_id = await archive_cited_pdf(aid, title=None, tools=reg, writer=writer)
    except Exception as exc:
        logger.warning("arxiv pdf download failed for %s: %s", aid, exc)
        return ArxivPdfResponse(error=f"download failed: {exc}")
    finally:
        await reg.close()

    if not artifact_id:
        return ArxivPdfResponse(error="download failed - paper may not be open access")
    return ArxivPdfResponse(local_pdf_url=f"/api/artifacts/{artifact_id}/pdf", archived=True)


@router.post("/reports/{run_id}/tags", response_model=TagUpdateResponse)
async def add_report_tag(run_id: str, body: TagBody, request: Request) -> TagUpdateResponse:
    backend = get_storage(request)
    artifact_id = await _require_report_artifact(backend, run_id)
    tag = body.tag.strip()
    if not tag:
        raise HTTPException(status_code=422, detail="tag cannot be blank")
    await backend.upsert_tag(TagRow(tag=tag, artifact_id=artifact_id, applied_in_run=None))
    rows = await backend.get_tags_for_artifact(artifact_id)
    return TagUpdateResponse(tags=sorted(t.tag for t in rows))


@router.delete("/reports/{run_id}/tags", response_model=TagUpdateResponse)
async def remove_report_tag(
    run_id: str,
    request: Request,
    tag: str = Query(..., min_length=1, max_length=64),
) -> TagUpdateResponse:
    backend = get_storage(request)
    artifact_id = await _require_report_artifact(backend, run_id)
    await backend.delete_tag(tag.strip(), artifact_id)
    rows = await backend.get_tags_for_artifact(artifact_id)
    return TagUpdateResponse(tags=sorted(t.tag for t in rows))


@router.delete("/reports/{run_id}", response_model=DeleteReportResponse)
async def delete_report(
    run_id: str,
    request: Request,
    confirm: bool = Query(False),
) -> DeleteReportResponse:
    if not confirm:
        raise HTTPException(status_code=400, detail="confirm=true is required to delete a report")
    backend = get_storage(request)
    root = get_root_dir(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    removed: list[str] = []
    date_str = report.completed_at or report.started_at
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str)
            day_dir = root / "reports" / f"{dt.year}" / f"{dt.month:02d}" / f"{dt.day:02d}"
            for suffix in (".md", ".pdf"):
                candidate = day_dir / f"{run_id}{suffix}"
                if candidate.exists():
                    candidate.unlink()
                    removed.append(str(candidate))
        except ValueError:
            pass
    # Delete the report row + its run-scoped rows.
    await backend.delete_report(run_id)
    # Clean up the report's own output artifact (report PDF / markdown) when no
    # other report still references it — delete_report deliberately keeps
    # artifacts, so without this the report's own artifact leaks forever.
    report_artifact_id = report.artifact_id
    if report_artifact_id:
        try:
            referencing = await backend.list_reports(limit=100000)
            if not any(r.artifact_id == report_artifact_id for r in referencing):
                art = await backend.get_artifact(report_artifact_id)
                if art is not None:
                    removed.extend(remove_artifact_files(root, art))
                    await backend.delete_artifact(report_artifact_id)
        except Exception as e:
            logger.warning("report artifact cleanup failed for %s: %s", report_artifact_id, e)
    return DeleteReportResponse(removed_files=removed)


@router.delete("/reports/{run_id}/references", response_model=ReportDetail)
async def delete_report_reference(
    run_id: str,
    request: Request,
    body: DeleteReportReferenceRequest,
    confirm: bool = Query(False),
) -> ReportDetail:
    """Remove a reference from a report.

    Deletes every citation matching the request's URL (or arXiv id) from the
    report's `citations_json`, regenerates the markdown bibliography section,
    and — when the removed citation had a locally archived copy that no other
    report still cites — removes that artifact (PDF/HTML + analysis) too.
    Returns the updated report detail so the UI can re-render.
    """
    if not confirm:
        raise HTTPException(
            status_code=400, detail="confirm=true is required to delete a reference"
        )
    backend = get_storage(request)
    root = get_root_dir(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")

    target_url = (body.url or "").strip()
    target_aid = (body.arxiv_id or "").strip()
    if not target_url and not target_aid:
        raise HTTPException(status_code=422, detail="a url or arxiv_id is required")

    citations = parse_citations(report.citations_json)
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for c in citations:
        if _citation_matches(c, target_url, target_aid):
            removed.append(c)
        else:
            kept.append(c)
    if not removed:
        raise HTTPException(status_code=404, detail="reference not found in report")

    # Compute shared-copy ownership up front: if this fails, nothing has been
    # mutated yet. The per-citation cleanup loop then never re-scans the whole
    # report table for each removed reference.
    referencing = await backend.list_reports(limit=100000)
    owner_ids = {r.artifact_id for r in referencing}
    other_cited = await _other_cited_artifact_ids(backend, run_id)

    new_citations_json = json.dumps(kept)
    new_markdown = _replace_bibliography_section(report.markdown or "", kept)
    await backend.update_report_content(
        run_id, markdown=new_markdown, citations_json=new_citations_json
    )

    for c in removed:
        artifact = await _citation_artifact(backend, c)
        if artifact is None:
            continue
        # Never delete a report's own output artifact out from under it.
        if artifact.artifact_id in owner_ids:
            continue
        # Keep shared copies still cited by other reports.
        if artifact.artifact_id in other_cited:
            continue
        remove_artifact_files(root, artifact)
        await backend.delete_artifact(artifact.artifact_id)

    return await get_report_detail(run_id, request)


@router.delete("/artifacts/{artifact_id}", response_model=DeleteArtifactResponse)
async def delete_artifact(
    artifact_id: str,
    request: Request,
    confirm: bool = Query(False),
) -> DeleteArtifactResponse:
    if not confirm:
        raise HTTPException(
            status_code=400, detail="confirm=true is required to delete an artifact"
        )
    backend = get_storage(request)
    root = get_root_dir(request)
    artifact = await backend.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    # A report's own output artifact must not be deleted out from under it —
    # steer the user to deleting the report instead.
    referencing = await backend.list_reports(limit=100000)
    owners = [r for r in referencing if r.artifact_id == artifact_id]
    if owners:
        raise HTTPException(
            status_code=409,
            detail=(
                "this artifact is the archived output of report(s) "
                + ", ".join(r.run_id[:12] for r in owners)
                + "; delete those report(s) instead"
            ),
        )
    removed = remove_artifact_files(root, artifact)
    await backend.delete_artifact(artifact_id)
    return DeleteArtifactResponse(removed_files=removed)


@router.patch("/reports/{run_id}", response_model=ReportDetail)
async def rename_report(run_id: str, body: RenameReportRequest, request: Request) -> ReportDetail:
    """Rename a research report (updates its display name/title).

    The web UI renders the report title from the markdown's first heading, so
    rename also rewrites that heading to keep the visible title in sync.
    """
    backend = get_storage(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    new_title = body.query.strip()
    await backend.rename_report(run_id, new_title)
    updated_md = _replace_report_title(report.markdown or "", new_title)
    if updated_md != (report.markdown or ""):
        await backend.update_report_content(run_id, markdown=updated_md, citations_json=None)
    return await get_report_detail(run_id, request)


def _replace_report_title(markdown: str, new_title: str) -> str:
    """Rewrite the first heading in a report's markdown to `new_title`.

    Matches the web UI's title extraction (first heading of any level), so a
    renamed report keeps its visible title consistent with `original_query`.
    """
    if not markdown:
        return markdown
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if _HEADING_RE.match(line):
            lines[i] = f"# {new_title}"
            return "\n".join(lines)
    return markdown


@router.post("/reports/{run_id}/merge", response_model=MergeReportsResponse)
async def merge_report(
    run_id: str, body: MergeReportsRequest, request: Request
) -> MergeReportsResponse:
    """Merge this report with the given others into one unified report.

    Runs inline (a single LLM call). Returns the new merged report's run_id.
    """
    backend = get_storage(request)
    cfg = get_config(request)
    root = get_root_dir(request)

    all_ids = list(dict.fromkeys([run_id] + [x.strip() for x in body.other_run_ids if x.strip()]))
    if len(all_ids) < 2:
        raise HTTPException(status_code=422, detail="merge requires at least two distinct reports")
    for rid in all_ids:
        if await backend.get_report(rid) is None:
            raise HTTPException(status_code=404, detail=f"report not found: {rid}")

    from deep_research.library.merge import merge_reports
    from deep_research.library.writer import LibraryWriter
    from deep_research.llm.router import LLMRouter

    writer = LibraryWriter(backend, str(root))
    try:
        async with LLMRouter(cfg.llm) as router:
            new_run_id = await merge_reports(
                backend,
                writer,
                all_ids,
                router,
                name=body.name,
                delete_sources=body.delete_sources,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("merge failed for %s: %s", run_id, e)
        raise HTTPException(status_code=500, detail=f"merge failed: {e}")
    new_report = await backend.get_report(new_run_id)
    return MergeReportsResponse(
        run_id=new_run_id,
        query=(new_report.original_query if new_report else "") or "",
    )


@router.get("/tags", response_model=list[TagInfo])
async def list_tags(
    request: Request,
    limit: int = Query(200, ge=1, le=1000),
) -> list[TagInfo]:
    backend = get_storage(request)
    rows = await backend.list_tags(limit=limit)
    return [TagInfo(tag=t, count=c) for t, c in rows]


@router.get("/artifacts", response_model=ArtifactListResponse)
async def list_artifacts(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, min_length=1, max_length=200),
    kind: str | None = Query(None, max_length=32),
) -> ArtifactListResponse:
    backend = get_storage(request)
    items = await backend.list_artifacts(limit, offset, q=q, kind=kind)
    total = await backend.count_artifacts(q=q, kind=kind)
    art_ids = [a.artifact_id for a in items]
    tags_map = await backend.get_tags_for_artifacts(art_ids) if art_ids else {}
    analyses_map = await backend.get_analyses_for_artifacts(art_ids) if art_ids else {}
    out: list[ArtifactListItem] = []
    for a in items:
        analyses = analyses_map.get(a.artifact_id, [])
        rel = max(
            (x.relevance_score for x in analyses if x.relevance_score is not None),
            default=None,
        )
        summary = next((x.summary for x in analyses if x.summary), "") or ""
        out.append(
            ArtifactListItem(
                artifact_id=a.artifact_id,
                kind=a.kind,
                source_url=a.source_url,
                source_type=a.source_type,
                title=a.title,
                authors=_parse_json_list(a.authors),
                arxiv_id=a.arxiv_id,
                bytes_size=a.bytes_size,
                first_seen_at=a.first_seen_at,
                tags=sorted(t.tag for t in tags_map.get(a.artifact_id, [])),
                relevance_score=rel,
                summary=summary,
            )
        )
    return ArtifactListResponse(total=total, limit=limit, offset=offset, items=out)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactDetail)
async def get_artifact_detail(artifact_id: str, request: Request) -> ArtifactDetail:
    backend = get_storage(request)
    artifact = await backend.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    analyses = await backend.get_analyses_for_artifact(artifact_id)
    edges = await backend.get_citation_edges_for_source(artifact_id)
    tag_rows = await backend.get_tags_for_artifact(artifact_id)
    return ArtifactDetail(
        artifact_id=artifact.artifact_id,
        kind=artifact.kind,
        source_url=artifact.source_url,
        source_type=artifact.source_type,
        title=artifact.title,
        image_url=(
            f"/api/artifacts/{artifact.artifact_id}/image" if artifact.kind == "image" else None
        ),
        authors=_parse_json_list(artifact.authors),
        arxiv_id=artifact.arxiv_id,
        bytes_path=artifact.bytes_path,
        bytes_size=artifact.bytes_size,
        first_seen_at=artifact.first_seen_at,
        last_touched_at=artifact.last_touched_at,
        tags=sorted(t.tag for t in tag_rows),
        analyses=[_analysis_info(a) for a in analyses],
        citation_edges=[_edge_info(e) for e in edges],
    )


@router.get("/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=100),
) -> SearchResponse:
    backend = get_storage(request)
    reports = await backend.search_reports(q, limit=limit, offset=0)
    items = await _report_items(backend, get_root_dir(request), reports)
    hits = await backend.full_text_search(q, kind="any", limit=limit)
    return SearchResponse(
        q=q,
        reports=items,
        artifacts=[
            SearchHitItem(
                artifact_id=h.artifact_id,
                title=h.title,
                authors=h.authors,
                summary=h.summary,
                score=h.score,
            )
            for h in hits
        ],
    )


@router.get("/stats", response_model=StatsResponse)
async def stats(request: Request) -> StatsResponse:
    backend = get_storage(request)
    return StatsResponse(
        reports=await backend.count_reports(),
        artifacts=await backend.count_artifacts(),
        tags=await backend.count_tags(),
    )
