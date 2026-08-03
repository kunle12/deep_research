"""Tests for the web UI research job API (P12.5 / Phase 4)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from deep_research.state import Report
from deep_research.webui import create_app
from deep_research.webui.jobs import ResearchJob, ResearchJobManager


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
def client(runner: FakeRunner, tmp_path):
    app = create_app(
        "config.yaml",
        research_runner=runner,
        checkpoint_dir=tmp_path / "checkpoints",
    )
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
    # run_id is assigned at task start (needed for pause/resume checkpointing)
    assert status["run_id"] is not None


def test_cancel_job(client, runner):
    runner.hold = asyncio.Event()
    job_id = client.post("/api/research", json={"query": "long running"}).json()["job_id"]
    time.sleep(0.1)  # let the task start and reach the hold

    r = client.post(f"/api/research/jobs/{job_id}/cancel")
    assert r.status_code == 200
    status = _wait_status(client, job_id, {"cancelled"})
    assert status["status"] == "cancelled"
    # run_id is assigned at start; cancel discards the checkpoint but the job
    # record keeps the run_id for reference.
    assert status["run_id"] is not None


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


def test_list_jobs_empty(client):
    r = client.get("/api/research/jobs")
    assert r.status_code == 200
    assert r.json() == []


def test_list_jobs_returns_query_and_status(client):
    job_id = client.post("/api/research", json={"query": "list me"}).json()["job_id"]
    _wait_status(client, job_id, {"done"})

    r = client.get("/api/research/jobs")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["job_id"] == job_id
    assert items[0]["query"] == "list me"
    assert items[0]["status"] == "done"


def test_list_jobs_most_recent_first(client, runner):
    runner.delay = 0.01
    first = client.post("/api/research", json={"query": "one"}).json()["job_id"]
    _wait_status(client, first, {"done"})
    second = client.post("/api/research", json={"query": "two"}).json()["job_id"]
    _wait_status(client, second, {"done"})

    items = client.get("/api/research/jobs").json()
    assert [j["query"] for j in items] == ["two", "one"]


def test_default_concurrency_is_single(client, runner):
    runner.hold = asyncio.Event()
    first = client.post("/api/research", json={"query": "only one"})
    assert first.status_code == 202
    time.sleep(0.1)
    second = client.post("/api/research", json={"query": "second should be rejected"})
    assert second.status_code == 409
    client.post(f"/api/research/jobs/{first.json()['job_id']}/cancel")


def test_concurrency_cap(runner, tmp_path):
    runner.hold = asyncio.Event()
    app = create_app(
        "config.yaml",
        research_runner=runner,
        checkpoint_dir=tmp_path / "ck",
    )
    app.state.jobs = ResearchJobManager(
        "config.yaml",
        runner=runner,
        max_concurrent=1,
        checkpoint_dir=tmp_path / "ck",
    )
    with TestClient(app) as client:
        first = client.post("/api/research", json={"query": "first"}).json()
        time.sleep(0.1)
        second = client.post("/api/research", json={"query": "second"})
        assert second.status_code == 409
        assert client.post(f"/api/research/jobs/{first['job_id']}/cancel").status_code == 200


async def test_job_config_error_releases_concurrency_slot(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("pdl: [unclosed\n", encoding="utf-8")
    manager = ResearchJobManager(str(bad), runner=FakeRunner())

    job = manager.start("hello")
    assert job is not None
    for _ in range(50):
        if job.status != "running":
            break
        await asyncio.sleep(0.02)
    assert job.status == "failed"
    assert job.error is not None

    # The failed job must not hold the concurrency slot forever: a new job
    # must be able to start (it also fails here — same bad config — but the
    # slot was released, which is the regression being guarded).
    job2 = manager.start("again")
    assert job2 is not None
    for _ in range(50):
        if job2.status != "running":
            break
        await asyncio.sleep(0.02)
    assert job2.status != "running"
    assert job2.completed_at is not None


# ---------------------------------------------------------------------------
# Pause / resume / restart-survival
# ---------------------------------------------------------------------------


def _write_checkpoint(checkpoint_dir, run_id: str, query: str) -> None:
    import json
    from pathlib import Path

    d = Path(checkpoint_dir)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "state": {"query": query, "iteration": 3},
    }
    (d / f"{run_id}.json").write_text(json.dumps(payload))


def test_pause_job(client, runner):
    runner.hold = asyncio.Event()
    job_id = client.post("/api/research", json={"query": "pause me"}).json()["job_id"]
    time.sleep(0.1)  # let the task reach the hold

    r = client.post(f"/api/research/jobs/{job_id}/pause")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert body["run_id"] is not None
    assert body["paused_at"] is not None

    # A paused job must not hold the concurrency slot: a new job can start.
    second = client.post("/api/research", json={"query": "second while paused"})
    assert second.status_code == 202


def test_resume_job_completes(client, runner):
    runner.hold = asyncio.Event()
    job_id = client.post("/api/research", json={"query": "resume me"}).json()["job_id"]
    time.sleep(0.1)
    paused = client.post(f"/api/research/jobs/{job_id}/pause").json()
    assert paused["status"] == "paused"
    run_id = paused["run_id"]

    runner.hold.set()  # the resumed task's hold will now pass immediately
    r = client.post(f"/api/research/jobs/{job_id}/resume")
    assert r.status_code == 200
    assert r.json()["status"] == "running"

    status = _wait_status(client, job_id, {"done"})
    assert status["status"] == "done"
    assert status["run_id"] == run_id  # reused the same checkpoint run_id


def test_resume_rejected_while_another_running(client, runner):
    runner.hold = asyncio.Event()
    first = client.post("/api/research", json={"query": "first"}).json()["job_id"]
    time.sleep(0.1)
    client.post(f"/api/research/jobs/{first}/pause")

    runner.hold = asyncio.Event()  # re-arm so the second job blocks too
    client.post("/api/research", json={"query": "second"}).json()["job_id"]
    time.sleep(0.1)

    r = client.post(f"/api/research/jobs/{first}/resume")
    assert r.status_code == 409


def test_pause_rejected_on_non_running(client):
    job_id = client.post("/api/research", json={"query": "fast"}).json()["job_id"]
    _wait_status(client, job_id, {"done"})
    assert client.post(f"/api/research/jobs/{job_id}/pause").status_code == 409


def test_cancel_discards_checkpoint(client, runner, tmp_path):
    runner.hold = asyncio.Event()
    job_id = client.post("/api/research", json={"query": "cancel ckpt"}).json()["job_id"]
    time.sleep(0.1)
    status = client.get(f"/api/research/jobs/{job_id}").json()
    # Simulate the checkpoint the engine would have written for this run_id.
    _write_checkpoint(tmp_path / "checkpoints", status["run_id"], "cancel ckpt")
    assert (tmp_path / "checkpoints" / f"{status['run_id']}.json").exists()

    client.post(f"/api/research/jobs/{job_id}/cancel")
    _wait_status(client, job_id, {"cancelled"})
    assert not (tmp_path / "checkpoints" / f"{status['run_id']}.json").exists()


def test_abandon_removes_job_and_discards_checkpoint(client, runner, tmp_path):
    runner.hold = asyncio.Event()
    job_id = client.post("/api/research", json={"query": "abandon ckpt"}).json()["job_id"]
    time.sleep(0.1)
    paused = client.post(f"/api/research/jobs/{job_id}/pause").json()
    assert paused["status"] == "paused"
    _write_checkpoint(tmp_path / "checkpoints", paused["run_id"], "abandon ckpt")

    r = client.post(f"/api/research/jobs/{job_id}/abandon")
    assert r.status_code == 200
    assert client.get(f"/api/research/jobs/{job_id}").status_code == 404
    assert not (tmp_path / "checkpoints" / f"{paused['run_id']}.json").exists()


def test_restore_paused_scans_checkpoints(tmp_path):
    manager = ResearchJobManager(
        "config.yaml",
        runner=FakeRunner(),
        checkpoint_dir=tmp_path / "checkpoints",
    )
    _write_checkpoint(tmp_path / "checkpoints", "run_aaa", "orphan query one")
    _write_checkpoint(tmp_path / "checkpoints", "run_bbb", "orphan query two")

    restored = manager.restore_paused()
    assert len(restored) == 2
    by_run = {j.run_id: j for j in restored}
    assert by_run["run_aaa"].query == "orphan query one"
    assert by_run["run_aaa"].status == "paused"
    assert by_run["run_bbb"].query == "orphan query two"

    # Second scan skips already-tracked checkpoints.
    assert manager.restore_paused() == []


def test_restore_paused_skips_tracked_and_corrupt(tmp_path):
    manager = ResearchJobManager(
        "config.yaml",
        runner=FakeRunner(),
        checkpoint_dir=tmp_path / "checkpoints",
    )
    _write_checkpoint(tmp_path / "checkpoints", "run_aaa", "orphan query")
    # A corrupt file must be skipped without crashing.
    (tmp_path / "checkpoints" / "junk.json").write_text("not json")
    # A tracked job with the same query suppresses the orphan.
    manager._jobs["tracked"] = ResearchJob(job_id="tracked", query="orphan query", status="running")

    restored = manager.restore_paused()
    assert restored == []
