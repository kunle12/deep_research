"""Tests for library CLI (P12.0)."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest
from typer.testing import CliRunner

from deep_research.library.cli import library_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_pdl_config(tmp: str, enabled: bool = True) -> str:
    """Write a minimal config.yaml with PDL settings and return the path."""
    cfg_path = os.path.join(tmp, "config.yaml")
    root_dir = os.path.join(tmp, "pdl_root")
    os.makedirs(root_dir, exist_ok=True)
    with open(cfg_path, "w") as f:
        f.write(f"""
pdl:
  enabled: {str(enabled).lower()}
  root_dir: {root_dir}
""")
    return cfg_path


def _seed_db(tmp: str) -> str:
    """Create a seeded SQLite database at tmp/pdl_root/index.db and return root_dir."""
    root_dir = os.path.join(tmp, "pdl_root")
    os.makedirs(root_dir, exist_ok=True)
    db_path = os.path.join(root_dir, "index.db")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY, kind TEXT, source_url TEXT,
            source_type TEXT, title TEXT, authors TEXT, discovered_by TEXT,
            arxiv_id TEXT, parents TEXT, bytes_path TEXT, bytes_size INTEGER,
            first_seen_at TEXT, last_touched_at TEXT, raw_metadata TEXT,
            refresh_after_at TEXT, last_refreshed_at TEXT, upstream_unchanged_since TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            run_id TEXT PRIMARY KEY, started_at TEXT, completed_at TEXT,
            original_query TEXT, path_taken TEXT, classifier_rationale TEXT,
            iterations INTEGER, config_snapshot TEXT, markdown TEXT,
            artifact_id TEXT, citations_json TEXT, classifier_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            tag TEXT NOT NULL, artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
            applied_in_run TEXT REFERENCES reports(run_id),
            PRIMARY KEY (tag, artifact_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            analysis_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
            run_id TEXT NOT NULL REFERENCES reports(run_id), analyzer TEXT NOT NULL,
            summary TEXT, key_findings TEXT, methodology TEXT, limitations TEXT,
            gaps TEXT, follow_ups TEXT, key_references TEXT, relevance_to_query TEXT,
            analyzed_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS glossary (
            term_id INTEGER PRIMARY KEY AUTOINCREMENT, term TEXT NOT NULL,
            term_canonical TEXT NOT NULL, kind TEXT NOT NULL, short_def TEXT,
            long_def TEXT, acronym_expansion TEXT, related_terms TEXT,
            domain_tags TEXT, confidence REAL, first_seen_run_id TEXT,
            first_seen_artifact_id TEXT, last_updated TEXT NOT NULL
        )
    """)

    # Seed data
    conn.execute("""
        INSERT INTO artifacts (artifact_id, kind, source_url, source_type, title,
            bytes_path, bytes_size, first_seen_at, last_touched_at)
        VALUES ('art1', 'pdf', 'https://example.com/paper', 'arxiv', 'Test Paper',
            'artifacts/pdf/art1.pdf', 1024, '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')
    """)
    conn.execute("""
        INSERT INTO reports (run_id, started_at, completed_at, original_query,
            path_taken, markdown, artifact_id)
        VALUES ('run001', '2024-06-15T10:30:00', '2024-06-15T11:00:00',
            'test query about transformers', 'deep', '# Report', 'art1')
    """)
    conn.execute("""
        INSERT INTO reports (run_id, started_at, completed_at, original_query,
            path_taken, markdown, artifact_id)
        VALUES ('run002', '2024-06-15T12:00:00', '2024-06-15T12:30:00',
            'another test query', 'quick', '# Report 2', 'art1')
    """)
    conn.execute("""
        INSERT INTO tags (tag, artifact_id, applied_in_run)
        VALUES ('transformer', 'art1', 'run001')
    """)
    conn.execute("""
        INSERT INTO tags (tag, artifact_id, applied_in_run)
        VALUES ('deep', 'art1', 'run001')
    """)
    conn.execute("""
        INSERT INTO glossary (term, term_canonical, kind, short_def, confidence, last_updated)
        VALUES ('Transformer', 'transformer', 'concept', 'A neural network architecture', 0.9, '2024-01-01T00:00:00Z')
    """)
    conn.commit()
    conn.close()
    return root_dir


# -- PDL-disabled error tests --


def test_cli_ls_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["ls", "--config", cfg])
    assert result.exit_code != 0


def test_cli_find_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["find", "test", "--config", cfg])
    assert result.exit_code != 0


def test_cli_show_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["show", "art1", "--config", cfg])
    assert result.exit_code != 0


def test_cli_stats_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["stats", "--config", cfg])
    assert result.exit_code != 0


def test_cli_delete_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["delete", "run001", "--config", cfg])
    assert result.exit_code != 0


def test_cli_prune_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["prune", "--config", cfg])
    assert result.exit_code != 0


def test_cli_export_bibtex_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["export-bibtex", "--config", cfg])
    assert result.exit_code != 0


def test_cli_glossary_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["glossary", "--config", cfg])
    assert result.exit_code != 0


def test_cli_refresh_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["refresh", "--config", cfg])
    assert result.exit_code != 0


def test_cli_tag_no_pdl(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=False)
    result = runner.invoke(library_app, ["tag", "art1", "newtag", "--config", cfg])
    assert result.exit_code != 0


# -- PDL-enabled tests with seeded DB --


def test_cli_ls_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["ls", "--config", cfg])
    assert result.exit_code == 0
    assert "run001" in result.stdout
    assert "run002" in result.stdout
    assert "test query" in result.stdout
    assert "transformer" in result.stdout or "deep" in result.stdout  # tags shown


def test_cli_find_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["find", "transformer", "--config", cfg])
    assert result.exit_code == 0
    # FTS may return no results for short seeded text, but command should succeed


def test_cli_show_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["show", "art1", "--config", cfg])
    assert result.exit_code == 0
    assert "art1" in result.stdout
    assert "Test Paper" in result.stdout or "pdf" in result.stdout


def test_cli_stats_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["stats", "--config", cfg])
    assert result.exit_code == 0


def test_cli_delete_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["delete", "run001", "--config", cfg])
    assert result.exit_code == 0
    assert "Deleting report" in result.stdout


def test_cli_delete_ambiguous(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    # Both run001 and run002 start with "run" — should show ambiguity
    result = runner.invoke(library_app, ["delete", "run", "--config", cfg])
    assert result.exit_code == 0
    assert "Multiple reports match" in result.stdout


def test_cli_delete_not_found(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["delete", "nonexistent", "--config", cfg])
    assert result.exit_code == 0
    assert "No report found" in result.stdout


def test_cli_prune_dry_run(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["prune", "--older-than", "1", "--config", cfg])
    assert result.exit_code == 0
    assert "Dry run" in result.stdout


def test_cli_export_bibtex_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    out = os.path.join(tmp, "refs.bib")
    result = runner.invoke(library_app, ["export-bibtex", "--out", out, "--config", cfg])
    assert result.exit_code == 0
    assert os.path.exists(out)
    with open(out) as f:
        content = f.read()
    assert "run001" in content or "test query" in content


def test_cli_glossary_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["glossary", "--config", cfg])
    assert result.exit_code == 0
    assert "Transformer" in result.stdout


def test_cli_glossary_find(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["glossary", "--find", "transformer", "--config", cfg])
    assert result.exit_code == 0


def test_cli_glossary_filter_tag(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["glossary", "--filter-tag", "nlp", "--config", cfg])
    assert result.exit_code == 0


def test_cli_glossary_export(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    out = os.path.join(tmp, "glossary.json")
    result = runner.invoke(library_app, ["glossary", "--out", out, "--config", cfg])
    assert result.exit_code == 0
    assert os.path.exists(out)


def test_cli_refresh_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["refresh", "--dry-run", "--once", "--config", cfg])
    assert result.exit_code == 0


def test_cli_tag_add_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["tag", "art1", "newtag", "--config", cfg])
    assert result.exit_code == 0
    assert "Tagged" in result.stdout


def test_cli_tag_list_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["tag", "--list", "art1", "--config", cfg])
    assert result.exit_code == 0
    assert "transformer" in result.stdout


def test_cli_tag_remove_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["tag", "--remove", "art1", "transformer", "--config", cfg])
    assert result.exit_code == 0
    assert "Removed tag" in result.stdout


def test_cli_tag_rename_with_data(runner):
    tmp = tempfile.mkdtemp()
    _seed_db(tmp)
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(
        library_app, ["tag", "--rename-old", "transformer", "--rename-new", "tf", "--config", cfg]
    )
    assert result.exit_code == 0
    assert "Renamed tag" in result.stdout


def test_cli_tag_usage(runner):
    tmp = tempfile.mkdtemp()
    cfg = _make_pdl_config(tmp, enabled=True)
    result = runner.invoke(library_app, ["tag", "--config", cfg])
    assert result.exit_code == 0
    assert "Usage" in result.stdout
