"""Web UI auth tests: login wall, register→verify→login, CSRF, isolation,
and the end-to-end legacy library migration in multi_user mode."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import deep_research.webui.app as webapp
from deep_research.accounts.passwords import hash_password
from deep_research.library.storage.rows import ReportRow as LibReportRow
from deep_research.library.storage.sqlite_backend import SqliteStorageBackend
from deep_research.webui import create_app


class CapturingSender:
    """Stands in for the email sender; records everything sent."""

    def __init__(self) -> None:
        self.emails: list = []

    async def send(self, email) -> None:
        self.emails.append(email)

    def last_token(self) -> str:
        for line in self.emails[-1].body.splitlines():
            t = line.strip()
            if t and " " not in t and len(t) >= 20:
                return t
        raise AssertionError(f"no token in body: {self.emails[-1].body!r}")


@pytest.fixture
def captured_sender(monkeypatch) -> CapturingSender:
    sender = CapturingSender()
    monkeypatch.setattr(webapp, "build_email_sender", lambda cfg: sender)
    return sender


def _write_config(root: Path, *, mode: str, owner_email: str = "") -> Path:
    cfg = root / "config.yaml"
    lines = [
        "pdl:",
        "  enabled: true",
        f"  root_dir: {root}",
        "  storage:",
        "    backend: sqlite",
        "  auth:",
        f"    mode: {mode}",
    ]
    if owner_email:
        lines.append(f"    owner_email: {owner_email}")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg


def _seed_legacy_library(root: Path) -> None:
    """Create a pre-multi-user library: <root>/index.db + artifacts + reports."""
    import asyncio

    async def _seed():
        backend = SqliteStorageBackend(str(root / "index.db"))
        await backend.connect()
        try:
            await backend.insert_report(
                LibReportRow(
                    run_id="legacy_run_1",
                    started_at="2026-01-01T00:00:00+00:00",
                    completed_at="2026-01-01T01:00:00+00:00",
                    original_query="What is a transformer?",
                    path_taken="deep",
                    markdown="# What is a transformer?\n\nLegacy report body.",
                )
            )
        finally:
            await backend.close()

    asyncio.run(_seed())
    day_dir = root / "reports" / "2026" / "01" / "01"
    day_dir.mkdir(parents=True)
    (day_dir / "legacy_run_1.md").write_text("# What is a transformer?", encoding="utf-8")


async def _precreate_owner_with_password(root: Path, email: str, password: str) -> None:
    from deep_research.accounts.emails import ConsoleEmailSender
    from deep_research.accounts.service import AuthService
    from deep_research.accounts.store import AccountStore
    from deep_research.config import PDLAuthConfig

    store = AccountStore(root / "registry.db")
    await store.connect()
    auth = AuthService(store, ConsoleEmailSender(), PDLAuthConfig())
    owner = await auth.provision_owner(email)
    await store.set_password(owner.user_id, hash_password(password))
    await store.close()


# -- single_user mode (default): legacy behavior --


def test_single_user_me_returns_owner(tmp_path):
    cfg = _write_config(tmp_path / "lib" and tmp_path, mode="single_user")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        body = r.json()
        assert body["auth_mode"] == "single_user"
        assert body["email"] == "owner@localhost"


def test_single_user_login_disabled(tmp_path):
    cfg = _write_config(tmp_path, mode="single_user")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/login", json={"email": "a@example.com", "password": "whatever123"}
        )
        assert r.status_code == 400


# -- multi_user mode --


def test_multi_user_requires_login(tmp_path):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        r = client.get("/api/reports")
        assert r.status_code == 401
        assert client.get("/api/auth/me").status_code == 401


def test_multi_user_requires_owner_email(tmp_path):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="")
    app = create_app(config_path=str(cfg))
    with pytest.raises(RuntimeError, match="owner_email"), TestClient(app):
        pass


def test_register_verify_login_roundtrip(tmp_path, captured_sender):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        # Anonymous users are walled off.
        assert client.get("/api/reports").status_code == 401

        r = client.post(
            "/api/auth/register",
            json={"email": "Alice@Example.COM", "password": "correct horse battery"},
        )
        assert r.status_code == 202
        assert r.json()["status"] == "pending"

        token = captured_sender.last_token()
        r = client.post("/api/auth/verify", json={"email": "alice@example.com", "token": token})
        assert r.status_code == 200
        assert "pdl_session" in client.cookies

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == "alice@example.com"  # normalized
        assert me.json()["auth_mode"] == "multi_user"

        # The verified user can use the library API (their own, empty library).
        reports = client.get("/api/reports")
        assert reports.status_code == 200
        assert reports.json()["total"] == 0


def test_login_rejects_wrong_password(tmp_path, captured_sender):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        client.post(
            "/api/auth/register",
            json={"email": "a@example.com", "password": "correct horse battery"},
        )
        client.post(
            "/api/auth/verify",
            json={"email": "a@example.com", "token": captured_sender.last_token()},
        )
        r = client.post(
            "/api/auth/login", json={"email": "a@example.com", "password": "wrong password"}
        )
        assert r.status_code == 401


def test_register_rejects_invalid_email_and_weak_password(tmp_path, captured_sender):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/register", json={"email": "not-an-email", "password": "long enough pw"}
        )
        assert r.status_code == 422
        r = client.post("/api/auth/register", json={"email": "a@example.com", "password": "short"})
        assert r.status_code == 422


def test_csrf_header_required_for_mutations(tmp_path, captured_sender):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        client.post(
            "/api/auth/register",
            json={"email": "a@example.com", "password": "correct horse battery"},
        )
        client.post(
            "/api/auth/verify",
            json={"email": "a@example.com", "token": captured_sender.last_token()},
        )

        # Authenticated POST without the header -> 403 (cross-site forms can't set it).
        r = client.post("/api/reports/run_a/tags", json={"tag": "x"})
        assert r.status_code == 403
        # With the SPA's header the request passes the CSRF gate (404 here:
        # run_a doesn't exist in this empty library — proving auth passed).
        r = client.post(
            "/api/reports/run_a/tags",
            json={"tag": "x"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r.status_code == 404

        # Auth endpoints are CSRF-exempt (no session yet at login time).
        r = client.post(
            "/api/auth/login", json={"email": "a@example.com", "password": "correct horse battery"}
        )
        assert r.status_code == 200


def test_logout_revokes_session(tmp_path, captured_sender):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        client.post(
            "/api/auth/register",
            json={"email": "a@example.com", "password": "correct horse battery"},
        )
        client.post(
            "/api/auth/verify",
            json={"email": "a@example.com", "token": captured_sender.last_token()},
        )
        assert client.get("/api/auth/me").status_code == 200

        r = client.post("/api/auth/logout")
        assert r.status_code == 204
        assert client.get("/api/auth/me").status_code == 401


def test_per_user_library_isolation(tmp_path, captured_sender):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
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

        # Seed a report into A's personal library (on disk, per-user layout).
        import asyncio

        async def _seed():
            backend = SqliteStorageBackend(
                str(tmp_path / "users" / uids["a@example.com"] / "index.db")
            )
            await backend.connect()
            try:
                await backend.insert_report(
                    LibReportRow(
                        run_id="run_of_a",
                        started_at="2026-01-01T00:00:00+00:00",
                        original_query="A's private research",
                        path_taken="quick",
                        markdown="# A private",
                    )
                )
            finally:
                await backend.close()

        asyncio.run(_seed())

        # A signs in and sees their report.
        client.post(
            "/api/auth/login", json={"email": "a@example.com", "password": "correct horse battery"}
        )
        body = client.get("/api/reports").json()
        assert body["total"] == 1
        assert body["items"][0]["run_id"] == "run_of_a"
        client.post("/api/auth/logout")

        # B signs in and sees nothing of A's library.
        client.post(
            "/api/auth/login", json={"email": "b@example.com", "password": "correct horse battery"}
        )
        body = client.get("/api/reports").json()
        assert body["total"] == 0
        # Direct access to A's run is also scoped away.
        assert client.get("/api/reports/run_of_a").status_code == 404


def test_legacy_migration_end_to_end(tmp_path):
    root = tmp_path
    _seed_legacy_library(root)
    password = "owner password 1"

    import asyncio

    asyncio.run(_precreate_owner_with_password(root, "wang.xun@gmail.com", password))

    cfg = _write_config(root, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/login", json={"email": "wang.xun@gmail.com", "password": password}
        )
        assert r.status_code == 200

        # The legacy report is visible through the owner's migrated library.
        body = client.get("/api/reports").json()
        assert body["total"] == 1
        assert body["items"][0]["run_id"] == "legacy_run_1"

        # The markdown file moved with the library.
        me = client.get("/api/auth/me").json()
        user_dir = root / "users" / me["user_id"]
        assert (user_dir / "index.db").exists()
        assert (user_dir / "reports" / "2026" / "01" / "01" / "legacy_run_1.md").exists()
        assert not (root / "index.db").exists()


def test_password_reset_flow_http(tmp_path, captured_sender):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        client.post(
            "/api/auth/register",
            json={"email": "a@example.com", "password": "correct horse battery"},
        )
        client.post(
            "/api/auth/verify",
            json={"email": "a@example.com", "token": captured_sender.last_token()},
        )

        r = client.post("/api/auth/request-password-reset", json={"email": "a@example.com"})
        assert r.status_code == 202
        reset_token = captured_sender.last_token()

        r = client.post(
            "/api/auth/reset",
            json={"email": "a@example.com", "token": reset_token, "new_password": "brand new pw 1"},
        )
        assert r.status_code == 200
        # New password works.
        client.cookies.clear()
        r = client.post(
            "/api/auth/login", json={"email": "a@example.com", "password": "brand new pw 1"}
        )
        assert r.status_code == 200
        # Old password no longer works.
        client.cookies.clear()
        r = client.post(
            "/api/auth/login", json={"email": "a@example.com", "password": "correct horse battery"}
        )
        assert r.status_code == 401


def test_unknown_password_reset_is_silent(tmp_path, captured_sender):
    cfg = _write_config(tmp_path, mode="multi_user", owner_email="wang.xun@gmail.com")
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        r = client.post("/api/auth/request-password-reset", json={"email": "ghost@example.com"})
        assert r.status_code == 202
        assert captured_sender.emails == []
