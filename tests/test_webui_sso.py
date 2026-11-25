"""Trusted-reverse-proxy SSO: header identity, JIT provisioning, required mode.

The console's nginx is the only trusted proxy; these tests use Starlette's
TestClient host name as the stand-in proxy address.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import deep_research.webui.app as webapp
from deep_research.webui import create_app


def _write_config(
    root: Path,
    *,
    trusted: str | list[str] = "testclient",
    auto_provision: bool = True,
    required: bool = True,
    enabled: bool = True,
    mode: str = "multi_user",
) -> Path:
    proxies = [trusted] if isinstance(trusted, str) else trusted
    trusted_yaml = "[" + ", ".join(f'"{p}"' for p in proxies) + "]"
    cfg = root / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "pdl:",
                "  enabled: true",
                f"  root_dir: {root}",
                "  storage:",
                "    backend: sqlite",
                "  auth:",
                f"    mode: {mode}",
                '    owner_email: "wang.xun@gmail.com"',
                "    sso:",
                f"      enabled: {str(enabled).lower()}",
                '      header: "X-Auth-User"',
                f"      trusted_proxies: {trusted_yaml}",
                f"      auto_provision: {str(auto_provision).lower()}",
                f"      required: {str(required).lower()}",
                '      workspace_url: "/"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture(autouse=True)
def _silence_email(monkeypatch):
    """No SMTP in these tests; the app falls back to the console sender."""
    monkeypatch.setattr(webapp, "build_email_sender", lambda cfg: _NoopSender())


class _NoopSender:
    async def send(self, email) -> None:  # pragma: no cover - not exercised
        return None


def test_header_provisions_and_authenticates(tmp_path):
    app = create_app(config_path=str(_write_config(tmp_path)))
    with TestClient(app) as client:
        r = client.get("/api/auth/status", headers={"X-Auth-User": "Alice@Example.COM"})
        assert r.status_code == 200
        body = r.json()
        assert body["authenticated"] is True
        assert body["sso_required"] is True
        assert body["workspace_url"] == "/"
        assert body["user"]["email"] == "alice@example.com"
        assert body["user"]["sso"] is True

        # Every scoped API request carries the same asserted identity.
        r = client.get("/api/reports", headers={"X-Auth-User": "alice@example.com"})
        assert r.status_code == 200
        assert r.json()["total"] == 0

        # A second sight of the same address links to the same account.
        again = client.get("/api/auth/me", headers={"X-Auth-User": "ALICE@example.com"})
        assert again.status_code == 200
        assert again.json()["user_id"] == body["user"]["user_id"]


def test_untrusted_source_is_ignored_and_local_login_is_closed(tmp_path):
    app = create_app(config_path=str(_write_config(tmp_path, trusted="192.0.2.10")))
    with TestClient(app) as client:
        r = client.get("/api/auth/status", headers={"X-Auth-User": "alice@example.com"})
        assert r.status_code == 200
        assert r.json()["authenticated"] is False

        r = client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "correct horse battery"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r.status_code == 403
        assert "single sign-on required" in r.json()["detail"]

        r = client.post(
            "/api/auth/register",
            json={"email": "alice@example.com", "password": "correct horse battery"},
        )
        assert r.status_code == 403


def test_malformed_header_is_refused(tmp_path):
    app = create_app(config_path=str(_write_config(tmp_path)))
    with TestClient(app) as client:
        r = client.get("/api/auth/status", headers={"X-Auth-User": "not-an-email"})
        assert r.status_code == 422
        assert "invalid email address" in r.json()["detail"]


def test_auto_provision_can_be_disabled(tmp_path):
    app = create_app(config_path=str(_write_config(tmp_path, auto_provision=False)))
    with TestClient(app) as client:
        r = client.get("/api/auth/status", headers={"X-Auth-User": "nobody@example.com"})
        assert r.status_code == 403
        assert "not provisioned" in r.json()["detail"]


def test_required_blocks_local_login_even_with_valid_header(tmp_path):
    app = create_app(config_path=str(_write_config(tmp_path)))
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "correct horse battery"},
            headers={
                "X-Auth-User": "alice@example.com",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        assert r.status_code == 403
        assert "single sign-on required" in r.json()["detail"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"enabled": False, "required": True}, "requires pdl.auth.sso.enabled"),
        ({"trusted": [], "required": False}, "trusted_proxies must not be empty"),
        ({"mode": "single_user", "required": False}, "requires pdl.auth.mode: multi_user"),
    ],
)
def test_invalid_sso_config_fails_closed(tmp_path, kwargs, message):
    cfg = _write_config(tmp_path, **kwargs)
    with pytest.raises(Exception, match=message), TestClient(create_app(config_path=str(cfg))):
        pass
