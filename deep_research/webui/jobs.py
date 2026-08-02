"""In-process background research job manager.

Each job runs `run_research` as an asyncio task and broadcasts phase/step
events to SSE subscribers. Jobs live only in memory: a server restart drops
in-flight jobs (acceptable for a single-user local app — documented in the
P12.5 section of docs/PLAN.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

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


@dataclass
class ResearchJob:
    job_id: str
    query: str
    path_override: str | None = None
    status: str = "running"  # running | done | failed | cancelled
    phase: str = ""
    step: str = ""
    detail: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
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


class ResearchJobManager:
    """Owns research jobs and their SSE subscriber queues."""

    def __init__(
        self,
        config_path: str,
        *,
        runner: ResearchRunner | None = None,
        max_concurrent: int = 2,
    ) -> None:
        self._config_path = config_path
        self._runner = runner or _default_runner
        self._max_concurrent = max_concurrent
        self._jobs: dict[str, ResearchJob] = {}
        self._running = 0

    def start(self, query: str, path_override: str | None = None) -> ResearchJob | None:
        """Start a research job, or return None when at the concurrency cap."""
        if self._running >= self._max_concurrent:
            return None
        job = ResearchJob(
            job_id=uuid.uuid4().hex[:16],
            query=query,
            path_override=path_override,
        )
        self._jobs[job.job_id] = job
        self._running += 1
        job.task = asyncio.create_task(self._run(job))
        self._prune()
        return job

    def get(self, job_id: str) -> ResearchJob | None:
        return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != "running" or job.task is None:
            return False
        # Surface the transition immediately: the CancelledError handler only
        # runs later (on the next await point), so without this a cancel
        # returns while the job still reads "running".
        job.status = "cancelling"
        self.emit(job, {"type": "cancelling"})
        job.task.cancel()
        return True

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
        run_id = uuid.uuid4().hex[:16]
        try:
            config = AgentTopConfig.load_yaml(self._config_path)
            report = await self._runner(config, job.query, job.path_override, reporter, run_id)
            if job.status != "running":
                # Cancelled while the runner was finishing (or already marked
                # cancelling). The CancelledError handler won't run on this
                # path, so emit the terminal event here.
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
            job.status = "cancelled"
            self.emit(job, {"type": "cancelled"})
        except Exception as exc:
            logger.exception("research job %s failed", job.job_id)
            job.status = "failed"
            job.error = str(exc)
            self.emit(job, {"type": "error", "error": str(exc)})
        finally:
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
