"""Tests for FastAPI microservice (P12.0)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from deep_research.microservice import app


def test_health_endpoint():
    """GET /health returns ok status."""
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_research_endpoint_validation():
    """POST /research with missing query returns validation error."""
    client = TestClient(app)
    resp = client.post("/research", json={})
    assert resp.status_code == 422


def test_config_path_outside_allowed_rejected():
    """An absolute config_path resolving outside cwd is rejected."""
    from deep_research.microservice import ResearchRequest, research_endpoint

    request = ResearchRequest(query="test", config_path="/etc/whatever/config.yaml")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(research_endpoint(request))
    assert exc.value.status_code == 400


def test_config_path_sibling_prefix_rejected(tmp_path, monkeypatch):
    """A sibling directory whose name shares a prefix with the allowed dir
    (the old startswith() check would have accepted it) must be rejected."""
    import deep_research.microservice as mod

    allowed = tmp_path / "project"
    evil = tmp_path / "project_evil"
    allowed.mkdir()
    evil.mkdir()
    monkeypatch.setattr(mod, "_ALLOWED_CONFIG_DIR", allowed.resolve())

    request = mod.ResearchRequest(query="test", config_path=str((evil / "evil.yaml").resolve()))
    with pytest.raises(mod.HTTPException) as exc:
        asyncio.run(mod.research_endpoint(request))
    assert exc.value.status_code == 400
