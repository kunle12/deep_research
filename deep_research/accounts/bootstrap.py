"""Account bootstrap + one-time legacy library migration.

``migrate_legacy_library`` moves a pre-multi-user library (``<root>/index.db``
+ ``artifacts/`` + ``reports/``) into the owner's per-user directory
(``<root>/users/<owner_id>/``) the first time ``multi_user`` mode starts. It
is idempotent, resumable after a partial move, and never touches anything
once the completion marker is set in the registry.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from deep_research.accounts.rows import UserRow
from deep_research.accounts.store import AccountStore
from deep_research.config import AgentTopConfig

logger = logging.getLogger(__name__)

#: registry_meta key recording a completed legacy migration.
_MIGRATION_MARKER = "legacy_migrated"

# Top-level entries of a legacy library root that belong to the library
# itself (everything else — e.g. registry.db, users/ — stays put).
_LEGACY_ENTRIES = ("index.db", "index.db-wal", "index.db-shm", "artifacts", "reports")

_POSTGRES_LEGACY_TABLES = (
    "schema_meta",
    "artifacts",
    "reports",
    "analyses",
    "citation_edges",
    "tags",
    "glossary",
    "artifact_versions",
    "refresh_jobs",
)


async def migrate_legacy_library(cfg: AgentTopConfig, store: AccountStore, owner: UserRow) -> bool:
    """Migrate the legacy single-user library into the owner's directory.

    Returns True when a migration was performed in this call. Safe to call on
    every multi_user startup — it no-ops once the marker is set and on fresh
    installations with no legacy layout.
    """
    if await store.meta_get(_MIGRATION_MARKER):
        return False
    if cfg.pdl.storage.backend == "postgres":
        return await _migrate_postgres_legacy(cfg, store, owner)
    return await _migrate_sqlite_legacy(cfg, store, owner)


async def _migrate_sqlite_legacy(cfg: AgentTopConfig, store: AccountStore, owner: UserRow) -> bool:
    root = Path(cfg.pdl.root_dir)
    legacy_db = root / "index.db"
    user_dir = root / "users" / owner.user_id
    user_db = user_dir / "index.db"

    if not legacy_db.exists() and not user_db.exists():
        # Fresh installation — nothing to migrate, but record that we looked
        # so a stray root/index.db created *after* first start is never
        # silently adopted later.
        await store.meta_set(_MIGRATION_MARKER, owner.user_id)
        return False

    if legacy_db.exists() and user_db.exists():
        raise RuntimeError(
            "multi_user migration ambiguous: both legacy <root>/index.db and "
            f"users/{owner.user_id}/index.db exist; resolve manually and re-run"
        )

    if user_db.exists():
        # A previous attempt moved index.db but crashed before setting the
        # marker — finish moving any remaining artifacts/reports.
        _move_remaining(root, user_dir)
        await store.meta_set(_MIGRATION_MARKER, owner.user_id)
        logger.info("completed legacy library migration resume for %s", owner.email)
        return True

    # Full legacy layout present: move it into the owner's directory.
    user_dir.mkdir(parents=True, exist_ok=True)
    for name in _LEGACY_ENTRIES:
        src = root / name
        if not src.exists():
            continue
        dst = user_dir / name
        if dst.exists():
            raise RuntimeError(f"migration collision: {dst} already exists; resolve manually")
        shutil.move(str(src), str(dst))
    await store.meta_set(_MIGRATION_MARKER, owner.user_id)
    logger.info("migrated legacy library at %s into %s (owner %s)", root, user_dir, owner.email)
    return True


def _move_remaining(root: Path, user_dir: Path) -> None:
    for name in _LEGACY_ENTRIES:
        src = root / name
        if not src.exists():
            continue
        dst = user_dir / name
        if dst.exists():
            continue  # already moved by the earlier attempt
        shutil.move(str(src), str(dst))


async def _migrate_postgres_legacy(
    cfg: AgentTopConfig, store: AccountStore, owner: UserRow
) -> bool:
    import os

    import asyncpg

    dsn = os.environ.get(cfg.pdl.storage.postgres_dsn_env, "")
    if not dsn:
        raise ValueError(f"Postgres DSN not set. Set {cfg.pdl.storage.postgres_dsn_env} env var.")
    target_schema = f"library_{owner.user_id}"
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            # Detect a legacy library: any known table still living in public.
            rows = await conn.fetch(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename = ANY($1::text[])
                """,
                list(_POSTGRES_LEGACY_TABLES),
            )
            if not rows:
                await store.meta_set(_MIGRATION_MARKER, owner.user_id)
                return False
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{target_schema}"')
            for row in rows:
                table = row["tablename"]
                await conn.execute(f'ALTER TABLE public."{table}" SET SCHEMA "{target_schema}"')
            await store.meta_set(_MIGRATION_MARKER, owner.user_id)
        logger.info(
            "migrated legacy Postgres library (public schema) into %s for %s",
            target_schema,
            owner.email,
        )
        return True
    finally:
        await conn.close()
