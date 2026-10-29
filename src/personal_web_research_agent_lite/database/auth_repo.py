"""Auth-related queries: users, one-time tokens, per-user LLM settings. No business rules here."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_web_research_agent_lite.database.models import (
    EmailVerificationToken,
    PasswordResetToken,
    User,
    UserLLMSettings,
)

TokenModel = type[EmailVerificationToken] | type[PasswordResetToken]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ───────────── users ─────────────

async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(func.lower(User.email) == email.lower()))


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def create_user(db: AsyncSession, email: str, password_hash: str) -> User:
    user = User(email=email, password_hash=password_hash)
    db.add(user)
    await db.flush()  # raises IntegrityError on duplicate email
    return user


# ───────────── one-time tokens (verification / password reset) ─────────────

async def create_token(db: AsyncSession, model: TokenModel, user_id: uuid.UUID,
                       token_hash: str, expires_at: datetime):
    tok = model(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    db.add(tok)
    await db.flush()
    return tok


async def get_token(db: AsyncSession, model: TokenModel, token_hash: str):
    return await db.scalar(select(model).where(model.token_hash == token_hash))


async def latest_token_created_at(db: AsyncSession, model: TokenModel,
                                  user_id: uuid.UUID) -> datetime | None:
    return await db.scalar(select(func.max(model.created_at)).where(model.user_id == user_id))


async def invalidate_unused_tokens(db: AsyncSession, model: TokenModel, user_id: uuid.UUID) -> None:
    await db.execute(
        update(model)
        .where(model.user_id == user_id, model.used_at.is_(None))
        .values(used_at=_now())
    )


# ───────────── LLM settings ─────────────

async def get_llm_settings(db: AsyncSession, user_id: uuid.UUID) -> UserLLMSettings | None:
    return await db.scalar(select(UserLLMSettings).where(UserLLMSettings.user_id == user_id))


async def delete_llm_settings(db: AsyncSession, user_id: uuid.UUID) -> bool:
    result = await db.execute(delete(UserLLMSettings).where(UserLLMSettings.user_id == user_id))
    await db.commit()
    return result.rowcount > 0