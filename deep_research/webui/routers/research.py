"""Research job API — start jobs, poll status, stream SSE, cancel."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from deep_research.webui.jobs import _TERMINAL_EVENTS, ResearchJob

router = APIRouter(prefix="/api/research", tags=["research"])


def _reject_private_url(url: str) -> bool:
    """True when *url* targets a loopback / private / link-local host.

    The server fetches (attach mode) or POSTs (webhook) to these URLs on the
    client's behalf, so pointing them at internal hosts is an SSRF vector.
    Literal IPs and localhost are rejected; hostnames that resolve to private
    ranges aren't checked here (would need a DNS lookup).
    """
    host = urlparse(url).hostname
    if not host:
        return False
    if host.lower() in ("localhost", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


class ResearchStartRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    path_override: Literal["quick", "deep", "academic", "url_source"] | None = None
    attach_to_run_id: str | None = Field(default=None, max_length=64)
    webhook_url: str | None = Field(default=None, max_length=2048)


class ResearchStartResponse(BaseModel):
    job_id: str
    status: str


class ResearchJobStatus(BaseModel):
    job_id: str
    query: str = ""
    status: str
    phase: str = ""
    step: str = ""
    detail: str = ""
    started_at: float = 0.0
    completed_at: float | None = None
    paused_at: float | None = None
    run_id: str | None = None
    archived: bool = False
    error: str | None = None
    attach_to: str | None = None


def _status(job: ResearchJob) -> ResearchJobStatus:
    return ResearchJobStatus(
        job_id=job.job_id,
        query=job.query,
        status=job.status,
        phase=job.phase,
        step=job.step,
        detail=job.detail,
        started_at=job.started_at,
        completed_at=job.completed_at,
        paused_at=job.paused_at,
        run_id=job.run_id,
        archived=job.archived,
        error=job.error,
        attach_to=job.attach_to,
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _jobs(request: Request):
    jobs = getattr(request.app.state, "jobs", None)
    if jobs is None:
        raise HTTPException(status_code=503, detail="research jobs not initialized")
    return jobs


@router.post("", response_model=ResearchStartResponse, status_code=202)
async def start_research(body: ResearchStartRequest, request: Request):
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="query must not be blank")

    # Webhook must be an http(s) URL to a public host (SSRF guard).
    if body.webhook_url:
        if not body.webhook_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="webhook_url must be an http(s) URL")
        if _reject_private_url(body.webhook_url):
            raise HTTPException(
                status_code=422, detail="webhook_url must not target a private/loopback host"
            )

    # Attach mode: query must be a URL and the target report must exist.
    if body.attach_to_run_id:
        if not query.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="attach query must be an http(s) URL")
        if _reject_private_url(query):
            raise HTTPException(
                status_code=422, detail="attach query must not target a private/loopback host"
            )
        from deep_research.webui.deps import get_storage

        backend = get_storage(request)
        target = await backend.get_report(body.attach_to_run_id)
        if target is None:
            raise HTTPException(status_code=404, detail="target report not found")

    job = _jobs(request).start(
        query,
        body.path_override,
        attach_to=body.attach_to_run_id,
        webhook_url=body.webhook_url,
    )
    if job is None:
        raise HTTPException(
            status_code=409,
            detail="a research job is already running; wait for it to finish and retry",
        )
    return ResearchStartResponse(job_id=job.job_id, status=job.status)


@router.get("/jobs", response_model=list[ResearchJobStatus])
async def list_jobs(request: Request) -> list[ResearchJobStatus]:
    return [_status(job) for job in _jobs(request).list_jobs()]


@router.get("/jobs/{job_id}", response_model=ResearchJobStatus)
async def get_job_status(job_id: str, request: Request) -> ResearchJobStatus:
    job = _jobs(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _status(job)


@router.post("/jobs/{job_id}/cancel", response_model=ResearchJobStatus)
async def cancel_job(job_id: str, request: Request) -> ResearchJobStatus:
    manager = _jobs(request)
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    await manager.cancel(job_id)
    return _status(job)


@router.post("/jobs/{job_id}/pause", response_model=ResearchJobStatus)
async def pause_job(job_id: str, request: Request) -> ResearchJobStatus:
    manager = _jobs(request)
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not await manager.pause(job_id):
        raise HTTPException(status_code=409, detail="job is not running")
    return _status(job)


@router.post("/jobs/{job_id}/resume", response_model=ResearchJobStatus)
async def resume_job(job_id: str, request: Request) -> ResearchJobStatus:
    manager = _jobs(request)
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not manager.resume(job_id):
        raise HTTPException(status_code=409, detail="job is not paused (or another job is running)")
    return _status(job)


@router.post("/jobs/{job_id}/abandon", response_model=ResearchJobStatus)
async def abandon_job(job_id: str, request: Request) -> ResearchJobStatus:
    manager = _jobs(request)
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not manager.abandon(job_id):
        raise HTTPException(status_code=409, detail="job is running; cancel it first")
    return _status(job)


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str, request: Request) -> StreamingResponse:
    manager = _jobs(request)
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    queue = manager.subscribe(job_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def event_source():
        try:
            # Snapshot first so late subscribers see the current state even
            # when no new events are flowing.
            yield _sse(
                {
                    "type": "status",
                    "status": job.status,
                    "phase": job.phase,
                    "step": job.step,
                    "detail": job.detail,
                    "run_id": job.run_id,
                    "archived": job.archived,
                    "error": job.error,
                }
            )
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
                # A terminal event (done/error/cancelled) or a pause closes the
                # stream; a paused job emits only status events, so without this
                # the connection would stay open with keepalives indefinitely.
                if event.get("type") in _TERMINAL_EVENTS or event.get("status") == "paused":
                    break
        finally:
            manager.unsubscribe(job_id, queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
