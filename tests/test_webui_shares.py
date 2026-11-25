"""View-only report sharing: links, named grants, authorization, isolation."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import deep_research.webui.app as webapp
from deep_research.library.storage.rows import ArtifactRow, ReportRow
from deep_research.library.storage.sqlite_backend import SqliteStorageBackend
from deep_research.webui import create_app


def _multi_config(root: Path, owner_email: str = "wang.xun@gmail.com") -> Path:
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


def _single_config(root: Path) -> Path:
    cfg = root / "config.yaml"
    cfg.write_text(
        f"pdl:\n  enabled: true\n  root_dir: {root}\n  storage:\n    backend: sqlite\n",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def captured_sender(monkeypatch) -> list:
    sent = []

    class Cap:
        async def send(self, email) -> None:
            sent.append(email)

    monkeypatch.setattr(webapp, "build_email_sender", lambda cfg: Cap())
    return sent


def _token_from_body(body: str) -> str:
    for line in body.splitlines():
        t = line.strip()
        if t and " " not in t and len(t) >= 20:
            return t
    raise AssertionError(f"no token in body: {body!r}")


def _register_and_login(client, captured_sender, email: str) -> str:
    """Register + verify + login; returns the user_id."""
    client.post("/api/auth/register", json={"email": email, "password": "correct horse battery"})
    token = _token_from_body(captured_sender[-1].body)
    client.post("/api/auth/verify", json={"email": email, "token": token})
    uid = client.get("/api/auth/me").json()["user_id"]
    # Keep the session (stay logged in as this user).
    return uid


def _seed_owner_library(root: Path, uid: str, *, with_pdf: bool = True) -> None:
    """Seed a report (+ optional archived PDF artifact) into the owner's library."""

    async def _seed():
        user_dir = root / "users" / uid
        backend = SqliteStorageBackend(str(user_dir / "index.db"))
        await backend.connect()
        try:
            artifact_id = None
            if with_pdf:
                artifact_id = "art_pdf_1"
                pdf_dir = user_dir / "artifacts" / "pdf"
                pdf_dir.mkdir(parents=True, exist_ok=True)
                (pdf_dir / "art_pdf_1.pdf").write_bytes(b"%PDF-1.4 shared pdf")
                await backend.upsert_artifact(
                    ArtifactRow(
                        artifact_id=artifact_id,
                        kind="pdf",
                        source_type="research_report",
                        title="Report artifact",
                        bytes_path="artifacts/pdf/art_pdf_1.pdf",
                        bytes_size=16,
                        first_seen_at="2026-01-01T00:00:00+00:00",
                        last_touched_at="2026-01-01T00:00:00+00:00",
                    )
                )
            await backend.insert_report(
                ReportRow(
                    run_id="owner_run_1",
                    started_at="2026-01-01T00:00:00+00:00",
                    completed_at="2026-01-01T01:00:00+00:00",
                    original_query="Shared research question",
                    path_taken="deep",
                    markdown="# Shared research question\n\nFindings here.",
                    artifact_id=artifact_id,
                    citations_json=json.dumps(
                        [
                            {
                                "url": "https://arxiv.org/abs/2401.00001",
                                "title": "Paper B",
                                "source_type": "arxiv",
                            }
                        ]
                    ),
                )
            )
        finally:
            await backend.close()

    asyncio.run(_seed())


def test_single_user_sharing_disabled(tmp_path):
    cfg = _single_config(tmp_path)
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        r = client.post("/api/shares", json={"run_id": "whatever1234"})
        assert r.status_code == 400
        assert "multi_user" in r.json()["detail"]
        assert client.get("/api/shared").json() == []


def test_share_link_roundtrip(tmp_path, captured_sender):
    root = tmp_path
    cfg = _multi_config(root)
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        uid = _register_and_login(client, captured_sender, "owner@example.com")
        client.post("/api/auth/logout")
        _seed_owner_library(root, uid)
        client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "correct horse battery"},
        )

        # Create a link share (no named grants).
        r = client.post(
            "/api/shares",
            json={"run_id": "owner_run_1"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["run_id"] == "owner_run_1"
        assert body["token"] and body["url"] == f"/#/share/{body['token']}"
        token = body["token"]

        # The link works WITHOUT any session.
        r2 = client.get(f"/api/share/{token}")
        assert r2.status_code == 200
        detail = r2.json()
        assert detail["query"] == "Shared research question"
        assert detail["has_pdf"] is True
        assert detail["pdf_url"] == f"/api/share/{token}/pdf"
        assert len(detail["citations"]) == 1

        # PDF + markdown assets serve from the OWNER's library.
        assert client.get(f"/api/share/{token}/pdf").content == b"%PDF-1.4 shared pdf"
        md = client.get(f"/api/share/{token}/markdown")
        assert md.status_code == 200 and "Findings here." in md.text

        # Owner sees the share in their list (no raw token in listings).
        listing = client.get("/api/shares").json()
        assert len(listing) == 1 and listing[0]["share_id"] == body["share_id"]
        assert "token" not in listing[0]

        # A bad token is 404, indistinguishable from revoked.
        assert client.get("/api/share/deadbeefdeadbeefdeadbeefdeadbeef").status_code == 404


def test_named_grant_roundtrip_and_isolation(tmp_path, captured_sender):
    root = tmp_path
    cfg = _multi_config(root)
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        owner_uid = _register_and_login(client, captured_sender, "owner@example.com")
        client.post("/api/auth/logout")
        _seed_owner_library(root, owner_uid)
        # The colleague must exist before the owner can share with them.
        _register_and_login(client, captured_sender, "colleague@example.com")
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "correct horse battery"},
        )

        # Share directly with a registered colleague.
        r = client.post(
            "/api/shares",
            json={"run_id": "owner_run_1", "with_emails": ["colleague@Example.COM"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r.status_code == 201, r.text
        share_id = r.json()["share_id"]
        assert r.json()["grants"] == ["colleague@example.com"]  # normalized
        client.post("/api/auth/logout")

        # Grantee signs in (already registered): sees it under "Shared with me".
        client.post(
            "/api/auth/login",
            json={"email": "colleague@example.com", "password": "correct horse battery"},
        )
        listed = client.get("/api/shared").json()
        assert [i["share_id"] for i in listed] == [share_id]
        assert listed[0]["owner_email"] == "owner@example.com"

        detail = client.get(f"/api/shared/{share_id}")
        assert detail.status_code == 200
        assert detail.json()["query"] == "Shared research question"
        assert detail.json()["pdf_url"] == f"/api/shared/{share_id}/pdf"
        assert client.get(f"/api/shared/{share_id}/pdf").content == b"%PDF-1.4 shared pdf"

        # The grantee's OWN library is untouched (isolation holds).
        assert client.get("/api/reports").json()["total"] == 0

        # A stranger cannot even see the share id exists.
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/register",
            json={"email": "stranger@example.com", "password": "correct horse battery"},
        )
        stranger_token = _token_from_body(captured_sender[-1].body)
        client.post(
            "/api/auth/verify", json={"email": "stranger@example.com", "token": stranger_token}
        )
        assert client.get("/api/shared").json() == []
        assert client.get(f"/api/shared/{share_id}").status_code == 404
        # Strangers cannot manage the share either (owner-only, 404 not 403).
        assert (
            client.delete(
                f"/api/shares/{share_id}", headers={"X-Requested-With": "XMLHttpRequest"}
            ).status_code
            == 404
        )


def test_share_revocation_and_rotation(tmp_path, captured_sender):
    root = tmp_path
    cfg = _multi_config(root)
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        owner_uid = _register_and_login(client, captured_sender, "owner@example.com")
        client.post("/api/auth/logout")
        _seed_owner_library(root, owner_uid)
        client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "correct horse battery"},
        )
        created = client.post(
            "/api/shares",
            json={"run_id": "owner_run_1"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        ).json()
        old_token, share_id = created["token"], created["share_id"]

        # Rotation invalidates the old link; the new one works.
        rotated = client.post(
            f"/api/shares/{share_id}/rotate", headers={"X-Requested-With": "XMLHttpRequest"}
        ).json()
        assert client.get(f"/api/share/{old_token}").status_code == 404
        assert client.get(f"/api/share/{rotated['token']}").status_code == 200

        # Revocation kills both the link and the share row.
        r = client.delete(f"/api/shares/{share_id}", headers={"X-Requested-With": "XMLHttpRequest"})
        assert r.status_code == 204
        assert client.get(f"/api/share/{rotated['token']}").status_code == 404
        assert client.get("/api/shares").json() == []


def test_share_expiry(tmp_path, captured_sender):
    root = tmp_path
    cfg = _multi_config(root)
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        owner_uid = _register_and_login(client, captured_sender, "owner@example.com")
        client.post("/api/auth/logout")
        _seed_owner_library(root, owner_uid)
        client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "correct horse battery"},
        )
        created = client.post(
            "/api/shares",
            json={"run_id": "owner_run_1", "expires_in_days": 7},
            headers={"X-Requested-With": "XMLHttpRequest"},
        ).json()
        assert created["expires_at"]
        token = created["token"]
        assert client.get(f"/api/share/{token}").status_code == 200

        # Force the share into the past directly in the registry.
        async def _expire():
            from deep_research.accounts.store import AccountStore

            store = AccountStore(root / "registry.db")
            await store.connect()
            try:
                past = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
                await store.meta_set("noop", "x")  # keep the fixture honest
                # Direct update via a raw statement through the store's conn is
                # not public API — use the migration-tested table by writing a
                # fresh share row with a past expiry instead.
                await store._conn.execute(
                    "UPDATE shares SET expires_at = ? WHERE share_id = ?",
                    (past, created["share_id"]),
                )
                await store._conn.commit()
            finally:
                await store.close()

        asyncio.run(_expire())
        assert client.get(f"/api/share/{token}").status_code == 404


def test_share_unknown_grantee_and_missing_run(tmp_path, captured_sender):
    root = tmp_path
    cfg = _multi_config(root)
    app = create_app(config_path=str(cfg))
    with TestClient(app) as client:
        owner_uid = _register_and_login(client, captured_sender, "owner@example.com")
        client.post("/api/auth/logout")
        _seed_owner_library(root, owner_uid)
        client.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "correct horse battery"},
        )
        # Nonexistent run → 404.
        assert (
            client.post(
                "/api/shares",
                json={"run_id": "missing_run_00"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            ).status_code
            == 404
        )
        # Existing run, unknown grantee email → 404 with a helpful message.
        r = client.post(
            "/api/shares",
            json={"run_id": "owner_run_1", "with_emails": ["ghost@example.com"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r.status_code == 404
        assert "register first" in r.json()["detail"]
        # Nothing was created for the failed request.
        assert client.get("/api/shares").json() == []
