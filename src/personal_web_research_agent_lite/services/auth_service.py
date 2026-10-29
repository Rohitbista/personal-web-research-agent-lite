"""Auth business rules: register, verify email, login, password reset. No FastAPI imports.

argon2 hashing is CPU-bound, so it runs in a worker thread to keep the event loop free."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from personal_web_research_agent_lite.config.settings import settings
from personal_web_research_agent_lite.database import auth_repo
from personal_web_research_agent_lite.database.models import (
    EmailVerificationToken,
    PasswordResetToken,
    User,
)
from personal_web_research_agent_lite.services import security

logger = logging.getLogger(__name__)

RESET_REQUEST_COOLDOWN_SECONDS = 60


class AuthError(Exception):
    pass


class EmailAlreadyRegisteredError(AuthError):
    pass


class InvalidCredentialsError(AuthError):
    pass


class EmailNotVerifiedError(AuthError):
    pass


class AccountDisabledError(AuthError):
    pass


class InvalidTokenError(AuthError):
    pass


class ResendCooldownError(AuthError):
    def __init__(self, retry_after: int):
        super().__init__(f"Please wait {retry_after}s before requesting another email.")
        self.retry_after = retry_after


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def _issue_token(db: AsyncSession, model, user_id, ttl: timedelta) -> str:
    """Invalidate older unused tokens, create a new one, return the RAW token (emailed once)."""
    await auth_repo.invalidate_unused_tokens(db, model, user_id)
    raw, token_hash = security.new_one_time_token()
    await auth_repo.create_token(db, model, user_id, token_hash, _now() + ttl)
    return raw


# ───────────── registration / verification ─────────────

async def register(db: AsyncSession, email: str, password: str) -> tuple[User, str]:
    """Returns (user, raw_verification_token). Caller emails the token."""
    email = normalize_email(email)
    if await auth_repo.get_user_by_email(db, email):
        raise EmailAlreadyRegisteredError(email)
    try:
        user = await auth_repo.create_user(db, email, await asyncio.to_thread(security.hash_password, password))
        raw = await _issue_token(db, EmailVerificationToken, user.id,
                           timedelta(hours=settings.verification_token_ttl_hours))
        await db.commit()
    except IntegrityError:  # lost a race against a concurrent signup
        await db.rollback()
        raise EmailAlreadyRegisteredError(email)
    await db.refresh(user)
    return user, raw


async def verify_email(db: AsyncSession, raw_token: str) -> User:
    tok = await auth_repo.get_token(db, EmailVerificationToken, security.hash_token(raw_token))
    if tok is None:
        raise InvalidTokenError("Invalid verification link.")
    user = await auth_repo.get_user_by_id(db, tok.user_id)
    if user is None:
        raise InvalidTokenError("Invalid verification link.")

    if tok.used_at is not None:
        if user.is_verified:      # idempotent: double-click / React strict-mode double fetch
            return user
        raise InvalidTokenError("This link has already been replaced or used. Request a new one.")
    if tok.expires_at <= _now():
        raise InvalidTokenError("This link has expired. Request a new one.")

    user.is_verified = True
    user.verified_at = _now()
    await auth_repo.invalidate_unused_tokens(db, EmailVerificationToken, user.id)
    await db.commit()
    return user


async def resend_verification(db: AsyncSession, email: str) -> tuple[User, str] | None:
    """None means 'nothing to send' (unknown / already verified): the router answers identically."""
    user = await auth_repo.get_user_by_email(db, normalize_email(email))
    if user is None or user.is_verified or not user.is_active:
        return None

    last = await auth_repo.latest_token_created_at(db, EmailVerificationToken, user.id)
    if last is not None:
        elapsed = (_now() - last).total_seconds()
        cooldown = settings.verification_resend_cooldown_seconds
        if elapsed < cooldown:
            raise ResendCooldownError(int(cooldown - elapsed) + 1)

    raw = await _issue_token(db, EmailVerificationToken, user.id,
                       timedelta(hours=settings.verification_token_ttl_hours))
    await db.commit()
    return user, raw


# ───────────── login ─────────────

async def login(db: AsyncSession, email: str, password: str) -> tuple[str, int]:
    """Returns (access_token, expires_in_seconds)."""
    user = await auth_repo.get_user_by_email(db, normalize_email(email))
    if user is None:
        await asyncio.to_thread(security.burn_password_check, password)
        raise InvalidCredentialsError()
    if not await asyncio.to_thread(security.verify_password, password, user.password_hash):
        raise InvalidCredentialsError()
    # Only reveal these after the password is proven correct.
    if not user.is_active:
        raise AccountDisabledError()
    if not user.is_verified:
        raise EmailNotVerifiedError()

    if security.password_needs_rehash(user.password_hash):
        user.password_hash = await asyncio.to_thread(security.hash_password, password)
    user.last_login_at = _now()
    await db.commit()
    return security.create_access_token(user.id)


# ───────────── password reset ─────────────

async def request_password_reset(db: AsyncSession, email: str) -> tuple[User, str] | None:
    """None means 'don't send anything'. The router always returns the same generic response."""
    user = await auth_repo.get_user_by_email(db, normalize_email(email))
    if user is None or not user.is_active:
        return None
    last = await auth_repo.latest_token_created_at(db, PasswordResetToken, user.id)
    if last is not None and (_now() - last).total_seconds() < RESET_REQUEST_COOLDOWN_SECONDS:
        return None
    raw = await _issue_token(db, PasswordResetToken, user.id,
                       timedelta(minutes=settings.password_reset_token_ttl_minutes))
    await db.commit()
    return user, raw


async def reset_password(db: AsyncSession, raw_token: str, new_password: str) -> None:
    tok = await auth_repo.get_token(db, PasswordResetToken, security.hash_token(raw_token))
    if tok is None or tok.used_at is not None or tok.expires_at <= _now():
        raise InvalidTokenError("This reset link is invalid or has expired.")
    user = await auth_repo.get_user_by_id(db, tok.user_id)
    if user is None:
        raise InvalidTokenError("This reset link is invalid or has expired.")

    user.password_hash = await asyncio.to_thread(security.hash_password, new_password)
    await auth_repo.invalidate_unused_tokens(db, PasswordResetToken, user.id)
    await db.commit()