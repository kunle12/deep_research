"""AccountStore (registry DB) unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from deep_research.accounts.store import (
    AccountStore,
    DuplicateEmailError,
    email_token_expiry,
    session_expiry,
)


@pytest.fixture
async def store(tmp_path):
    s = AccountStore(tmp_path / "registry.db")
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


async def test_create_and_get_user(store):
    user = await store.create_user("Wang.Xun@Gmail.com", status="active")
    assert user.user_id
    assert user.email == "Wang.Xun@Gmail.com"  # stored as given; service normalizes
    assert user.status == "active"

    fetched = await store.get_user(user.user_id)
    assert fetched is not None
    assert fetched.email == user.email

    by_email = await store.get_user_by_email("wang.xun@gmail.com")
    # Lookup is exact (normalization happens in AuthService) — different case
    # is a different row key.
    assert by_email is None


async def test_duplicate_email_rejected(store):
    await store.create_user("a@example.com", status="active")
    with pytest.raises(DuplicateEmailError):
        await store.create_user("a@example.com")


async def test_user_status_and_password_updates(store):
    user = await store.create_user("a@example.com", status="pending")
    await store.set_user_status(user.user_id, "active")
    await store.set_password(user.user_id, "argon2$hash")
    fetched = await store.get_user(user.user_id)
    assert fetched.status == "active"
    assert fetched.password_hash == "argon2$hash"

    with pytest.raises(ValueError):
        await store.set_user_status(user.user_id, "bogus")


async def test_record_login_sets_timestamp_and_purges_expired(store):
    user = await store.create_user("a@example.com", status="active")
    # An expired session and an expired token that must be purged on login.
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    await store.create_session(user.user_id, "deadhash", expires_at=past)
    await store.create_email_token(user.user_id, "deadtoken", purpose="verify", expires_at=past)

    await store.record_login(user.user_id)

    assert await store.get_session("deadhash") is None
    assert await store.get_email_token("deadtoken", purpose="verify") is None
    fetched = await store.get_user(user.user_id)
    assert fetched.last_login_at is not None


async def test_session_lifecycle(store):
    user = await store.create_user("a@example.com", status="active")
    expiry = session_expiry(30)
    session = await store.create_session(user.user_id, "hash1", expires_at=expiry)
    assert session.token_hash == "hash1"

    fetched = await store.get_session("hash1")
    assert fetched.user_id == user.user_id
    assert fetched.auth_method == "password"

    await store.touch_session("hash1")
    touched = await store.get_session("hash1")
    assert touched.last_seen_at is not None

    new_expiry = session_expiry(1)
    await store.touch_session("hash1", expires_at=new_expiry)
    assert (await store.get_session("hash1")).expires_at == new_expiry

    await store.revoke_session("hash1")
    assert await store.get_session("hash1") is None


async def test_revoke_user_sessions(store):
    u1 = await store.create_user("a@example.com", status="active")
    u2 = await store.create_user("b@example.com", status="active")
    await store.create_session(u1.user_id, "h1", expires_at=session_expiry(1))
    await store.create_session(u1.user_id, "h2", expires_at=session_expiry(1))
    await store.create_session(u2.user_id, "h3", expires_at=session_expiry(1))

    await store.revoke_user_sessions(u1.user_id)

    assert await store.get_session("h1") is None
    assert await store.get_session("h2") is None
    assert await store.get_session("h3") is not None


async def test_email_token_single_active_per_purpose(store):
    user = await store.create_user("a@example.com", status="pending")
    await store.create_email_token(
        user.user_id, "hash_old", purpose="verify", expires_at=email_token_expiry(24)
    )
    await store.create_email_token(
        user.user_id, "hash_new", purpose="verify", expires_at=email_token_expiry(24)
    )
    # Issuing a new token invalidates the previous one.
    assert await store.get_email_token("hash_old", purpose="verify") is None
    assert await store.get_email_token("hash_new", purpose="verify") is not None
    # Purposes are independent.
    assert await store.get_email_token("hash_new", purpose="reset") is None

    await store.mark_email_token_used("hash_new")
    row = await store.get_email_token("hash_new", purpose="verify")
    assert row.used_at is not None


async def test_meta_roundtrip(store):
    assert await store.meta_get("legacy_migrated") is None
    await store.meta_set("legacy_migrated", "uid123")
    await store.meta_set("legacy_migrated", "uid456")  # upsert
    assert await store.meta_get("legacy_migrated") == "uid456"


async def test_list_users(store):
    await store.create_user("b@example.com", status="pending")
    await store.create_user("a@example.com", status="active")
    users = await store.list_users()
    assert [u.email for u in users] == ["b@example.com", "a@example.com"]


async def test_connect_is_idempotent_schema(tmp_path):
    # Opening the same registry twice in sequence must not fail.
    s = AccountStore(tmp_path / "registry.db")
    await s.connect()
    await s.create_user("a@example.com")
    await s.close()
    s2 = AccountStore(tmp_path / "registry.db")
    await s2.connect()
    assert await s2.get_user_by_email("a@example.com") is not None
    await s2.close()
