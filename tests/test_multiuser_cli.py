"""Multi-user CLI tests: users admin subcommands, --user library targeting,
scheduler per-user iteration."""

from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from deep_research.accounts.passwords import hash_password
from deep_research.accounts.store import AccountStore
from deep_research.cli.app import app as research_app
from deep_research.config import AgentTopConfig, PDLAuthConfig
from deep_research.library.cli import library_app
from deep_research.library.storage.rows import ReportRow
from deep_research.library.storage.sqlite_backend import SqliteStorageBackend
from deep_research.scheduler import _user_library_configs

runner = CliRunner()


def _multi_config(root: Path, *, owner_email: str = "wang.xun@gmail.com") -> Path:
    cfg = root / "config.yaml"
    cfg.write_text(
        "pdl:\n"
        "  enabled: true\n"
        f"  root_dir: {root}\n"
        "  storage:\n"
        "    backend: sqlite\n"
        "  auth:\n"
        "    mode: multi_user\n"
        f"    owner_email: {owner_email}\n",
        encoding="utf-8",
    )
    return cfg


def test_users_add_list_set_status(tmp_path):
    cfg = _multi_config(tmp_path)
    r = runner.invoke(
        library_app,
        ["users", "add", "Alice@Example.com", "--display-name", "Alice", "--config", str(cfg)],
    )
    assert r.exit_code == 0, r.output
    assert "Created account alice@example.com" in r.output

    # Duplicate add fails cleanly.
    r2 = runner.invoke(library_app, ["users", "add", "alice@example.com", "--config", str(cfg)])
    assert r2.exit_code == 1
    assert "already exists" in r2.output

    # Invalid email fails cleanly.
    r3 = runner.invoke(library_app, ["users", "add", "not-an-email", "--config", str(cfg)])
    assert r3.exit_code == 1

    r4 = runner.invoke(library_app, ["users", "list", "--config", str(cfg)])
    assert r4.exit_code == 0
    assert "alice@example.com" in r4.output
    assert "active" in r4.output
    assert "--" in r4.output  # no password set

    r5 = runner.invoke(
        library_app, ["users", "set-status", "alice@example.com", "disabled", "--config", str(cfg)]
    )
    assert r5.exit_code == 0
    r6 = runner.invoke(library_app, ["users", "list", "--config", str(cfg)])
    assert "disabled" in r6.output

    # Bad status rejected.
    r7 = runner.invoke(
        library_app, ["users", "set-status", "alice@example.com", "bogus", "--config", str(cfg)]
    )
    assert r7.exit_code == 1


def test_users_set_password_env(tmp_path, monkeypatch):
    cfg = _multi_config(tmp_path)
    runner.invoke(library_app, ["users", "add", "a@example.com", "--config", str(cfg)])
    monkeypatch.setenv("TEST_PW", "super secret pw")
    r = runner.invoke(
        library_app,
        [
            "users",
            "set-password",
            "a@example.com",
            "--password-env",
            "TEST_PW",
            "--config",
            str(cfg),
        ],
    )
    assert r.exit_code == 0, r.output

    async def _check():
        store = AccountStore(tmp_path / "registry.db")
        await store.connect()
        try:
            user = await store.get_user_by_email("a@example.com")
            return user.password_hash is not None
        finally:
            await store.close()

    assert asyncio.run(_check()) is True


def test_library_ls_targets_user_library(tmp_path):
    """--user picks that account's per-user library; default picks the owner's."""
    cfg = _multi_config(tmp_path)

    # Create accounts with passwords (owner has one; alice does too).
    async def _mk_accounts():
        store = AccountStore(tmp_path / "registry.db")
        await store.connect()
        try:
            from deep_research.accounts.emails import ConsoleEmailSender
            from deep_research.accounts.service import AuthService, normalize_email
            from deep_research.config import PDLAuthConfig

            auth = AuthService(store, ConsoleEmailSender(), PDLAuthConfig())
            owner = await auth.provision_owner("wang.xun@gmail.com")
            await store.set_password(owner.user_id, hash_password("owner password 1"))
            alice = await store.create_user(normalize_email("alice@example.com"), status="active")
            await store.set_password(alice.user_id, hash_password("alice password 1"))
            return owner, alice
        finally:
            await store.close()

    owner, alice = asyncio.run(_mk_accounts())

    # Seed ONE report into ALICE's library only.
    async def _seed():
        backend = SqliteStorageBackend(str(tmp_path / "users" / alice.user_id / "index.db"))
        await backend.connect()
        try:
            await backend.insert_report(
                ReportRow(
                    run_id="alice_run",
                    started_at="2026-01-01T00:00:00+00:00",
                    original_query="Alice's research",
                    path_taken="quick",
                    markdown="# Alice",
                )
            )
        finally:
            await backend.close()

    asyncio.run(_seed())

    # Owner default: sees an empty library (their own).
    r = runner.invoke(library_app, ["ls", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    assert "alice_run" not in r.output
    assert "using the owner's library" in r.output

    # Explicit --user alice: sees her report.
    r2 = runner.invoke(
        library_app,
        ["--user", "alice@example.com", "ls", "--config", str(cfg)],
    )
    assert r2.exit_code == 0, r2.output
    assert "alice_run" in r2.output
    assert "Targeting library of alice@example.com" in r2.output

    # Unknown user → clean error.
    r3 = runner.invoke(
        library_app,
        ["--user", "ghost@example.com", "ls", "--config", str(cfg)],
    )
    assert r3.exit_code == 1
    assert "no library account" in r3.output

    # Legacy single_user mode: no targeting logic at all.
    legacy_cfg = tmp_path / "legacy.yaml"
    legacy_cfg.write_text(
        "pdl:\n  enabled: true\n"
        f"  root_dir: {tmp_path / 'legacy_lib'}\n"
        "  storage:\n    backend: sqlite\n",
        encoding="utf-8",
    )
    (tmp_path / "legacy_lib").mkdir()
    r4 = runner.invoke(library_app, ["ls", "--config", str(legacy_cfg)])
    assert r4.exit_code == 0, r4.output
    assert "Targeting library" not in r4.output


def test_scheduler_iterates_user_libraries(tmp_path):
    root = tmp_path / "lib"
    (root / "users" / ("a" * 32)).mkdir(parents=True)
    (root / "users" / ("b" * 32)).mkdir(parents=True)
    (root / "users" / "not-a-user").mkdir(parents=True)
    cfg = AgentTopConfig(
        pdl={
            "root_dir": str(root),
            "storage": {"backend": "sqlite"},
            "auth": PDLAuthConfig(mode="multi_user", owner_email="o@x.com"),
        }
    )
    configs = asyncio.run(_user_library_configs(cfg))
    roots = [c.pdl.root_dir for c in configs]
    assert roots == [str(root / "users" / ("a" * 32)), str(root / "users" / ("b" * 32))]

    # single_user mode: exactly the base config.
    single = AgentTopConfig(pdl={"root_dir": str(root)})
    assert asyncio.run(_user_library_configs(single)) == [single]


def test_main_cli_rejects_unknown_user(tmp_path):
    cfg = _multi_config(tmp_path)
    r = runner.invoke(
        research_app,
        ["--user", "ghost@example.com", "--quiet", "test query", "--config", str(cfg)],
    )
    assert r.exit_code == 2
    assert "no library account" in r.output
