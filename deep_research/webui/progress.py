"""ProgressReporter bridge — forwards agent phase/step events into a job."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deep_research.progress import ProgressReporter

if TYPE_CHECKING:
    from deep_research.webui.jobs import ResearchJob, ResearchJobManager


class JobProgressReporter(ProgressReporter):
    """Routes `phase` / `step` calls from the agent into a research job's
    event stream (and status fields)."""

    def __init__(self, manager: ResearchJobManager, job: ResearchJob) -> None:
        self._manager = manager
        self._job = job

    def phase(self, name: str, detail: str = "") -> None:
        self._job.phase = name
        self._job.detail = detail or ""
        self._manager.emit(self._job, {"type": "phase", "phase": name, "detail": detail})

    def step(self, label: str, detail: str = "") -> None:
        self._job.step = label
        self._job.detail = detail or ""
        self._manager.emit(self._job, {"type": "step", "step": label, "detail": detail})

    def complete(self) -> None:
        # The job wrapper emits the terminal event; nothing to do here.
        return None
