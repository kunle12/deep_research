"""In-process background research job manager.

Each job runs `run_research` as an asyncio task and broadcasts phase/step
events to SSE subscribers. Jobs live only in memory: a server restart drops
in-flight jobs (acceptable for a single-user local app — documented in the
P12.5 section of docs/PLAN.md).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deep_research.checkpoint import _CHECKPOINT_DIR
from deep_research.config import AgentTopConfig
from deep_research.progress import ProgressReporter
from deep_research.state import Report
from deep_research.webui.progress import JobProgressReporter

logger = logging.getLogger(__name__)

_MAX_JOBS = 50
_QUEUE_MAX = 500
_TERMINAL_EVENTS = {"done", "error", "cancelled"}

ResearchRunner = Callable[
    [AgentTopConfig, str, str | None, ProgressReporter, str], Awaitable[Report]
]

# Attach runner: (config, url, target_run_id, progress) -> dict. Runs the
# attach-source flow (fetch + analyze + append to an existing report).
AttachRunner = Callable[[AgentTopConfig, str, str, ProgressReporter], Awaitable[dict]]


@dataclass
class ResearchJob:
    job_id: str
    query: str
    path_override: str | None = None
    attach_to: str | None = None
    status: str = "running"  # running | done | failed | cancelled
    phase: str = ""
    step: str = ""
    detail: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    paused_at: float | None = None
    run_id: str | None = None
    archived: bool = False
    error: str | None = None
    event_log: list[dict[str, Any]] = field(default_factory=list)
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    _subscribers: set[asyncio.Queue] = field(default_factory=set, repr=False, compare=False)


def _put(queue: asyncio.Queue, event: dict[str, Any]) -> None:
    """Best-effort enqueue; drop the oldest event when the queue is full."""
    if queue.full():
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    with suppress(asyncio.QueueFull):
        queue.put_nowait(event)


async def _default_runner(
    config: AgentTopConfig,
    query: str,
    path_override: str | None,
    progress: ProgressReporter,
    run_id: str,
) -> Report:
    from deep_research import run_research

    return await run_research(
        query,
        config,
        path_override=path_override,
        progress=progress,
        run_id=run_id,
    )


async def _default_attach_runner(
    config: AgentTopConfig,
    url: str,
    run_id: str,
    progress: ProgressReporter,
) -> dict:
    """Fetch + analyze + attach *url* to the research *run_id*."""
    from deep_research.library.attach import attach_source
    from deep_research.library.storage import get_backend
    from deep_research.library.writer import LibraryWriter
    from deep_research.llm.router import LLMRouter

    backend = await get_backend(config)
    try:
        writer = LibraryWriter(backend, config.pdl.root_dir)
        async with LLMRouter(config.llm) as router:
            return await attach_source(
                url,
                run_id,
                backend,
                writer,
                config,
                router,
                progress=progress,
            )
    finally:
        await backend.close()


class ResearchJobManager:
    """Owns research jobs and their SSE subscriber queues."""

    def __init__(
        self,
        config_path: str,
        *,
        runner: ResearchRunner | None = None,
        attach_runner: AttachRunner | None = None,
        max_concurrent: int = 1,
        checkpoint_dir: Path | None = None,
    ) -> None:
        self._config_path = config_path
        self._runner = runner or _default_runner
        self._attach_runner = attach_runner or _default_attach_runner
        self._max_concurrent = max_concurrent
        self._checkpoint_dir = checkpoint_dir or _CHECKPOINT_DIR
        self._jobs: dict[str, ResearchJob] = {}
        self._running = 0

    def start(
        self,
        query: str,
        path_override: str | None = None,
        *,
        attach_to: str | None = None,
    ) -> ResearchJob | None:
        """Start a research job, or return None when at the concurrency cap."""
        if self._running >= self._max_concurrent:
            return None
        job = ResearchJob(
            job_id=uuid.uuid4().hex[:16],
            query=query,
            path_override=path_override,
            attach_to=attach_to,
        )
        if attach_to:
            # Attach jobs mutate an existing report; use its run_id so the
            # `done` event points at the updated research and archival check
            # resolves against the existing report.
            job.run_id = attach_to
        self._jobs[job.job_id] = job
        self._running += 1
        job.task = asyncio.create_task(self._run(job))
        self._prune()
        return job

    def get(self, job_id: str) -> ResearchJob | None:
        return self._jobs.get(job_id)

    def _discard_checkpoint(self, run_id: str) -> None:
        """Remove the on-disk checkpoint for *run_id* from the managed dir."""
        with suppress(Exception):
            (self._checkpoint_dir / f"{run_id}.json").unlink(missing_ok=True)

    def list_jobs(self) -> list[ResearchJob]:
        """All known jobs, most recently started first."""
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != "running" or job.task is None:
            return False
        # Surface the transition immediately: the CancelledError handler only
        # runs later (on the next await point), so without this a cancel
        # returns while the job still reads "running".
        job.status = "cancelling"
        self.emit(job, {"type": "cancelling"})
        # An explicit cancel abandons the research — drop its checkpoint so it
        # never resurfaces as a resumable "paused" job after a restart.
        if job.run_id:
            self._discard_checkpoint(job.run_id)
        job.task.cancel()
        return True

    async def pause(self, job_id: str) -> bool:
        """Stop a running job at its last checkpoint, keeping it resumable.

        The engine writes a checkpoint after each iteration, so a paused job
        resumes from the last completed iteration (per-iteration granularity).
        Attach jobs have no checkpoints and cannot be paused (resume would
        re-run and duplicate the analysis section).
        """
        job = self._jobs.get(job_id)
        if job is None or job.status != "running" or job.task is None:
            return False
        if job.attach_to:
            return False
        job.status = "paused"
        job.paused_at = time.time()
        self.emit(
            job,
            {
                "type": "status",
                "status": "paused",
                "phase": job.phase,
                "step": job.step,
                "detail": job.detail,
            },
        )
        task = job.task
        job.task = None
        task.cancel()
        # Wait for the task to actually unwind so the concurrency slot
        # (`_running`) is released before a resume is attempted.
        with suppress(asyncio.CancelledError):
            await task
        return True

    def resume(self, job_id: str) -> bool:
        """Restart a paused job from its checkpoint (reuses the run_id)."""
        job = self._jobs.get(job_id)
        if job is None or job.status != "paused":
            return False
        if not job.run_id:
            return False
        if self._running >= self._max_concurrent:
            return False
        job.status = "running"
        job.completed_at = None
        job.paused_at = None
        job.error = None
        self._running += 1
        job.task = asyncio.create_task(self._run(job))
        self.emit(
            job,
            {
                "type": "status",
                "status": "running",
                "phase": "deep.resume",
                "step": job.step,
                "detail": "resuming from checkpoint",
            },
        )
        return True

    def abandon(self, job_id: str) -> bool:
        """Permanently discard a non-running job and its checkpoint."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status in ("running", "cancelling"):
            return False
        if job.run_id:
            self._discard_checkpoint(job.run_id)
        if job.task is not None:
            job.task.cancel()
        del self._jobs[job_id]
        return True

    def restore_paused(self) -> list[ResearchJob]:
        """After a restart, register orphaned checkpoint files as paused jobs.

        Every running web job writes a checkpoint per iteration keyed by its
        run_id. A restart drops the in-memory job, but its checkpoint survives
        on disk — so we surface it as a resumable paused job here.
        """
        restored: list[ResearchJob] = []
        if not self._checkpoint_dir.exists():
            return restored
        for f in self._checkpoint_dir.iterdir():
            if not f.is_file() or not f.name.endswith(".json"):
                continue
            try:
                raw = json.loads(f.read_text())
            except Exception:
                continue
            run_id = raw.get("run_id")
            state = raw.get("state")
            if not run_id or not isinstance(state, dict):
                continue
            query = state.get("query") or ""
            # Skip checkpoints that belong to jobs we already track (e.g. a
            # running job whose checkpoint exists mid-run).
            if any(
                j.run_id == run_id or (j.query and j.query == query) for j in self._jobs.values()
            ):
                continue
            job = ResearchJob(
                job_id=uuid.uuid4().hex[:16],
                query=query,
                status="paused",
                run_id=run_id,
                started_at=f.stat().st_mtime,
            )
            self._jobs[job.job_id] = job
            restored.append(job)
        return restored

    def subscribe(self, job_id: str) -> asyncio.Queue | None:
        """Return an event queue for a job, replaying its event log first."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        job._subscribers.add(queue)
        for event in job.event_log:
            _put(queue, event)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job._subscribers.discard(queue)

    def emit(self, job: ResearchJob, event: dict[str, Any]) -> None:
        event = {"ts": time.time(), **event}
        job.event_log.append(event)
        if len(job.event_log) > _QUEUE_MAX:
            job.event_log = job.event_log[-_QUEUE_MAX:]
        for queue in list(job._subscribers):
            _put(queue, event)

    def _prune(self) -> None:
        if len(self._jobs) <= _MAX_JOBS:
            return
        # Drop oldest finished jobs first.
        finished = sorted(
            (j for j in self._jobs.values() if j.status != "running"),
            key=lambda j: j.completed_at or 0,
        )
        overflow = len(self._jobs) - _MAX_JOBS
        for job in finished[:overflow]:
            del self._jobs[job.job_id]

    async def _run(self, job: ResearchJob) -> None:
        reporter = JobProgressReporter(self, job)
        run_id = job.run_id or uuid.uuid4().hex[:16]
        # Store the run_id up front (not just on success): pause/resume and
        # cancel need it to find the on-disk checkpoint while the job lives.
        job.run_id = run_id
        try:
            config = AgentTopConfig.load_yaml(self._config_path)
            if job.attach_to:
                result = await self._attach_runner(config, job.query, job.attach_to, reporter)
                if job.status != "running":
                    # Cancelled while the attach runner was finishing.
                    job.status = "cancelled"
                    self.emit(job, {"type": "cancelled"})
                    return
                # The target report already exists in the library.
                job.archived = True
                job.status = "done"
                self.emit(
                    job,
                    {
                        "type": "done",
                        "run_id": job.attach_to,
                        "archived": True,
                        "path": "attach",
                        "query": job.query,
                        "attach_status": result.get("status", "attached"),
                    },
                )
                return
            report = await self._runner(config, job.query, job.path_override, reporter, run_id)
            if job.status != "running":
                # Cancelled while the runner was finishing (or already marked
                # cancelling/paused). The CancelledError handler won't run on
                # this path, so emit the terminal event here.
                if job.status == "paused":
                    job.paused_at = job.paused_at or time.time()
                    self.emit(
                        job,
                        {
                            "type": "status",
                            "status": "paused",
                            "phase": job.phase,
                            "step": job.step,
                            "detail": job.detail,
                        },
                    )
                else:
                    job.status = "cancelled"
                    self.emit(job, {"type": "cancelled"})
                return
            if report.path == "unclear" and report.markdown.strip().startswith("# Error"):
                raise RuntimeError(report.markdown.strip().splitlines()[0])
            job.run_id = run_id
            job.archived = await self._is_archived(config, run_id)
            job.status = "done"
            self.emit(
                job,
                {
                    "type": "done",
                    "run_id": run_id,
                    "archived": job.archived,
                    "path": report.path,
                    "query": job.query,
                },
            )
        except asyncio.CancelledError:
            if job.status == "paused":
                job.paused_at = job.paused_at or time.time()
                self.emit(
                    job,
                    {
                        "type": "status",
                        "status": "paused",
                        "phase": job.phase,
                        "step": job.step,
                        "detail": job.detail,
                    },
                )
            else:
                job.status = "cancelled"
                self.emit(job, {"type": "cancelled"})
        except Exception as exc:
            logger.exception("research job %s failed", job.job_id)
            job.status = "failed"
            job.error = str(exc)
            self.emit(job, {"type": "error", "error": str(exc)})
        finally:
            if job.status == "paused":
                job.paused_at = job.paused_at or time.time()
            else:
                job.completed_at = time.time()
            self._running -= 1

    async def _is_archived(self, config: AgentTopConfig, run_id: str) -> bool:
        """Check whether the finished report made it into the library."""
        if not config.pdl.enabled:
            return False
        try:
            from deep_research.library.storage import get_backend

            backend = await get_backend(config)
            try:
                return await backend.get_report(run_id) is not None
            finally:
                await backend.close()
        except Exception:
            logger.debug("archival check failed for %s", run_id, exc_info=True)
            return False
