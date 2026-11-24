"""Password hashing — Argon2id via argon2-cffi.

Kept in its own module so tests can monkeypatch hashing cost or swap the KDF
without touching the auth service.
"""

from __future__ import annotations

import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2-cffi defaults: argon2id, sane time/memory cost

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-time-ish verification; False for NULL hashes or bad formats."""
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash's parameters are below current policy."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return False


def validate_password_strength(password: str) -> str | None:
    """Return a human-readable reason when *password* is unacceptable, else None."""
    if not isinstance(password, str) or not password:
        return "password must not be empty"
    if len(password) < MIN_PASSWORD_LEN:
        return f"password must be at least {MIN_PASSWORD_LEN} characters"
    if len(password) > MAX_PASSWORD_LEN:
        return f"password must be at most {MAX_PASSWORD_LEN} characters"
    return None


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
