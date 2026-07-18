"""Tests for FastAPI microservice endpoint (P12.0)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from deep_research.microservice import app


def test_health():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_research_invalid():
    client = TestClient(app)
    resp = client.post("/research", json={})
    assert resp.status_code == 422


def test_research_empty_query():
    client = TestClient(app)
    resp = client.post("/research", json={"query": ""})
    assert resp.status_code in (200, 422)
