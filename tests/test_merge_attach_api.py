"""Web API tests for rename, merge, and attach-source endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from deep_research.library.storage.rows import (
    ArtifactRow,
    ReportRow,
)
from deep_research.library.storage.sqlite_backend import SqliteStorageBackend
from deep_research.webui import create_app


@pytest.fixture
def library_root(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    root.mkdir()
    return root


def _now(day: int) -> str:
    return datetime(2026, 7, day, 12, 0, tzinfo=UTC).isoformat()


@pytest.fixture
async def seeded_config(library_root: Path) -> Path:
    """Seed a library with two reports (run_a + run_b)."""
    backend = SqliteStorageBackend(str(library_root / "index.db"))
    await backend.connect()
    try:
        for aid, title in (("art_a", "Report A"), ("art_b", "Report B")):
            await backend.upsert_artifact(
                ArtifactRow(
                    artifact_id=aid,
                    kind="pdf",
                    source_type="research_report",
                    title=title,
                    bytes_path=f"artifacts/pdf/{aid}.pdf",
                    bytes_size=10,
                    first_seen_at=_now(1),
                    last_touched_at=_now(1),
                )
            )
        await backend.insert_report(
            ReportRow(
                run_id="run_a",
                started_at=_now(1),
                completed_at=_now(1),
                original_query="Transformer survey",
                path_taken="deep",
                markdown="# A\n\ncontent\n\n## Bibliography\n\n- x",
                artifact_id="art_a",
            )
        )
        await backend.insert_report(
            ReportRow(
                run_id="run_b",
                started_at=_now(2),
                completed_at=_now(2),
                original_query="Attention mechanisms",
                path_taken="quick",
                markdown="# B\n\ncontent",
                artifact_id="art_b",
            )
        )
    finally:
        await backend.close()

    config = library_root / "config.yaml"
    config.write_text(
        f"pdl:\n  enabled: true\n  root_dir: {library_root}\n  storage:\n    backend: sqlite\n",
        encoding="utf-8",
    )
    return config


@pytest.fixture
def client(seeded_config: Path, tmp_path):
    app = create_app(
        config_path=str(seeded_config),
        checkpoint_dir=tmp_path / "checkpoints",
    )
    with TestClient(app) as c:
        yield c


# -- Rename --


def test_rename_report(client):
    r = client.patch("/api/reports/run_a", json={"query": "Brand new name"})
    assert r.status_code == 200
    assert r.json()["query"] == "Brand new name"

    detail = client.get("/api/reports/run_a").json()
    assert detail["query"] == "Brand new name"


def test_rename_report_validation(client):
    assert client.patch("/api/reports/nope", json={"query": "x"}).status_code == 404
    assert client.patch("/api/reports/run_a", json={"query": ""}).status_code == 422
    assert client.patch("/api/reports/run_a", json={}).status_code == 422


# -- Merge --


def _fake_llm(content: str):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    return mock_client


def test_merge_reports(monkeypatch, client):
    async def _fake_merge(backend, writer, run_ids, router, **kw):
        # Persist a minimal merged report so the endpoint can return its query.
        await backend.insert_report(
            ReportRow(
                run_id="run_merged",
                started_at=_now(3),
                completed_at=_now(3),
                original_query=kw.get("name") or "Merged Topic",
                path_taken="merged",
                markdown="# Merged",
            )
        )
        return "run_merged"

    monkeypatch.setattr("deep_research.library.merge.merge_reports", _fake_merge)
    r = client.post(
        "/api/reports/run_a/merge",
        json={"other_run_ids": ["run_b"], "name": "Merged Topic"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "run_merged"
    assert body["query"] == "Merged Topic"


def test_merge_validation(client):
    assert client.post("/api/reports/run_a/merge", json={"other_run_ids": []}).status_code == 422
    assert (
        client.post("/api/reports/nope/merge", json={"other_run_ids": ["run_b"]}).status_code == 404
    )
    assert (
        client.post("/api/reports/run_a/merge", json={"other_run_ids": ["missing"]}).status_code
        == 404
    )


def test_merge_rejects_missing_report_via_lib(monkeypatch, client):
    async def _fake_merge(backend, writer, run_ids, router, **kw):
        raise ValueError("report not found: nope")

    monkeypatch.setattr("deep_research.library.merge.merge_reports", _fake_merge)
    r = client.post("/api/reports/run_a/merge", json={"other_run_ids": ["run_b"]})
    assert r.status_code == 400


# -- Attach job --


class FakeAttachRunner:
    """Injectable attach runner recording its inputs."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.result: dict = {"status": "attached", "artifact_id": "src", "analysis_id": "an"}
        self.delay = 0.01

    async def __call__(self, cfg, url, run_id, progress):
        import asyncio

        self.calls.append((url, run_id))
        progress.phase("attach.fetch", url)
        await asyncio.sleep(self.delay)
        return self.result


@pytest.fixture
def attach_client(seeded_config: Path, tmp_path):
    runner = FakeAttachRunner()
    app = create_app(
        config_path=str(seeded_config),
        attach_runner=runner,
        checkpoint_dir=tmp_path / "checkpoints",
    )
    app.state.fake_attach = runner
    with TestClient(app) as c:
        yield c


def test_attach_source_job(attach_client):
    r = attach_client.post(
        "/api/research",
        json={"query": "https://example.com/new", "attach_to_run_id": "run_a"},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # Wait for the job to finish
    import time

    status = None
    deadline = time.time() + 5
    while time.time() < deadline:
        status = attach_client.get(f"/api/research/jobs/{job_id}").json()
        if status["status"] == "done":
            break
        time.sleep(0.02)

    assert status is not None
    assert status["status"] == "done"
    # done event points at the target run
    assert status["run_id"] == "run_a"
    assert status["archived"] is True
    assert attach_client.app.state.fake_attach.calls == [("https://example.com/new", "run_a")]


def test_attach_validation(attach_client):
    # Non-URL query
    assert (
        attach_client.post(
            "/api/research",
            json={"query": "not a url", "attach_to_run_id": "run_a"},
        ).status_code
        == 422
    )
    # Missing target
    assert (
        attach_client.post(
            "/api/research",
            json={"query": "https://example.com/x", "attach_to_run_id": "nope"},
        ).status_code
        == 404
    )


def test_attach_job_not_pausable(attach_client):
    r = attach_client.post(
        "/api/research",
        json={"query": "https://example.com/new", "attach_to_run_id": "run_a"},
    )
    job_id = r.json()["job_id"]
    # Pause must be rejected for attach jobs
    assert attach_client.post(f"/api/research/jobs/{job_id}/pause").status_code == 409
