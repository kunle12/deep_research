"""Tests for the web UI research job API (P12.5 / Phase 4)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from deep_research.state import Report
from deep_research.webui import create_app
from deep_research.webui.jobs import ResearchJobManager


class FakeRunner:
    """Injectable stand-in for `run_research`."""

    def __init__(
        self, *, delay: float = 0.05, fail: bool = False, hold: asyncio.Event | None = None
    ):
        self.delay = delay
        self.fail = fail
        self.hold = hold
        self.received_run_id: str | None = None
        self.progress_events: list[tuple[str, str]] = []

    async def __call__(self, cfg, query, path_override, progress, run_id):
        self.received_run_id = run_id
        progress.phase("routing", "classifying query")
        progress.step("searching", query)
        self.progress_events.append(("phase", "routing"))
        self.progress_events.append(("step", "searching"))
        if self.hold is not None:
            await self.hold.wait()
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("simulated research failure")
        return Report(
            markdown="# Fake Report\n\ncontent",
            path=path_override or "quick",
            query=query,
        )


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def client(runner: FakeRunner):
    app = create_app("config.yaml", research_runner=runner)
    with TestClient(app) as c:
        yield c


def _wait_status(client: TestClient, job_id: str, wanted: set[str], timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/research/jobs/{job_id}").json()
        if status["status"] in wanted:
            return status
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {wanted}; last={status}")


def _sse_events(resp) -> list[dict]:
    events = []
    for line in resp.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
            if events[-1]["type"] in ("done", "error", "cancelled"):
                break
    return events


def test_start_job_and_poll_status(client, runner):
    r = client.post(
        "/api/research",
        json={"query": "hello world", "path_override": "deep"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "running"

    status = _wait_status(client, body["job_id"], {"done"})
    assert status["status"] == "done"
    assert status["phase"] == "routing"
    assert status["run_id"] == runner.received_run_id
    assert status["archived"] is False
    assert status["error"] is None


def test_start_job_validation(client):
    assert client.post("/api/research", json={}).status_code == 422
    assert client.post("/api/research", json={"query": ""}).status_code == 422
    assert client.post("/api/research", json={"query": "   "}).status_code == 422
    # Unknown path override rejected by pydantic
    assert (
        client.post("/api/research", json={"query": "x", "path_override": "bogus"}).status_code
        == 422
    )


def test_job_failure(client, runner):
    runner.fail = True
    r = client.post("/api/research", json={"query": "boom please"})
    job_id = r.json()["job_id"]
    status = _wait_status(client, job_id, {"failed"})
    assert status["error"] == "simulated research failure"
    assert status["run_id"] is None


def test_cancel_job(client, runner):
    runner.hold = asyncio.Event()
    job_id = client.post("/api/research", json={"query": "long running"}).json()["job_id"]
    time.sleep(0.1)  # let the task start and reach the hold

    r = client.post(f"/api/research/jobs/{job_id}/cancel")
    assert r.status_code == 200
    status = _wait_status(client, job_id, {"cancelled"})
    assert status["status"] == "cancelled"
    assert status["run_id"] is None


def test_sse_stream_receives_events(client, runner):
    runner.delay = 0.3
    job_id = client.post("/api/research", json={"query": "stream me"}).json()["job_id"]

    with client.stream("GET", f"/api/research/jobs/{job_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(resp)

    types = [e["type"] for e in events]
    assert types[0] == "status"
    assert "phase" in types
    assert "step" in types
    assert types[-1] == "done"
    assert events[-1]["run_id"] == runner.received_run_id


def test_sse_stream_replays_completed_job(client):
    job_id = client.post("/api/research", json={"query": "already done"}).json()["job_id"]
    _wait_status(client, job_id, {"done"})

    with client.stream("GET", f"/api/research/jobs/{job_id}/stream") as resp:
        events = _sse_events(resp)
    assert events[-1]["type"] == "done"


def test_missing_job_404(client):
    assert client.get("/api/research/jobs/nope").status_code == 404
    assert client.post("/api/research/jobs/nope/cancel").status_code == 404
    assert client.get("/api/research/jobs/nope/stream").status_code == 404


def test_concurrency_cap(runner):
    runner.hold = asyncio.Event()
    app = create_app("config.yaml", research_runner=runner)
    app.state.jobs = ResearchJobManager("config.yaml", runner=runner, max_concurrent=1)
    with TestClient(app) as client:
        first = client.post("/api/research", json={"query": "first"}).json()
        time.sleep(0.1)
        second = client.post("/api/research", json={"query": "second"})
        assert second.status_code == 409
        assert client.post(f"/api/research/jobs/{first['job_id']}/cancel").status_code == 200
