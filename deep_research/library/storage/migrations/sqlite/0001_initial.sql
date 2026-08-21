-- Consolidated initial schema: all tables (artifacts, reports, analyses,
-- citation_edges, tags, glossary, artifact_versions, refresh_jobs, FTS indices)

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
    CHECK (kind IN ('pdf', 'html', 'report', 'image'))
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
    artifact_id        TEXT,
    citations_json     TEXT,
    classifier_json    TEXT,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)
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
    analyzed_at       TEXT NOT NULL,
    relevance_score   REAL
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

CREATE TABLE IF NOT EXISTS glossary (
    term_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    term         TEXT NOT NULL,
    term_canonical TEXT NOT NULL,
    kind         TEXT NOT NULL,
    short_def    TEXT,
    long_def     TEXT,
    acronym_expansion TEXT,
    related_terms TEXT,
    domain_tags  TEXT,
    confidence   REAL,
    first_seen_run_id TEXT REFERENCES reports(run_id),
    first_seen_artifact_id TEXT REFERENCES artifacts(artifact_id),
    last_updated TEXT NOT NULL,
    UNIQUE(term_canonical),
    CHECK (kind IN ('concept','acronym','method','metric','dataset','model','tool'))
);

CREATE TABLE IF NOT EXISTS artifact_versions (
    artifact_id_old TEXT NOT NULL REFERENCES artifacts(artifact_id),
    artifact_id_new TEXT NOT NULL REFERENCES artifacts(artifact_id),
    reason          TEXT NOT NULL,
    discovered_at   TEXT NOT NULL,
    discovered_in_run TEXT REFERENCES reports(run_id),
    PRIMARY KEY (artifact_id_old, artifact_id_new)
);

CREATE TABLE IF NOT EXISTS refresh_jobs (
    job_id              TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    scope_kind          TEXT NOT NULL,
    scope_value         TEXT NOT NULL,
    artifacts_considered INTEGER,
    artifacts_refreshed   INTEGER,
    status              TEXT NOT NULL,
    error               TEXT
);

-- FTS5 virtual tables (internal content — populated via code in insert_analysis)
-- analysis_id is UNINDEXED so it can be used as a join key without being
-- tokenized. We delete-by-rowid during upserts to keep the index in sync.
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    analysis_id UNINDEXED,
    summary,
    key_findings,
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS glossary_fts USING fts5(
    term, short_def, long_def, related_terms,
    content='glossary', content_rowid='term_id',
    tokenize='porter unicode61'
);

-- Keep the content-backed glossary_fts index in sync with the glossary table.
-- The canonical FTS5 external-content triggers: delete on row removal and
-- insert/update on row write. term_id is preserved across updates because
-- upserts use ON CONFLICT DO UPDATE (never INSERT OR REPLACE), so the rowid
-- linkage stays valid.
CREATE TRIGGER IF NOT EXISTS glossary_ai AFTER INSERT ON glossary BEGIN
  INSERT INTO glossary_fts(rowid, term, short_def, long_def, related_terms)
  VALUES (new.term_id, new.term, new.short_def, new.long_def, new.related_terms);
END;
CREATE TRIGGER IF NOT EXISTS glossary_ad AFTER DELETE ON glossary BEGIN
  INSERT INTO glossary_fts(glossary_fts, rowid, term, short_def, long_def, related_terms)
  VALUES ('delete', old.term_id, old.term, old.short_def, old.long_def, old.related_terms);
END;
CREATE TRIGGER IF NOT EXISTS glossary_au AFTER UPDATE ON glossary BEGIN
  INSERT INTO glossary_fts(glossary_fts, rowid, term, short_def, long_def, related_terms)
  VALUES ('delete', old.term_id, old.term, old.short_def, old.long_def, old.related_terms);
  INSERT INTO glossary_fts(rowid, term, short_def, long_def, related_terms)
  VALUES (new.term_id, new.term, new.short_def, new.long_def, new.related_terms);
END;

-- Record schema version
INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '1');
