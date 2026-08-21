"""Tests for the Deep Research Library web UI API (P12.5 / Phase 1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deep_research.library.storage.rows import (
    AnalysisRow,
    ArtifactRow,
    CitationEdgeRow,
    GlossaryEntry,
    ReportRow,
    TagRow,
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
    """Seed a small library (reports, artifacts, tags, analysis, citation edge,
    PDF + markdown files) and return the config path pointing at it."""
    backend = SqliteStorageBackend(str(library_root / "index.db"))
    await backend.connect()
    try:
        art_a = ArtifactRow(
            artifact_id="art_a",
            kind="pdf",
            source_type="research_report",
            title="Report A",
            bytes_path="artifacts/pdf/art_a.pdf",
            bytes_size=10,
            first_seen_at=_now(1),
            last_touched_at=_now(1),
        )
        await backend.upsert_artifact(art_a)
        art_b = ArtifactRow(
            artifact_id="art_b",
            kind="pdf",
            source_type="arxiv",
            title="Paper B",
            source_url="https://arxiv.org/abs/2401.00001",
            arxiv_id="2401.00001",
            authors=json.dumps(["Ada Lovelace"]),
            bytes_path="artifacts/pdf/art_b.pdf",
            bytes_size=20,
            first_seen_at=_now(2),
            last_touched_at=_now(2),
        )
        await backend.upsert_artifact(art_b)

        md_a = (
            "# Report A\n\n"
            "Executive summary about transformers and attention mechanisms "
            "in modern NLP systems.\n\n"
            "## Bibliography\n\n1. [Paper B](https://arxiv.org/abs/2401.00001)"
        )
        report_a = ReportRow(
            run_id="run_a",
            started_at=_now(1),
            completed_at=_now(1),
            original_query="What is a transformer?",
            path_taken="deep",
            iterations=3,
            classifier_rationale="deep synthesis needed",
            markdown=md_a,
            artifact_id="art_a",
            citations_json=json.dumps(
                [
                    {
                        "url": "https://arxiv.org/abs/2401.00001",
                        "title": "Paper B",
                        "source_type": "arxiv",
                        "arxiv_id": "2401.00001",
                        "authors": ["Ada Lovelace"],
                    }
                ]
            ),
        )
        await backend.insert_report(report_a)
        await backend.insert_report(
            ReportRow(
                run_id="run_b",
                started_at=_now(2),
                completed_at=_now(2),
                original_query="Summarize attention mechanisms",
                path_taken="quick",
                iterations=0,
                markdown="# Report B\n\nShort content.",
            )
        )

        await backend.upsert_tag(TagRow(tag="ml", artifact_id="art_a", applied_in_run="run_a"))
        await backend.upsert_tag(TagRow(tag="survey", artifact_id="art_a", applied_in_run="run_a"))

        await backend.insert_analysis(
            AnalysisRow(
                analysis_id="ana1",
                artifact_id="art_b",
                run_id="run_a",
                analyzer="analyze_paper",
                summary="A paper summary about language models",
                key_findings=json.dumps(["key finding one"]),
                analyzed_at=_now(1),
            )
        )
        await backend.insert_citation_edge(
            CitationEdgeRow(
                source_artifact_id="art_b",
                target_arxiv_id="2401.54321",
                weight=0.9,
                rationale="key reference",
                discovered_in_run="run_a",
            )
        )
        await backend.upsert_glossary_entry(
            GlossaryEntry(
                term="Transformer",
                term_canonical="transformer",
                kind="concept",
                short_def="A neural network architecture using attention.",
                domain_tags=json.dumps(["nlp"]),
                first_seen_run_id="run_a",
                last_updated=_now(1),
            )
        )

        pdf_dir = library_root / "artifacts" / "pdf"
        pdf_dir.mkdir(parents=True)
        (pdf_dir / "art_a.pdf").write_bytes(b"%PDF-1.4 fake report pdf")
        (pdf_dir / "art_b.pdf").write_bytes(b"%PDF-1.4 fake paper pdf")

        day_dir = library_root / "reports" / "2026" / "07" / "01"
        day_dir.mkdir(parents=True)
        (day_dir / "run_a.md").write_text(md_a, encoding="utf-8")
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


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_list_reports(client):
    r = client.get("/api/reports")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    newest, older = body["items"]
    assert newest["run_id"] == "run_b"
    assert newest["path"] == "quick"
    assert older["run_id"] == "run_a"
    assert older["tags"] == ["ml", "survey"]
    assert older["citation_count"] == 1
    assert older["has_pdf"] is True
    assert "transformers" in older["snippet"]


def test_list_reports_filters_and_pagination(client):
    by_tag = client.get("/api/reports", params={"tag": "ml"}).json()
    assert by_tag["total"] == 1
    assert by_tag["items"][0]["run_id"] == "run_a"

    by_path = client.get("/api/reports", params={"path": "quick"}).json()
    assert by_path["total"] == 1
    assert by_path["items"][0]["run_id"] == "run_b"

    by_q = client.get("/api/reports", params={"q": "transformer"}).json()
    assert by_q["total"] == 1
    assert by_q["items"][0]["run_id"] == "run_a"

    page = client.get("/api/reports", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert page["items"][0]["run_id"] == "run_a"


def test_list_reports_validation(client):
    assert client.get("/api/reports", params={"limit": 0}).status_code == 422
    assert client.get("/api/reports", params={"q": ""}).status_code == 422


def test_report_detail(client):
    r = client.get("/api/reports/run_a")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "What is a transformer?"
    assert body["path"] == "deep"
    assert body["iterations"] == 3
    assert body["classifier_rationale"] == "deep synthesis needed"
    assert body["tags"] == ["ml", "survey"]
    assert len(body["citations"]) == 1
    assert body["citations"][0]["arxiv_id"] == "2401.00001"
    assert body["citations"][0]["local_pdf_url"] == "/api/artifacts/art_b/pdf"
    assert body["has_pdf"] is True
    assert body["pdf_url"] == "/api/reports/run_a/pdf"
    assert body["markdown_url"] == "/api/reports/run_a/markdown"
    assert body["markdown"].startswith("# Report A")

    assert client.get("/api/reports/nope").status_code == 404


def test_report_markdown_download(client):
    r = client.get("/api/reports/run_a/markdown")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("# Report A")


def test_report_detail_includes_glossary_and_export_urls(client):
    body = client.get("/api/reports/run_a").json()
    assert body["glossary_url"] == "/api/reports/run_a/glossary/markdown"
    assert body["bibliography_url"] == "/api/reports/run_a/bibliography"
    assert body["bibliography_bib_url"] == "/api/reports/run_a/bibliography/bib"
    assert len(body["glossary"]) == 1
    assert body["glossary"][0]["term"] == "Transformer"
    assert body["glossary"][0]["term_canonical"] == "transformer"
    assert body["glossary"][0]["domain_tags"] == ["nlp"]


def test_report_glossary_markdown_export(client):
    r = client.get("/api/reports/run_a/glossary/markdown")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "## Transformer" in r.text
    assert "attention" in r.text
    assert client.get("/api/reports/nope/glossary/markdown").status_code == 404


def test_report_bibliography_markdown_export(client):
    r = client.get("/api/reports/run_a/bibliography")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "## Bibliography" in r.text
    assert "Paper B" in r.text
    assert "2401.00001" in r.text
    assert client.get("/api/reports/nope/bibliography").status_code == 404


def test_report_bibliography_bib_export(client):
    r = client.get("/api/reports/run_a/bibliography/bib")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-bibtex")
    assert "@misc{cite1" in r.text
    assert "Paper B" in r.text
    assert "Ada Lovelace" in r.text
    assert client.get("/api/reports/nope/bibliography/bib").status_code == 404


def test_report_pdf(client):
    r = client.get("/api/reports/run_a/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")

    # run_b has no artifact/PDF
    assert client.get("/api/reports/run_b/pdf").status_code == 404
    assert client.get("/api/reports/nope/pdf").status_code == 404


def test_artifact_pdf(client):
    r = client.get("/api/artifacts/art_b/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")

    assert client.get("/api/artifacts/nope/pdf").status_code == 404


def test_archive_arxiv_pdf_already_archived(client):
    r = client.post("/api/arxiv/pdf", json={"arxiv_id": "2401.00001"})
    assert r.status_code == 200
    body = r.json()
    assert body["archived"] is False
    assert body["local_pdf_url"] == "/api/artifacts/art_b/pdf"


def test_archive_arxiv_pdf_download(monkeypatch, client):
    async def _fake_archive(arxiv_id, *, title, tools, writer, timeout_s=180.0):
        assert arxiv_id == "2401.99999"
        return "art_new"

    import deep_research.webui.routers.library as lib

    monkeypatch.setattr(lib, "archive_cited_pdf", _fake_archive)
    r = client.post("/api/arxiv/pdf", json={"arxiv_id": "2401.99999"})
    assert r.status_code == 200
    body = r.json()
    assert body["archived"] is True
    assert body["local_pdf_url"] == "/api/artifacts/art_new/pdf"


def test_archive_arxiv_pdf_failure(monkeypatch, client):
    async def _fake_archive(arxiv_id, *, title, tools, writer, timeout_s=180.0):
        return None

    import deep_research.webui.routers.library as lib

    monkeypatch.setattr(lib, "archive_cited_pdf", _fake_archive)
    r = client.post("/api/arxiv/pdf", json={"arxiv_id": "2401.99998"})
    assert r.status_code == 200
    assert r.json()["error"]


def test_archive_arxiv_pdf_invalid(client):
    assert client.post("/api/arxiv/pdf", json={"arxiv_id": "scholar:abc"}).status_code == 422
    assert client.post("/api/arxiv/pdf", json={"arxiv_id": ""}).status_code == 422


def test_add_and_remove_tags(client):
    r = client.post("/api/reports/run_a/tags", json={"tag": "  nlp  "})
    assert r.status_code == 200
    assert r.json()["tags"] == ["ml", "nlp", "survey"]

    r = client.delete("/api/reports/run_a/tags", params={"tag": "nlp"})
    assert r.status_code == 200
    assert r.json()["tags"] == ["ml", "survey"]

    # Reports without an artifact cannot be tagged
    assert client.post("/api/reports/run_b/tags", json={"tag": "x"}).status_code == 400
    assert client.post("/api/reports/nope/tags", json={"tag": "x"}).status_code == 404


def test_delete_report_requires_confirmation(client):
    r = client.delete("/api/reports/run_a")
    assert r.status_code == 400

    r = client.delete("/api/reports/run_a", params={"confirm": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert any(path.endswith("run_a.md") for path in body["removed_files"])
    assert client.get("/api/reports/run_a").status_code == 404


def test_tags_endpoint(client):
    r = client.get("/api/tags")
    assert r.status_code == 200
    assert {"tag": "ml", "count": 1} in r.json()
    assert {"tag": "survey", "count": 1} in r.json()


def test_artifact_detail(client):
    r = client.get("/api/artifacts/art_b")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "pdf"
    assert body["authors"] == ["Ada Lovelace"]
    assert body["analyses"][0]["key_findings"] == ["key finding one"]
    assert body["citation_edges"][0]["target_arxiv_id"] == "2401.54321"
    assert body["citation_edges"][0]["weight"] == 0.9

    assert client.get("/api/artifacts/nope").status_code == 404


def test_search(client):
    r = client.get("/api/search", params={"q": "transformer"})
    assert r.status_code == 200
    body = r.json()
    assert [x["run_id"] for x in body["reports"]] == ["run_a"]

    r = client.get("/api/search", params={"q": "summary"})
    body = r.json()
    assert len(body["artifacts"]) >= 1
    assert body["artifacts"][0]["artifact_id"] == "art_b"

    assert client.get("/api/search", params={"q": ""}).status_code == 422


def test_stats(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    assert r.json() == {"reports": 2, "artifacts": 2, "tags": 2}


def test_index_and_static_assets(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Deep Research Library" in r.text
    assert 'src="/static/js/app.js"' in r.text

    r = client.get("/static/js/app.js")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] in (
        "application/javascript",
        "text/javascript",
    )
    r = client.get("/static/styles.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]

    assert client.get("/static/missing.js").status_code == 404


def test_security_headers(client):
    r = client.get("/api/health")
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("referrer-policy") == "no-referrer"


def test_list_artifacts(client):
    r = client.get("/api/artifacts")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    ids = [x["artifact_id"] for x in body["items"]]
    assert set(ids) == {"art_a", "art_b"}

    by_kind = client.get("/api/artifacts", params={"kind": "pdf"}).json()
    assert by_kind["total"] == 2

    by_q = client.get("/api/artifacts", params={"q": "Paper B"}).json()
    assert by_q["total"] == 1
    assert by_q["items"][0]["artifact_id"] == "art_b"

    detail = by_q["items"][0]
    assert detail["arxiv_id"] == "2401.00001"
    assert detail["relevance_score"] is None  # seeded analysis has no score


def test_delete_artifact_requires_confirmation(client):
    r = client.delete("/api/artifacts/art_b")
    assert r.status_code == 400
    # Confirm required even for a valid artifact.
    r = client.delete("/api/artifacts/art_b", params={"confirm": "false"})
    assert r.status_code == 400
    assert client.get("/api/artifacts/art_b").status_code == 200


def test_delete_artifact_refuses_report_output(client):
    r = client.delete("/api/artifacts/art_a", params={"confirm": "true"})
    assert r.status_code == 409
    assert "delete those report(s) instead" in r.json()["detail"]
    # Report's own artifact is untouched.
    assert client.get("/api/artifacts/art_a").status_code == 200


def test_delete_artifact_removes_file_and_rows(client):
    assert (
        Path(client.app.state.config.pdl.root_dir) / "artifacts" / "pdf" / "art_b.pdf"
    ).is_file()
    r = client.delete("/api/artifacts/art_b", params={"confirm": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert any(p.endswith("art_b.pdf") for p in body["removed_files"])
    # Artifact + its analysis + FTS row are gone.
    assert client.get("/api/artifacts/art_b").status_code == 404
    # The referencing report still exists but its citation no longer resolves
    # to a local copy.
    report = client.get("/api/reports/run_a").json()
    ref = report["citations"][0]
    assert ref["arxiv_id"] == "2401.00001"
    assert "local_pdf_url" not in ref
    assert "local_artifact_id" not in ref


def test_delete_report_cleans_up_report_artifact(client):
    """Deleting a report also removes its own (report) artifact + file, since
    no other report references it."""
    root = Path(client.app.state.config.pdl.root_dir)
    assert (root / "artifacts" / "pdf" / "art_a.pdf").is_file()
    r = client.delete("/api/reports/run_a", params={"confirm": "true"})
    assert r.status_code == 200
    assert any(p.endswith("art_a.pdf") for p in r.json()["removed_files"])
    assert client.get("/api/artifacts/art_a").status_code == 404


def test_report_detail_citations_carry_local_artifact_id(client):
    report = client.get("/api/reports/run_a").json()
    ref = report["citations"][0]
    assert ref["local_artifact_id"] == "art_b"
    assert ref["local_artifact_kind"] == "pdf"
    assert ref["local_pdf_url"].endswith("/api/artifacts/art_b/pdf")


def test_artifact_detail_includes_relevance_score(client):
    # Seed an analysis with a relevance score for art_b, then re-check.
    # (art_b's existing analysis has no score; that's fine — field present.)
    body = client.get("/api/artifacts/art_b").json()
    assert "relevance_score" in body["analyses"][0]
    assert body["analyses"][0]["relevance_score"] is None


def test_delete_reference_requires_confirmation(client):
    r = client.request(
        "DELETE",
        "/api/reports/run_a/references",
        json={"url": "https://arxiv.org/abs/2401.00001"},
    )
    assert r.status_code == 400
    # Report is untouched.
    report = client.get("/api/reports/run_a").json()
    assert len(report["citations"]) == 1


def test_delete_reference_not_found(client):
    r = client.request(
        "DELETE",
        "/api/reports/run_a/references",
        params={"confirm": "true"},
        json={"url": "https://example.com/missing"},
    )
    assert r.status_code == 404
    assert client.get("/api/reports/run_a").status_code == 200


def test_delete_reference_removes_citation_and_cleans_bib(client):
    """Deleting a reference removes it from citations_json, regenerates the
    markdown bibliography, updates the .bib export, and removes the archived
    artifact when no other report still cites it."""
    root = Path(client.app.state.config.pdl.root_dir)
    assert (root / "artifacts" / "pdf" / "art_b.pdf").is_file()

    r = client.request(
        "DELETE",
        "/api/reports/run_a/references",
        params={"confirm": "true"},
        json={"url": "https://arxiv.org/abs/2401.00001"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "run_a"
    assert body["citations"] == []
    assert "## Bibliography" not in body["markdown"]

    # .bib export is empty now (no @misc entries).
    bib = client.get("/api/reports/run_a/bibliography/bib").text
    assert "@misc" not in bib
    assert "Paper B" not in bib

    # The archived artifact (PDF + analysis) is gone — no other report cites it.
    assert client.get("/api/artifacts/art_b").status_code == 404
    assert not (root / "artifacts" / "pdf" / "art_b.pdf").exists()


async def test_delete_reference_keeps_shared_artifact(client, seeded_config: Path):
    """When another report still cites the artifact, deleting the reference
    from one report keeps the archived copy (only the citation is removed)."""
    from deep_research.library.storage.rows import ReportRow
    from deep_research.library.storage.sqlite_backend import SqliteStorageBackend

    backend = SqliteStorageBackend(str(seeded_config.parent / "index.db"))
    await backend.connect()
    try:
        await backend.insert_report(
            ReportRow(
                run_id="run_c",
                started_at=_now(3),
                completed_at=_now(3),
                original_query="Another paper citing B",
                path_taken="quick",
                markdown="# Report C",
                citations_json=json.dumps(
                    [
                        {
                            "url": "https://arxiv.org/abs/2401.00001",
                            "title": "Paper B",
                            "arxiv_id": "2401.00001",
                        }
                    ]
                ),
            )
        )
    finally:
        await backend.close()

    r = client.request(
        "DELETE",
        "/api/reports/run_a/references",
        params={"confirm": "true"},
        json={"url": "https://arxiv.org/abs/2401.00001"},
    )
    assert r.status_code == 200
    # run_a lost its reference…
    report = client.get("/api/reports/run_a").json()
    assert report["citations"] == []
    # …but the shared artifact is kept.
    assert client.get("/api/artifacts/art_b").status_code == 200
