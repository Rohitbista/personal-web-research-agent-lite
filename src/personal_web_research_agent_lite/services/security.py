"""Password hashing, JWTs, one-time tokens and API-key encryption. No DB, no HTTP."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from personal_web_research_agent_lite.config.settings import settings

# ───────────── passwords (argon2id) ─────────────

_ph = PasswordHasher()
_DUMMY_HASH = _ph.hash("dummy-password-used-for-timing")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


def burn_password_check(password: str) -> None:
    """Unknown email: do equivalent work so response time doesn't reveal which emails exist."""
    verify_password(password, _DUMMY_HASH)


# ───────────── access tokens (JWT) ─────────────

def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    ttl = timedelta(minutes=settings.access_token_expire_minutes)
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + ttl}
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> uuid.UUID | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key,
                             algorithms=[settings.jwt_algorithm],
                             options={"require": ["exp", "sub"]})
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        return None


# ───────────── one-time tokens (email verification / password reset) ─────────────

def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_one_time_token() -> tuple[str, str]:
    """Returns (raw_token_to_email, sha256_hash_to_store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


# ───────────── API-key encryption (Fernet) ─────────────

_fernet = Fernet(settings.api_key_encryption_key.encode())


def encrypt_secret(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Stored secret can't be decrypted (was the encryption key changed?)") from exc