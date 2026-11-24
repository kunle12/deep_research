"""AuthService flow tests: register → verify → login → resolve → reset."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from deep_research.accounts.emails import ConsoleEmailSender, OutgoingEmail
from deep_research.accounts.service import (
    AuthError,
    AuthService,
    RateLimiter,
    hash_token,
    normalize_email,
)
from deep_research.accounts.store import AccountStore, session_expiry
from deep_research.config import PDLAuthConfig


class RecordingSender:
    """Captures outgoing emails for assertions."""

    def __init__(self) -> None:
        self.emails: list[OutgoingEmail] = []

    async def send(self, email: OutgoingEmail) -> None:
        self.emails.append(email)

    def last_token(self) -> str:
        # Tokens are rendered on their own "    <token>" line in the body.
        body = self.emails[-1].body
        for line in body.splitlines():
            t = line.strip()
            if t and " " not in t and len(t) >= 20:
                return t
        raise AssertionError(f"no token found in body: {body!r}")


@pytest.fixture
async def store(tmp_path):
    s = AccountStore(tmp_path / "registry.db")
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def sender() -> RecordingSender:
    return RecordingSender()


@pytest.fixture
def service(store, sender) -> AuthService:
    return AuthService(store, sender, PDLAuthConfig())


async def test_normalize_email(store):
    assert normalize_email("Wang.Xun@GMAIL.com") == "wang.xun@gmail.com"
    assert normalize_email("  wang.xun@gmail.com ") == "wang.xun@gmail.com"
    with pytest.raises(AuthError):
        normalize_email("not-an-email")
    with pytest.raises(AuthError):
        normalize_email("")


async def test_register_verify_login_roundtrip(store, sender, service):
    result = await service.register("wang.xun@gmail.com", "correct horse battery")
    assert result.status == "pending"
    # A pending user cannot log in.
    with pytest.raises(AuthError) as exc:
        await service.login("wang.xun@gmail.com", "correct horse battery")
    assert "not verified" in str(exc.value)

    token = sender.last_token()
    session_token = await service.verify("wang.xun@gmail.com", token)
    assert session_token

    user = await service.resolve_session(session_token)
    assert user is not None
    assert user.email == "wang.xun@gmail.com"
    assert user.status == "active"

    # Login works now and returns a fresh session.
    token2 = await service.login("Wang.Xun@Gmail.com", "correct horse battery")
    assert await service.resolve_session(token2) is not None


async def test_verify_rejects_bad_or_reused_token(store, sender, service):
    await service.register("a@example.com", "long enough password")
    good = sender.last_token()
    with pytest.raises(AuthError):
        await service.verify("a@example.com", "bogus-token")
    session = await service.verify("a@example.com", good)
    assert session
    # Token is single-use: second verify with the same token fails.
    with pytest.raises(AuthError):
        await service.verify("a@example.com", good)


async def test_verify_email_must_match_token_owner(store, sender, service):
    await service.register("a@example.com", "long enough password")
    token = sender.last_token()
    await service.register("b@example.com", "long enough password")
    with pytest.raises(AuthError):
        await service.verify("b@example.com", token)


async def test_login_wrong_password(store, sender, service):
    await service.register("a@example.com", "long enough password")
    await service.verify("a@example.com", sender.last_token())
    with pytest.raises(AuthError) as exc:
        await service.login("a@example.com", "wrong password 1")
    assert exc.value.status == 401


async def test_login_unknown_user_is_generic(store, sender, service):
    with pytest.raises(AuthError) as exc:
        await service.login("nobody@example.com", "long enough password")
    assert exc.value.status == 401
    assert "invalid email or password" in str(exc.value)


async def test_disabled_user_cannot_login_or_resolve(store, sender, service):
    await service.register("a@example.com", "long enough password")
    await service.verify("a@example.com", sender.last_token())
    session = await service.login("a@example.com", "long enough password")
    user = await service.resolve_session(session)
    await store.set_user_status(user.user_id, "disabled")
    with pytest.raises(AuthError) as exc:
        await service.login("a@example.com", "long enough password")
    assert "disabled" in str(exc.value)
    assert await service.resolve_session(session) is None


async def test_logout_revokes_session(store, sender, service):
    await service.register("a@example.com", "long enough password")
    await service.verify("a@example.com", sender.last_token())
    session = await service.login("a@example.com", "long enough password")
    await service.logout(session)
    assert await service.resolve_session(session) is None
    await service.logout(None)  # no-op, must not raise


async def test_register_duplicate_is_not_enumerated_but_notified(store, sender, service):
    await service.register("a@example.com", "long enough password")
    n_before = len(sender.emails)
    result = await service.register("a@example.com", "another password 1")
    assert result.status == "exists"
    # A notification email went to the existing address instead of a verify mail.
    assert len(sender.emails) == n_before + 1
    assert "registration attempt" in sender.emails[-1].subject


async def test_registration_closed(store, sender):
    cfg = PDLAuthConfig(registration="closed")
    service = AuthService(store, sender, cfg)
    with pytest.raises(AuthError) as exc:
        await service.register("a@example.com", "long enough password")
    assert exc.value.status == 403


async def test_password_policy(store, sender, service):
    with pytest.raises(AuthError) as exc:
        await service.register("a@example.com", "short")
    assert "at least 8" in str(exc.value)
    with pytest.raises(AuthError):
        await service.register("a@example.com", "x" * 200)


async def test_expired_session_not_resolved(store, sender, service):
    await service.register("a@example.com", "long enough password")
    await service.verify("a@example.com", sender.last_token())
    session = await service.login("a@example.com", "long enough password")
    # Force-expire the session row directly.
    raw_hash = hash_token(session)
    user = await store.get_user_by_email("a@example.com")
    await store.revoke_session(raw_hash)
    await store.create_session(user.user_id, raw_hash, expires_at=session_expiry(-1))
    assert await service.resolve_session(session) is None


async def test_session_sliding_expiry_extends(store, sender, service):
    cfg = PDLAuthConfig(session_ttl_days=30)
    service = AuthService(store, sender, cfg)
    await service.register("a@example.com", "long enough password")
    await service.verify("a@example.com", sender.last_token())
    session = await service.login("a@example.com", "long enough password")
    raw_hash = hash_token(session)
    user = await store.get_user_by_email("a@example.com")
    # Rewrite the session so it is past half its TTL -> resolve must extend it.
    await store.revoke_session(raw_hash)
    soon = (datetime.now(UTC) + timedelta(days=5)).isoformat(timespec="seconds")
    await store.create_session(user.user_id, raw_hash, expires_at=soon)
    resolved = await service.resolve_session(session)
    assert resolved is not None
    extended = await store.get_session(raw_hash)
    parsed = datetime.fromisoformat(extended.expires_at)
    assert parsed > datetime.fromisoformat(soon)


async def test_password_reset_flow(store, sender, service):
    await service.register("a@example.com", "long enough password")
    await service.verify("a@example.com", sender.last_token())
    old_session = await service.login("a@example.com", "long enough password")

    await service.request_password_reset("a@example.com")
    reset_token = sender.last_token()
    new_session = await service.reset_password("a@example.com", reset_token, "new password 9")
    assert await service.resolve_session(new_session) is not None
    # Old session revoked by the reset.
    assert await service.resolve_session(old_session) is None
    # New password works, old one does not.
    with pytest.raises(AuthError):
        await service.login("a@example.com", "long enough password")
    assert await service.login("a@example.com", "new password 9")


async def test_password_reset_unknown_user_is_silent(store, sender, service):
    await service.request_password_reset("nobody@example.com")
    assert sender.emails == []


async def test_set_password_direct(store, sender, service):
    await service.register("a@example.com", "long enough password")
    await service.verify("a@example.com", sender.last_token())
    session = await service.login("a@example.com", "long enough password")
    await service.set_password_direct("a@example.com", "admin password 7")
    assert await service.resolve_session(session) is None
    assert await service.login("a@example.com", "admin password 7")
    with pytest.raises(AuthError):
        await service.set_password_direct("ghost@example.com", "admin password 7")


async def test_provision_owner_is_idempotent(store, sender, service):
    owner1 = await service.provision_owner("wang.xun@gmail.com")
    owner2 = await service.provision_owner("wang.xun@gmail.com")
    assert owner1.user_id == owner2.user_id
    assert owner1.status == "active"
    # Disabled owner is re-activated by provisioning.
    await store.set_user_status(owner1.user_id, "disabled")
    owner3 = await service.provision_owner("wang.xun@gmail.com")
    assert owner3.status == "active"


async def test_provision_owner_placeholder_email(store, sender, service):
    owner = await service.provision_owner("")
    assert owner.email == "owner@localhost"
    assert owner.status == "active"


async def test_rate_limiter_blocks_login_after_limit(store, sender):
    limiter = RateLimiter()
    cfg = PDLAuthConfig()
    service = AuthService(store, sender, cfg, rate_limiter=limiter)
    for _ in range(10):
        with pytest.raises(AuthError):
            await service.login("a@example.com", "long enough password")
    # The 11th attempt inside the window is rate-limited (not 401).
    with pytest.raises(AuthError) as exc:
        await service.login("a@example.com", "long enough password")
    assert exc.value.status == 429
    limiter.reset()
    # After reset the limiter no longer fires (still 401 for bad credentials).
    with pytest.raises(AuthError) as exc:
        await service.login("a@example.com", "long enough password")
    assert exc.value.status == 401


async def test_concurrent_session_mint(store, sender, service):
    """Parallel logins must each persist a distinct usable session."""
    await service.register("a@example.com", "long enough password")
    await service.verify("a@example.com", sender.last_token())
    tokens = await asyncio.gather(
        *(service.login("a@example.com", "long enough password") for _ in range(5))
    )
    assert len(set(tokens)) == 5
    for t in tokens:
        assert await service.resolve_session(t) is not None


async def test_console_sender_logs(tmp_path, store):
    sender = ConsoleEmailSender()
    await sender.send(OutgoingEmail(to_address="a@b.com", subject="s", body="body"))
    # No exception; logging is the contract.
