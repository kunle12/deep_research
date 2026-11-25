"""View-only report sharing API (docs/MULTI_USER_PLAN.md §14).

Two access paths over one authorization core:

- **secret link** — ``/api/share/{token}[/asset]`` (no auth; capability URL);
- **named grant** — ``/api/shared/{share_id}[/asset]`` (session + grant row).

Owner management lives under ``/api/shares*``. Every shared read opens the
OWNER's library through the app's ``LibraryRegistry`` — the requesting user
can never write into it, and their own library is untouched.
"""

from __future__ import annotations

import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from deep_research.accounts.service import AuthError, normalize_email
from deep_research.accounts.shares import ShareService, hash_share_token
from deep_research.accounts.store import AccountStore
from deep_research.citations import render_bibliography_markdown
from deep_research.webui.deps import get_user
from deep_research.webui.format import parse_citations
from deep_research.webui.models import ReportDetail
from deep_research.webui.routers.library import (
    _artifact_has_pdf,
    _citations_objects,
    _enrich_citations,
    _render_citations_bib,
    _render_glossary_markdown,
    _report_glossary,
    _safe_filename,
    _serve_artifact_pdf,
)

router = APIRouter(prefix="/api", tags=["sharing"])

ShareAsset = Literal["pdf", "markdown", "bibliography", "bibliography.bib", "glossary.md"]


class ShareCreateRequest(BaseModel):
    run_id: str = Field(min_length=4, max_length=64)
    with_emails: list[str] = Field(default_factory=list, max_length=25)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class ShareGrantsRequest(BaseModel):
    emails: list[str] = Field(min_length=1, max_length=25)


def _sharing(request: Request) -> tuple[AccountStore, ShareService]:
    """(store, service) for sharing; 400 when multi_user mode is off."""
    store: AccountStore | None = getattr(request.app.state, "accounts_store", None)
    if store is None:
        raise HTTPException(
            status_code=400,
            detail="sharing requires multi_user mode (pdl.auth.mode)",
        )
    return store, ShareService(store)


def _registry(request: Request):
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="library registry not initialized")
    return registry


def _map_auth_error(exc: AuthError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


async def _shared_report_detail(request: Request, share) -> ReportDetail:
    """Build the same shape as GET /api/reports/{run_id} from the OWNER's library."""
    registry = _registry(request)
    backend = await registry.backend_for(share.owner_user_id)
    root = registry.root_for(share.owner_user_id)
    report = await backend.get_report(share.run_id)
    if report is None:
        # The report was deleted after the share was created.
        raise HTTPException(status_code=404, detail="shared report no longer exists")
    tags: list[str] = []
    artifact = None
    if report.artifact_id:
        artifact = await backend.get_artifact(report.artifact_id)
        tag_rows = await backend.get_tags_for_artifact(report.artifact_id)
        tags = sorted(t.tag for t in tag_rows)
    has_pdf = _artifact_has_pdf(root, artifact)
    citations = await _enrich_citations(backend, root, report.citations_json)
    # `_enrich_citations` builds viewer-library URLs (`/api/artifacts/…`) for
    # locally archived citation PDFs — those 404/401 for a share recipient
    # (F6). Strip them: recipients keep the upstream link, and the shared
    # report's own PDF stays reachable via the share-scoped `pdf_url`.
    for c in citations:
        for key in ("local_pdf_url", "local_image_url", "local_artifact_id"):
            c.pop(key, None)
    return ReportDetail(
        run_id=report.run_id,
        started_at=report.started_at,
        completed_at=report.completed_at,
        query=report.original_query,
        path=report.path_taken,
        classifier_rationale=report.classifier_rationale,
        iterations=report.iterations,
        markdown=report.markdown,
        citations=citations,
        tags=tags,
        artifact_id=report.artifact_id,
        has_pdf=has_pdf,
        pdf_url=None,  # set by the caller (path differs per access route)
        markdown_url="",  # ditto
        glossary=await _report_glossary(backend, share.run_id),
        glossary_url="",
        bibliography_url="",
        bibliography_bib_url="",
    )


async def _shared_asset(request: Request, share, asset: ShareAsset) -> Response:
    """Serve a downloadable asset of a shared report from the OWNER's library."""
    registry = _registry(request)
    backend = await registry.backend_for(share.owner_user_id)
    root = registry.root_for(share.owner_user_id)
    report = await backend.get_report(share.run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="shared report no longer exists")
    fname = _safe_filename(share.run_id)
    if asset == "pdf":
        artifact = await backend.get_artifact(report.artifact_id) if report.artifact_id else None
        if artifact is None or artifact.kind != "pdf":
            raise HTTPException(status_code=404, detail="no archived PDF for this report")
        return _serve_artifact_pdf(root, artifact, f"{fname}.pdf")
    if asset == "markdown":
        return PlainTextResponse(
            report.markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'inline; filename="{fname}.md"'},
        )
    if asset == "bibliography":
        bib_md = render_bibliography_markdown(
            _citations_objects(parse_citations(report.citations_json))
        )
        if not bib_md:
            bib_md = "## Bibliography\n\n_No cited sources recorded for this report._\n"
        return PlainTextResponse(
            bib_md,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}-bibliography.md"'},
        )
    if asset == "bibliography.bib":
        bib = _render_citations_bib(_citations_objects(parse_citations(report.citations_json)))
        if not bib.strip():
            bib = "% No cited sources recorded for this report.\n"
        return PlainTextResponse(
            bib,
            media_type="application/x-bibtex; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}.bib"'},
        )
    # asset == "glossary.md"
    md = _render_glossary_markdown(await _report_glossary(backend, share.run_id))
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}-glossary.md"'},
    )


# ---------- Owner management ----------


class ShareGrantInfo(BaseModel):
    user_id: str
    email: str


class ShareInfo(BaseModel):
    share_id: str
    run_id: str
    title: str | None = None
    created_at: str
    expires_at: str | None = None
    revoked_at: str | None = None
    grants: list[ShareGrantInfo] = Field(default_factory=list)  # id + email (F5)


@router.post("/shares", status_code=201)
async def create_share(body: ShareCreateRequest, request: Request):
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    store, service = _sharing(request)
    # The report must exist in the CALLER's own library.
    registry = _registry(request)
    backend = await registry.backend_for(user.user_id)
    report = await backend.get_report(body.run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")

    try:
        # Inside the try: normalize_email raises AuthError(422) on a bad
        # address — it must map to HTTP, not surface as a 500 (F3).
        emails = [normalize_email(e) for e in body.with_emails]
        created = await service.create_share(
            user.user_id,
            body.run_id,
            title=report.original_query[:200],
            with_emails=emails,
            expires_in_days=body.expires_in_days,
        )
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    grants = [
        ShareGrantInfo(user_id=g.user_id, email=g.email)
        for g in await store.list_grants(created.share.share_id)
    ]
    return {
        "share_id": created.share.share_id,
        "run_id": created.share.run_id,
        "title": created.share.title,
        "expires_at": created.share.expires_at,
        "token": created.token,  # shown ONCE
        "url": f"/#/share/{created.token}",
        "grants": grants,
    }


@router.get("/shares", response_model=list[ShareInfo])
async def list_my_shares(request: Request) -> list[ShareInfo]:
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    store, _service = _sharing(request)
    shares = await store.list_shares_for_owner(user.user_id)
    out: list[ShareInfo] = []
    for s in shares:
        grants = [
            ShareGrantInfo(user_id=g.user_id, email=g.email)
            for g in await store.list_grants(s.share_id)
        ]
        out.append(
            ShareInfo(
                share_id=s.share_id,
                run_id=s.run_id,
                title=s.title,
                created_at=s.created_at,
                expires_at=s.expires_at,
                revoked_at=s.revoked_at,
                grants=grants,
            )
        )
    return out


@router.delete("/shares/{share_id}", status_code=204)
async def revoke_share(share_id: str, request: Request) -> None:
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    store, service = _sharing(request)
    try:
        await service.revoke(share_id, user.user_id)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc


@router.post("/shares/{share_id}/rotate")
async def rotate_share_token(share_id: str, request: Request):
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    store, service = _sharing(request)
    try:
        share = await service.resolve_owner_share(share_id, user.user_id)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    new_token = secrets.token_urlsafe(32)
    # Check the rowcount: a concurrent revoke between resolve and update means
    # the share is gone — 404 instead of minting a link to nothing (F10).
    if not await store.rotate_share_token(share.share_id, hash_share_token(new_token)):
        raise HTTPException(status_code=404, detail="share not found")
    return {"share_id": share.share_id, "token": new_token, "url": f"/#/share/{new_token}"}


@router.post("/shares/{share_id}/grants", response_model=ShareInfo)
async def add_share_grants(share_id: str, body: ShareGrantsRequest, request: Request):
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    store, service = _sharing(request)
    try:
        share = await service.resolve_owner_share(share_id, user.user_id)
        await service.add_grantees(share.share_id, user.user_id, body.emails)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    grants = [
        ShareGrantInfo(user_id=g.user_id, email=g.email)
        for g in await store.list_grants(share.share_id)
    ]
    return ShareInfo(
        share_id=share.share_id,
        run_id=share.run_id,
        title=share.title,
        created_at=share.created_at,
        expires_at=share.expires_at,
        revoked_at=share.revoked_at,
        grants=grants,
    )


@router.delete("/shares/{share_id}/grants/{grantee_user_id}", status_code=204)
async def remove_share_grant(share_id: str, grantee_user_id: str, request: Request) -> None:
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    store, service = _sharing(request)
    try:
        await service.resolve_owner_share(share_id, user.user_id)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    await store.remove_grant(share_id, grantee_user_id)


# ---------- Recipient: named-account path ----------


class SharedItem(BaseModel):
    share_id: str
    run_id: str
    title: str | None = None
    owner_email: str
    created_at: str


@router.get("/shared", response_model=list[SharedItem])
async def list_shared_with_me(request: Request) -> list[SharedItem]:
    user = get_user(request)
    if user is None:
        return []  # unauthenticated: nothing shared (no 401 — harmless probe)
    store = getattr(request.app.state, "accounts_store", None)
    if store is None:
        return []
    items = await store.list_shares_for_grantee(user.user_id)
    return [
        SharedItem(
            share_id=s.share_id,
            run_id=s.run_id,
            title=s.title,
            owner_email=owner_email,
            created_at=s.created_at,
        )
        for s, owner_email in items
    ]


@router.get("/shared/{share_id}")
async def read_shared_report(share_id: str, request: Request) -> ReportDetail:
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    store = getattr(request.app.state, "accounts_store", None)
    if store is None:
        raise HTTPException(status_code=400, detail="sharing requires multi_user mode")
    service = ShareService(store)
    try:
        share = await service.resolve_for_user(share_id, user.user_id)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    detail = await _shared_report_detail(request, share)
    detail.pdf_url = f"/api/shared/{share.share_id}/pdf" if detail.has_pdf else None
    detail.markdown_url = f"/api/shared/{share.share_id}/markdown"
    detail.glossary_url = f"/api/shared/{share.share_id}/glossary.md"
    detail.bibliography_url = f"/api/shared/{share.share_id}/bibliography"
    detail.bibliography_bib_url = f"/api/shared/{share.share_id}/bibliography.bib"
    return detail


@router.get("/shared/{share_id}/{asset}")
async def read_shared_asset(share_id: str, asset: ShareAsset, request: Request) -> Response:
    user = get_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    store = getattr(request.app.state, "accounts_store", None)
    if store is None:
        raise HTTPException(status_code=400, detail="sharing requires multi_user mode")
    service = ShareService(store)
    try:
        share = await service.resolve_for_user(share_id, user.user_id)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    return await _shared_asset(request, share, asset)


# ---------- Recipient: secret-link path ----------


@router.get("/share/{token}")
async def read_share_by_token(token: str, request: Request) -> ReportDetail:
    store = getattr(request.app.state, "accounts_store", None)
    if store is None:
        raise HTTPException(status_code=404, detail="share not found")
    service = ShareService(store)
    try:
        share = await service.resolve_for_token(token)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    detail = await _shared_report_detail(request, share)
    detail.pdf_url = f"/api/share/{token}/pdf" if detail.has_pdf else None
    detail.markdown_url = f"/api/share/{token}/markdown"
    detail.glossary_url = f"/api/share/{token}/glossary.md"
    detail.bibliography_url = f"/api/share/{token}/bibliography"
    detail.bibliography_bib_url = f"/api/share/{token}/bibliography.bib"
    return detail


@router.get("/share/{token}/{asset}")
async def read_share_asset_by_token(token: str, asset: ShareAsset, request: Request) -> Response:
    store = getattr(request.app.state, "accounts_store", None)
    if store is None:
        raise HTTPException(status_code=404, detail="share not found")
    service = ShareService(store)
    try:
        share = await service.resolve_for_token(token)
    except AuthError as exc:
        raise _map_auth_error(exc) from exc
    return await _shared_asset(request, share, asset)
