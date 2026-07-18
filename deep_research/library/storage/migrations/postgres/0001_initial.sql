-- 0001_initial: schema_meta, artifacts, reports, analyses, citation_edges, tags, FTS indices

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id      TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    source_url       TEXT,
    source_type      TEXT,
    title            TEXT,
    authors          TEXT,
    discovered_by    TEXT,
    arxiv_id         TEXT,
    parents          TEXT,
    bytes_path       TEXT NOT NULL,
    bytes_size       INTEGER,
    first_seen_at    TEXT NOT NULL,
    last_touched_at  TEXT NOT NULL,
    raw_metadata     TEXT,
    refresh_after_at        TEXT,
    last_refreshed_at       TEXT,
    upstream_unchanged_since TEXT,
    CHECK (kind IN ('pdf', 'html', 'report'))
);

CREATE TABLE IF NOT EXISTS reports (
    run_id             TEXT PRIMARY KEY,
    started_at         TEXT NOT NULL,
    completed_at       TEXT,
    original_query     TEXT NOT NULL,
    path_taken         TEXT NOT NULL,
    classifier_rationale TEXT,
    iterations         INTEGER,
    config_snapshot    TEXT,
    markdown           TEXT NOT NULL,
    artifact_id        TEXT REFERENCES artifacts(artifact_id),
    citations_json     TEXT,
    classifier_json    TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id       TEXT PRIMARY KEY,
    artifact_id       TEXT NOT NULL REFERENCES artifacts(artifact_id),
    run_id            TEXT NOT NULL REFERENCES reports(run_id),
    analyzer          TEXT NOT NULL,
    summary           TEXT,
    key_findings      TEXT,
    methodology       TEXT,
    limitations       TEXT,
    gaps              TEXT,
    follow_ups        TEXT,
    key_references    TEXT,
    relevance_to_query TEXT,
    analyzed_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS citation_edges (
    source_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    target_artifact_id TEXT REFERENCES artifacts(artifact_id),
    target_arxiv_id    TEXT,
    rationale          TEXT,
    weight             REAL DEFAULT 0.5,
    discovered_in_run  TEXT REFERENCES reports(run_id),
    PRIMARY KEY (source_artifact_id, target_arxiv_id)
);

CREATE TABLE IF NOT EXISTS tags (
    tag              TEXT NOT NULL,
    artifact_id      TEXT NOT NULL REFERENCES artifacts(artifact_id),
    applied_in_run   TEXT REFERENCES reports(run_id),
    PRIMARY KEY (tag, artifact_id)
);

-- Postgres FTS via tsvector (no FTS5 — that's SQLite-specific)
CREATE INDEX IF NOT EXISTS idx_analyses_summary ON analyses USING gin(to_tsvector('english', coalesce(summary, '')));
CREATE INDEX IF NOT EXISTS idx_analyses_key_findings ON analyses USING gin(to_tsvector('english', coalesce(key_findings, '')));

-- Record schema version
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')
ON CONFLICT (key) DO UPDATE SET value = '1';
