"""Multi-user research job scoping: per-user caps, ownership, config derivation,
and checkpoint ownership restore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import deep_research.webui.app as webapp
from deep_research.state import Report
from deep_research.webui import create_app


def _write_config(root: Path, *, owner_email: str = "wang.xun@gmail.com") -> Path:
    cfg = root / "config.yaml"
    cfg.write_text(
        "pdl:\n"
        "  enabled: true\n"
        f"  root_dir: {root}\n"
        "  storage:\n"
        "    backend: sqlite\n"
        "  auth:\n"
        "    mode: multi_user\n"
        f"    owner_email: {owner_email}\n",
        encoding="utf-8",
    )
    return cfg


class CapturingSender:
    def __init__(self) -> None:
        self.emails: list = []

    async def send(self, email) -> None:
        self.emails.append(email)

    def last_token(self) -> str:
        for line in self.emails[-1].body.splitlines():
            t = line.strip()
            if t and " " not in t and len(t) >= 20:
                return t
        raise AssertionError("no token in email body")


@pytest.fixture
def captured_sender(monkeypatch) -> CapturingSender:
    sender = CapturingSender()
    monkeypatch.setattr(webapp, "build_email_sender", lambda cfg: sender)
    return sender


def _report(query: str) -> Report:
    return Report(
        query=query,
        path="quick",
        markdown=f"# {query}\n\nbody",
        citations=[],
        iterations=1,
    )


def _capturing_runner(configs: list, monkeypatch):
    """Inject a research_runner that records the config it receives."""

    async def runner(config, query, path_override, progress, run_id):
        configs.append(config)
        return _report(query)

    monkeypatch.setattr(webapp, "ResearchJobManager", _patched_manager_with(runner))
    return runner


def _patched_manager_with(runner):
    original = webapp.ResearchJobManager

    def factory(*args, **kwargs):
        kwargs["runner"] = runner
        return original(*args, **kwargs)

    return factory


def test_per_user_job_cap_and_ownership(tmp_path, captured_sender, monkeypatch):
    """A's running job neither blocks B nor leaks to B."""
    configs: list = []
    runner = _make_runner(configs, monkeypatch)
    cfg = _write_config(tmp_path)
    app = create_app(config_path=str(cfg), research_runner=runner)
    with TestClient(app) as client:
        _signin(client, captured_sender)

        # A starts a job (blocking FakeRunner so it stays "running").
        client.post(
            "/api/auth/login", json={"email": "a@example.com", "password": "correct horse battery"}
        )
        started = client.post(
            "/api/research",
            json={"query": "A's question"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert started.status_code == 202
        job_a = started.json()["job_id"]

        # A is at the per-user cap: a second job is rejected with 409.
        second = client.post(
            "/api/research",
            json={"query": "A again"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert second.status_code == 409

        # B is not blocked by A's job.
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login", json={"email": "b@example.com", "password": "correct horse battery"}
        )
        started_b = client.post(
            "/api/research",
            json={"query": "B's question"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert started_b.status_code == 202
        job_b = started_b.json()["job_id"]

        # Ownership: B cannot see/cancel A's job (404, not 403).
        assert client.get(f"/api/research/jobs/{job_a}").status_code == 404
        assert (
            client.post(
                f"/api/research/jobs/{job_a}/cancel", headers={"X-Requested-With": "XMLHttpRequest"}
            ).status_code
            == 404
        )

        # A's job list shows only A's job.
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login", json={"email": "a@example.com", "password": "correct horse battery"}
        )
        jobs = client.get("/api/research/jobs").json()
        assert [j["job_id"] for j in jobs] == [job_a]

        # B's job list shows only B's job.
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login", json={"email": "b@example.com", "password": "correct horse battery"}
        )
        jobs = client.get("/api/research/jobs").json()
        assert [j["job_id"] for j in jobs] == [job_b]

        client.post(
            f"/api/research/jobs/{job_b}/cancel", headers={"X-Requested-With": "XMLHttpRequest"}
        )


def _make_runner(configs: list, monkeypatch):
    from deep_research.webui import jobs as jobs_mod

    async def runner(config, query, path_override, progress, run_id):
        configs.append(config)
        return _report(query)

    original_init = jobs_mod.ResearchJobManager.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["runner"] = runner
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(jobs_mod.ResearchJobManager, "__init__", patched_init)
    return runner


def _signin(client, captured_sender) -> dict[str, str]:
    uids = {}
    for email in ("a@example.com", "b@example.com"):
        client.post(
            "/api/auth/register", json={"email": email, "password": "correct horse battery"}
        )
        client.post(
            "/api/auth/verify", json={"email": email, "token": captured_sender.last_token()}
        )
        uids[email] = client.get("/api/auth/me").json()["user_id"]
        client.post("/api/auth/logout")
    return uids


def test_job_pipeline_uses_user_library_root(tmp_path, captured_sender, monkeypatch):
    """The runner (and its LibraryWriter) receives a config rooted at the
    owner's per-user library directory."""
    configs: list = []
    runner = _make_runner(configs, monkeypatch)
    cfg = _write_config(tmp_path)
    app = create_app(config_path=str(cfg), research_runner=runner)
    with TestClient(app) as client:
        client.post(
            "/api/auth/register",
            json={"email": "a@example.com", "password": "correct horse battery"},
        )
        client.post(
            "/api/auth/verify",
            json={"email": "a@example.com", "token": captured_sender.last_token()},
        )
        uid = client.get("/api/auth/me").json()["user_id"]

        started = client.post(
            "/api/research",
            json={"query": "scope check"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert started.status_code == 202
        job_id = started.json()["job_id"]

        # Wait for the (instant) fake runner to finish.
        import time

        for _ in range(100):
            if client.get(f"/api/research/jobs/{job_id}").json()["status"] == "done":
                break
            time.sleep(0.05)
        assert client.get(f"/api/research/jobs/{job_id}").json()["status"] == "done"

    assert len(configs) == 1
    expected_root = tmp_path / "users" / uid
    assert Path(configs[0].pdl.root_dir) == expected_root
    # The report landed in the user's own library DB.
    assert (expected_root / "index.db").exists()


def test_restore_paused_uses_user_sidecar(tmp_path, monkeypatch):
    """Checkpoint + .user sidecar → restored job belongs to that user."""
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir()
    (checkpoint_dir / "runX.json").write_text(
        json.dumps({"run_id": "runX", "state": {"query": "restored question"}}),
        encoding="utf-8",
    )
    (checkpoint_dir / "runX.user").write_text("b" * 32, encoding="utf-8")

    from deep_research.webui.jobs import ResearchJobManager

    manager = ResearchJobManager(str(tmp_path / "config.yaml"), checkpoint_dir=checkpoint_dir)
    restored = manager.restore_paused(owner_user_id="a" * 32)
    assert len(restored) == 1
    assert restored[0].user_id == "b" * 32
    assert restored[0].run_id == "runX"

    # Without a sidecar the job is attributed to the owner.
    (checkpoint_dir / "runY.json").write_text(
        json.dumps({"run_id": "runY", "state": {"query": "legacy job"}}),
        encoding="utf-8",
    )
    restored2 = manager.restore_paused(owner_user_id="a" * 32)
    assert [j.user_id for j in restored2] == ["a" * 32]
