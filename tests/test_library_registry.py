"""LibraryRegistry + legacy library migration tests (multi_user layout)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_research.accounts.bootstrap import migrate_legacy_library
from deep_research.accounts.rows import UserRow
from deep_research.accounts.store import AccountStore
from deep_research.config import AgentTopConfig, PDLAuthConfig
from deep_research.library.storage.postgres_backend import PostgresStorageBackend
from deep_research.library.storage.registry import (
    LibraryRegistry,
    StaticLibraryRegistry,
    validate_user_id,
)
from deep_research.library.storage.rows import ReportRow
from deep_research.library.storage.sqlite_backend import SqliteStorageBackend


def _multi_cfg(root: Path) -> AgentTopConfig:
    return AgentTopConfig(
        pdl={
            "root_dir": str(root),
            "storage": {"backend": "sqlite"},
            "auth": PDLAuthConfig(mode="multi_user", owner_email="wang.xun@gmail.com"),
        }
    )


def _legacy_cfg(root: Path) -> AgentTopConfig:
    return AgentTopConfig(pdl={"root_dir": str(root), "storage": {"backend": "sqlite"}})


@pytest.fixture
async def registry(tmp_path):
    cfg = _multi_cfg(tmp_path / "lib")
    reg = LibraryRegistry(cfg)
    try:
        yield reg
    finally:
        await reg.close_all()


async def test_per_user_backends_are_isolated(registry, tmp_path):
    uid_a = "a" * 32
    uid_b = "b" * 32
    backend_a = await registry.backend_for(uid_a)
    backend_b = await registry.backend_for(uid_b)
    assert backend_a is not backend_b

    await backend_a.insert_report(
        ReportRow(
            run_id="run_a",
            original_query="A's research",
            markdown="# A",
            path_taken="quick",
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    assert await backend_b.get_report("run_a") is None
    assert (await backend_b.list_reports(limit=10)) == []

    # Same instance is returned on repeat (cached).
    assert await registry.backend_for(uid_a) is backend_a


async def test_root_for_layout(registry, tmp_path):
    root = tmp_path / "lib"
    assert registry.root_for("a" * 32) == root / "users" / ("a" * 32)
    assert not registry.has_library("a" * 32)


@pytest.mark.parametrize("bad", ["", "../evil", "a/b", "A" * 32, "short", "z" * 33])
async def test_invalid_user_id_rejected(registry, bad):
    with pytest.raises(ValueError):
        validate_user_id(bad)
    with pytest.raises(ValueError):
        await registry.backend_for(bad)
    with pytest.raises(ValueError):
        registry.root_for(bad)


async def test_user_ids_listing(registry, tmp_path):
    await registry.backend_for("a" * 32)
    await registry.backend_for("b" * 32)
    (tmp_path / "lib" / "users" / "not-a-uuid").mkdir()  # ignored
    (tmp_path / "lib" / "users" / ("C" * 32 + ".txt")).write_text("x")  # ignored
    ids = registry.user_ids()
    assert ids == sorted(["a" * 32, "b" * 32])


async def test_static_registry_wraps_single_backend(tmp_path):
    backend = SqliteStorageBackend(str(tmp_path / "legacy" / "index.db"))
    await backend.connect()
    reg = StaticLibraryRegistry(backend, tmp_path / "legacy")
    assert await reg.backend_for("whatever") is backend
    assert reg.root_for("whatever") == tmp_path / "legacy"
    await reg.close_all()


async def test_postgres_schema_name_validation():
    with pytest.raises(ValueError):
        PostgresStorageBackend(dsn="postgres://x", schema='lib"; DROP SCHEMA x')
    with pytest.raises(ValueError):
        PostgresStorageBackend(dsn="postgres://x", schema="lib space")
    # uuid-hex derived names are accepted (no connection attempted).
    backend = PostgresStorageBackend(dsn="postgres://x", schema="library_" + "a" * 32)
    assert backend._schema == "library_" + "a" * 32


# -- Legacy migration (sqlite) --


@pytest.fixture
async def store(tmp_path):
    s = AccountStore(tmp_path / "registry.db")
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


def _owner() -> UserRow:
    return UserRow(user_id="c" * 32, email="wang.xun@gmail.com", status="active")


async def test_migration_moves_legacy_layout(tmp_path, store):
    root = tmp_path / "lib"
    (root / "artifacts" / "pdf").mkdir(parents=True)
    (root / "reports" / "2026" / "01" / "01").mkdir(parents=True)
    (root / "index.db").write_bytes(b"sqlite")
    (root / "artifacts" / "pdf" / "x.pdf").write_bytes(b"pdf")
    (root / "reports" / "2026" / "01" / "01" / "run.md").write_text("# hi")

    cfg = _multi_cfg(root)
    owner = _owner()
    moved = await migrate_legacy_library(cfg, store, owner)
    assert moved is True

    user_dir = root / "users" / owner.user_id
    assert (user_dir / "index.db").exists()
    assert (user_dir / "artifacts" / "pdf" / "x.pdf").exists()
    assert (user_dir / "reports" / "2026" / "01" / "01" / "run.md").exists()
    assert not (root / "index.db").exists()
    assert await store.meta_get("legacy_migrated") == owner.user_id

    # Idempotent: a second run does nothing.
    assert await migrate_legacy_library(cfg, store, owner) is False
    assert (user_dir / "index.db").exists()


async def test_migration_fresh_install_sets_marker(tmp_path, store):
    root = tmp_path / "lib"
    root.mkdir()
    cfg = _multi_cfg(root)
    assert await migrate_legacy_library(cfg, store, _owner()) is False
    assert await store.meta_get("legacy_migrated") is not None


async def test_migration_resume_after_partial_move(tmp_path, store):
    root = tmp_path / "lib"
    owner = _owner()
    user_dir = root / "users" / owner.user_id
    # Simulate a crash after index.db was moved but before the marker.
    (user_dir / "index.db").parent.mkdir(parents=True)
    (user_dir / "index.db").write_bytes(b"sqlite")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "late.pdf").write_bytes(b"pdf")

    cfg = _multi_cfg(root)
    assert await migrate_legacy_library(cfg, store, owner) is True
    assert (user_dir / "artifacts" / "late.pdf").exists()
    assert not (root / "artifacts").exists()
    assert await store.meta_get("legacy_migrated") == owner.user_id


async def test_migration_refuses_ambiguous_state(tmp_path, store):
    root = tmp_path / "lib"
    owner = _owner()
    (root / "users" / owner.user_id).mkdir(parents=True)
    (root / "users" / owner.user_id / "index.db").write_bytes(b"new")
    (root / "index.db").write_bytes(b"old")

    cfg = _multi_cfg(root)
    with pytest.raises(RuntimeError, match="ambiguous"):
        await migrate_legacy_library(cfg, store, owner)


async def test_migration_ignores_unrelated_files(tmp_path, store):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "index.db").write_bytes(b"sqlite")
    (root / "notes.txt").write_text("keep me")
    (root / "registry.db").write_bytes(b"registry")

    cfg = _multi_cfg(root)
    owner = _owner()
    await migrate_legacy_library(cfg, store, owner)
    assert (root / "notes.txt").exists()
    assert (root / "registry.db").exists()
