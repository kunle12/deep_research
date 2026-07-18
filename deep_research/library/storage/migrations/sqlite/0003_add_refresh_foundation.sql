-- 0003_add_refresh_foundation: artifact_versions + refresh_jobs tables

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

-- Update schema version
INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '3');
