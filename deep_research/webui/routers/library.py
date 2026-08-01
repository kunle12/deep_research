"""Library browsing API — reports, tags, artifacts, search, stats."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from deep_research.config import AgentTopConfig
from deep_research.library.citation_archive import archive_cited_pdf
from deep_research.library.storage.base import StorageBackend
from deep_research.library.storage.rows import AnalysisRow, ArtifactRow, CitationEdgeRow, TagRow
from deep_research.library.writer import LibraryWriter
from deep_research.llm.tool_loop import ToolRegistry
from deep_research.tools import arxiv as arxiv_tool
from deep_research.util import strip_arxiv_version
from deep_research.webui.deps import get_config, get_root_dir, get_storage
from deep_research.webui.format import citation_count, make_snippet, parse_citations
from deep_research.webui.models import (
    AnalysisInfo,
    ArtifactDetail,
    CitationEdgeInfo,
    DeleteReportResponse,
    ReportDetail,
    ReportListItem,
    ReportListResponse,
    SearchHitItem,
    SearchResponse,
    StatsResponse,
    TagInfo,
    TagUpdateResponse,
)

router = APIRouter(prefix="/api", tags=["library"])


class TagBody(BaseModel):
    tag: str = Field(min_length=1, max_length=64)


class ArxivPdfBody(BaseModel):
    arxiv_id: str = Field(min_length=3, max_length=64)


class ArxivPdfResponse(BaseModel):
    local_pdf_url: str | None = None
    archived: bool = False
    error: str | None = None


async def _report_items(backend: StorageBackend, reports) -> list[ReportListItem]:
    """Enrich report rows with tags, snippets, and PDF availability."""
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
                path=r.path_taken,
                iterations=r.iterations,
                tags=sorted(t.tag for t in tags_map.get(r.artifact_id, [])),
                snippet=make_snippet(r.markdown),
                citation_count=citation_count(r.citations_json),
                markdown_length=len(r.markdown),
                has_pdf=bool(art and art.kind == "pdf" and art.bytes_path),
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


def _strip_arxiv_version(arxiv_id: str) -> str:
    """Normalize 2401.12345v2 -> 2401.12345 for artifact lookups."""
    return re.sub(r"v\d+$", "", arxiv_id)


async def _citation_local_pdf_url(
    backend: StorageBackend,
    root: Path,
    citation: dict[str, Any],
) -> str | None:
    """Return a URL for the locally archived PDF of this citation, if any.

    Papers archived by the academic path are stored as `kind="pdf"` artifacts
    keyed by arxiv_id (and source_url), so references can open the library's
    own copy instead of the upstream page.
    """
    aid = citation.get("arxiv_id")
    artifact: ArtifactRow | None = None
    if isinstance(aid, str) and aid and not aid.startswith("scholar:"):
        artifact = await backend.find_artifact_by_arxiv_id(_strip_arxiv_version(aid))
    if artifact is None:
        url = citation.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            artifact = await backend.find_artifact_by_url(url)
    if artifact is None or artifact.kind != "pdf" or not artifact.bytes_path:
        return None
    root_resolved = root.resolve()
    file_path = (root_resolved / artifact.bytes_path).resolve()
    if not file_path.is_relative_to(root_resolved) or not file_path.is_file():
        return None
    return f"/api/artifacts/{artifact.artifact_id}/pdf"


async def _enrich_citations(
    backend: StorageBackend,
    root: Path,
    citations_json: str | None,
) -> list[dict[str, Any]]:
    """Attach `local_pdf_url` to citations that have an archived PDF copy."""
    citations = parse_citations(citations_json)
    for c in citations:
        local_pdf = await _citation_local_pdf_url(backend, root, c)
        if local_pdf:
            c["local_pdf_url"] = local_pdf
    return citations


async def _require_report_artifact(backend: StorageBackend, run_id: str) -> str:
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    if not report.artifact_id:
        raise HTTPException(status_code=400, detail="report has no artifact; cannot manage tags")
    return report.artifact_id


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
    items = await _report_items(backend, reports)
    return ReportListResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/reports/{run_id}", response_model=ReportDetail)
async def get_report_detail(run_id: str, request: Request) -> ReportDetail:
    backend = get_storage(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    tags: list[str] = []
    artifact: ArtifactRow | None = None
    if report.artifact_id:
        artifact = await backend.get_artifact(report.artifact_id)
        tag_rows = await backend.get_tags_for_artifact(report.artifact_id)
        tags = sorted(t.tag for t in tag_rows)
    has_pdf = bool(artifact and artifact.kind == "pdf" and artifact.bytes_path)
    return ReportDetail(
        run_id=report.run_id,
        started_at=report.started_at,
        completed_at=report.completed_at,
        query=report.original_query,
        path=report.path_taken,
        classifier_rationale=report.classifier_rationale,
        iterations=report.iterations,
        markdown=report.markdown,
        citations=await _enrich_citations(backend, get_root_dir(request), report.citations_json),
        tags=tags,
        artifact_id=report.artifact_id,
        has_pdf=has_pdf,
        pdf_url=f"/api/reports/{run_id}/pdf" if has_pdf else None,
        markdown_url=f"/api/reports/{run_id}/markdown",
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
        headers={"Content-Disposition": f'inline; filename="{run_id}.md"'},
    )


@router.get("/reports/{run_id}/pdf")
async def get_report_pdf(run_id: str, request: Request) -> FileResponse:
    backend = get_storage(request)
    root = get_root_dir(request)
    report = await backend.get_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    artifact = await backend.get_artifact(report.artifact_id) if report.artifact_id else None
    if artifact is None or artifact.kind != "pdf" or not artifact.bytes_path:
        raise HTTPException(status_code=404, detail="no archived PDF for this report")
    root_resolved = root.resolve()
    file_path = (root_resolved / artifact.bytes_path).resolve()
    if not file_path.is_relative_to(root_resolved) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="archived PDF file missing")
    return FileResponse(file_path, media_type="application/pdf", filename=f"{run_id}.pdf")


@router.get("/artifacts/{artifact_id}/pdf")
async def get_artifact_pdf(artifact_id: str, request: Request) -> FileResponse:
    """Serve an archived PDF artifact (e.g. a paper PDF stored by the academic path)."""
    backend = get_storage(request)
    root = get_root_dir(request)
    artifact = await backend.get_artifact(artifact_id)
    if artifact is None or artifact.kind != "pdf" or not artifact.bytes_path:
        raise HTTPException(status_code=404, detail="no archived PDF for this artifact")
    root_resolved = root.resolve()
    file_path = (root_resolved / artifact.bytes_path).resolve()
    if not file_path.is_relative_to(root_resolved) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="archived PDF file missing")
    return FileResponse(
        file_path, media_type="application/pdf", filename=f"{artifact.artifact_id}.pdf"
    )


@router.post("/arxiv/pdf", response_model=ArxivPdfResponse)
async def archive_cited_arxiv_pdf(body: ArxivPdfBody, request: Request) -> ArxivPdfResponse:
    """On-demand: download + archive one arXiv paper PDF from a reference card."""
    backend = get_storage(request)
    root = get_root_dir(request)
    cfg: AgentTopConfig = get_config(request)
    aid = body.arxiv_id.strip()
    base = strip_arxiv_version(aid)
    if not base or base.startswith("scholar:"):
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
    await backend.delete_report(run_id)
    return DeleteReportResponse(removed_files=removed)


@router.get("/tags", response_model=list[TagInfo])
async def list_tags(
    request: Request,
    limit: int = Query(200, ge=1, le=1000),
) -> list[TagInfo]:
    backend = get_storage(request)
    rows = await backend.list_tags(limit=limit)
    return [TagInfo(tag=t, count=c) for t, c in rows]


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
    items = await _report_items(backend, reports)
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
        tags=len(await backend.list_tags(limit=100000)),
    )
