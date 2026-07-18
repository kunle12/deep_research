-- 0002_add_glossary: glossary + glossary_fts tables

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

CREATE VIRTUAL TABLE IF NOT EXISTS glossary_fts USING fts5(
    term, short_def, long_def, related_terms,
    content='glossary', content_rowid='term_id',
    tokenize='porter unicode61'
);

-- Update schema version
INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '2');
