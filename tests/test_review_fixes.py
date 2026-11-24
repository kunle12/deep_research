"""Regression tests for the adversarial-review findings (see
docs/MULTI_USER_IMPLEMENTATION_LOG.md — H1, H2, H3, M5, L1, L2, L4, L6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deep_research.accounts.bootstrap import migrate_legacy_library
from deep_research.accounts.rows import UserRow
from deep_research.accounts.store import AccountStore
from deep_research.config import AgentTopConfig, PDLAuthConfig
from deep_research.library.storage.registry import LibraryRegistry, validate_user_id
from deep_research.webui.jobs import ResearchJobManager, _with_library_root


def _multi_cfg(root: Path, *, backend: str = "sqlite") -> AgentTopConfig:
    return AgentTopConfig(
        pdl={
            "root_dir": str(root),
            "storage": {"backend": backend},
            "auth": PDLAuthConfig(mode="multi_user", owner_email="wang.xun@gmail.com"),
        }
    )


# -- H1: concurrent backend_for must open exactly one backend per user --


async def test_concurrent_backend_for_opens_once(tmp_path):
    reg = LibraryRegistry(_multi_cfg(tmp_path / "lib"))
    try:
        uid = "a" * 32
        results = await asyncio.gather(reg.backend_for(uid), reg.backend_for(uid))
        assert results[0] is results[1]
        # Cache holds exactly one backend; close_all closes exactly it.
        assert len(reg._backends) == 1
    finally:
        await reg.close_all()


async def test_concurrent_backend_for_distinct_users(tmp_path):
    reg = LibraryRegistry(_multi_cfg(tmp_path / "lib"))
    try:
        results = await asyncio.gather(reg.backend_for("a" * 32), reg.backend_for("b" * 32))
        assert results[0] is not results[1]
    finally:
        await reg.close_all()


# -- L1: validate_user_id rejects a trailing newline --


@pytest.mark.parametrize("bad", ["a" * 32 + "\n", "\n" + "a" * 32, "a" * 32 + " "])
def test_user_id_rejects_whitespace_variants(bad):
    with pytest.raises(ValueError):
        validate_user_id(bad)


# -- M5: _prune never evicts paused (resumable) jobs --


def test_prune_keeps_paused_jobs(tmp_path):
    manager = ResearchJobManager(str(tmp_path / "config.yaml"), checkpoint_dir=tmp_path / "cp")
    # Fill the manager with finished jobs plus one paused job.
    paused = ResearchJobManager.__dict__  # noqa: F841 — clarity only
    from deep_research.webui.jobs import ResearchJob

    paused_job = ResearchJob(job_id="paused1", query="p", status="paused")
    paused_job.started_at = 1.0
    manager._jobs[paused_job.job_id] = paused_job
    for i in range(60):
        finished = ResearchJob(job_id=f"done{i}", query="q", status="done")
        finished.started_at = 10.0 + i
        finished.completed_at = 10.0 + i
        manager._jobs[f"done{i}"] = finished
    manager._prune()
    assert "paused1" in manager._jobs
    # Finished jobs were pruned down to the cap.
    assert len(manager._jobs) <= 50


# -- L2: corrupt sidecar attributes to owner; unresolvable user fails the job --


def test_restore_paused_ignores_corrupt_sidecar(tmp_path):
    cp = tmp_path / "cp"
    cp.mkdir()
    (cp / "runZ.json").write_text('{"run_id": "runZ", "state": {"query": "q"}}', encoding="utf-8")
    (cp / "runZ.user").write_text("owner\n", encoding="utf-8")  # pre-multi-user junk
    manager = ResearchJobManager(str(tmp_path / "config.yaml"), checkpoint_dir=cp)
    restored = manager.restore_paused(owner_user_id="a" * 32)
    assert [j.user_id for j in restored] == ["a" * 32]


def test_with_library_root_sets_schema():
    cfg = AgentTopConfig(pdl={"root_dir": "/tmp/base", "storage": {"backend": "postgres"}})
    scoped = _with_library_root(cfg, "/tmp/users/abc", "library_abc")
    assert scoped.pdl.root_dir == "/tmp/users/abc"
    assert scoped.pdl.storage.pg_schema == "library_abc"
    # The base config is untouched.
    assert cfg.pdl.storage.pg_schema == ""
    # SQLite scoping leaves schema empty.
    sqlite_scoped = _with_library_root(cfg, "/tmp/users/abc", None)
    assert sqlite_scoped.pdl.storage.pg_schema == ""


# -- H3: scheduler enumerates postgres users from the registry --


async def test_scheduler_postgres_enumerates_registry(tmp_path):
    from deep_research.scheduler import _user_library_configs

    root = tmp_path / "lib"
    cfg = _multi_cfg(root, backend="postgres")
    store = AccountStore(root / "registry.db")
    await store.connect()
    try:
        active = await store.create_user("a@example.com", status="active")
        await store.create_user("b@example.com", status="disabled")  # skipped
    finally:
        await store.close()

    configs = await _user_library_configs(cfg)
    assert len(configs) == 1
    assert configs[0].pdl.storage.pg_schema == f"library_{active.user_id}"
    assert configs[0].pdl.root_dir == str(root / "users" / active.user_id)

    # multi_user + postgres with no users: nothing is refreshed (never the
    # global root — that would recreate the legacy library in public).
    empty_root = tmp_path / "lib2"
    cfg_empty = _multi_cfg(empty_root, backend="postgres")
    assert await _user_library_configs(cfg_empty) == []


async def test_scheduler_sqlite_multi_user_empty_users_dir(tmp_path):
    from deep_research.scheduler import _user_library_configs

    # No users/ dir at all -> [] (never the global root).
    assert await _user_library_configs(_multi_cfg(tmp_path / "lib")) == []


# -- L4: non-canonical IP literals hit the SSRF guard --


def test_ssrf_rejects_noncanonical_ip_literals():
    from deep_research.webui.routers.research import _reject_private_url

    assert _reject_private_url("http://2130706433/") is True  # 127.0.0.1 decimal
    assert _reject_private_url("http://0x7f000001/") is True  # 127.0.0.1 hex
    assert _reject_private_url("http://3232235529/") is True  # 192.168.0.9
    assert _reject_private_url("https://arxiv.org/abs/2401.00001") is False
    assert _reject_private_url("http://8.8.8.8/") is False


# -- L6: login requires the CSRF header when a session cookie exists --


def test_login_csrf_header_required(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import deep_research.webui.app as webapp
    from deep_research.webui import create_app

    class Cap:
        def __init__(self) -> None:
            self.emails: list = []

        async def send(self, email):
            pass

    monkeypatch.setattr(webapp, "build_email_sender", lambda cfg: Cap())
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "pdl:\n"
        "  enabled: true\n"
        f"  root_dir: {tmp_path}\n"
        "  storage:\n"
        "    backend: sqlite\n"
        "  auth:\n"
        "    mode: multi_user\n"
        "    owner_email: o@x.com\n",
        encoding="utf-8",
    )
    app = create_app(config_path=str(cfg_path))
    with TestClient(app) as client:
        # No session yet: login works without the header (plain API client).
        r = client.post("/api/auth/login", json={"email": "o@x.com", "password": "nope12345"})
        assert r.status_code == 401  # wrong password, but CSRF gate not applied
        # With an existing session (attacker re-auth scenario), the gate holds.
        # Provision a real session first via the owner account.
        from deep_research.accounts.emails import ConsoleEmailSender
        from deep_research.accounts.passwords import hash_password
        from deep_research.accounts.service import AuthService
        from deep_research.accounts.store import AccountStore

        async def _mk():
            store = AccountStore(tmp_path / "registry.db")
            await store.connect()
            auth = AuthService(store, ConsoleEmailSender(), PDLAuthConfig())
            owner = await auth.provision_owner("o@x.com")
            await store.set_password(owner.user_id, hash_password("real password 1"))
            await store.close()

        asyncio.run(_mk())
        client.post("/api/auth/login", json={"email": "o@x.com", "password": "real password 1"})
        # Cross-site login attempt (no header) with a live session -> 403.
        r2 = client.post(
            "/api/auth/login", json={"email": "o@x.com", "password": "real password 1"}
        )
        assert r2.status_code == 403
        # The SPA's own call carries the header -> passes.
        r3 = client.post(
            "/api/auth/login",
            json={"email": "o@x.com", "password": "real password 1"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r3.status_code == 200


# -- L8: crashed migration claim is recovered --


async def test_migration_recovers_stale_claim(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "index.db").write_bytes(b"sqlite")
    cfg = _multi_cfg(root)
    store = AccountStore(root / "registry.db")
    await store.connect()
    owner = UserRow(user_id="c" * 32, email="wang.xun@gmail.com", status="active")
    try:
        # Simulate a crashed holder: lock present, no marker, nothing moved.
        assert await store.meta_claim("legacy_migrated_lock", "deadbeef")
        moved = await migrate_legacy_library(cfg, store, owner)
        assert moved is True
        assert (root / "users" / owner.user_id / "index.db").exists()
        assert await store.meta_get("legacy_migrated") == owner.user_id
        # The lock was released after completion.
        assert await store.meta_get("legacy_migrated_lock") is None
    finally:
        await store.close()
