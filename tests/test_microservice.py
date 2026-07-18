"""Tests for FastAPI microservice (P12.0)."""

from __future__ import annotations

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
