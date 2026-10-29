"""
SQLAlchemy 2.0 models (PostgreSQL).

Suggested location: personal_web_research_agent_lite/db/models.py

Design notes
- UUID primary keys (gen_random_uuid(), built into PostgreSQL 13+).
- Enum-like columns (role, provider, status) are plain VARCHAR + CHECK constraints
  rather than native PG enums: adding a new role/provider later is a cheap
  constraint change instead of an awkward ALTER TYPE migration.
- Emails are unique case-insensitively (unique index on lower(email)).
- Verification / reset tokens: only the SHA-256 hash is stored. The raw token
  is emailed once and never persisted.
- LLM API keys are stored Fernet-encrypted (see settings.api_key_encryption_key).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship


# ───────────────────────── enums (used in app code) ─────────────────────────

class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class LLMProvider(str, enum.Enum):
    GROQ = "groq"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class JobStatus(str, enum.Enum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _in_list(column: str, enum_cls: type[enum.Enum]) -> str:
    values = ", ".join(f"'{e.value}'" for e in enum_cls)
    return f"{column} IN ({values})"


# ───────────────────────── base ─────────────────────────

from personal_web_research_agent_lite.database.base import Base

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ───────────────────────── users ─────────────────────────

class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_in_list("role", UserRole), name="ck_users_role"),
        # case-insensitive uniqueness: Foo@x.com == foo@x.com
        Index("uq_users_email_lower", func.lower(text("email")), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Promote manually later:  UPDATE users SET role = 'admin' WHERE email = '...';
    role: Mapped[str] = mapped_column(
        String(20), default=UserRole.USER.value, server_default=UserRole.USER.value, nullable=False
    )

    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lets an admin disable an account without deleting it.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    llm_settings: Mapped["UserLLMSettings | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", passive_deletes=True
    )
    research_jobs: Mapped[list["ResearchJobRow"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    verification_tokens: Mapped[list["EmailVerificationToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    password_reset_tokens: Mapped[list["PasswordResetToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value


# ───────────────────────── one-time tokens ─────────────────────────

class _TokenMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # sha256 hex digest of the raw token that was emailed
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # also drives the resend cooldown (latest created_at per user)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EmailVerificationToken(_TokenMixin, Base):
    __tablename__ = "email_verification_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user: Mapped[User] = relationship(back_populates="verification_tokens")


class PasswordResetToken(_TokenMixin, Base):
    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user: Mapped[User] = relationship(back_populates="password_reset_tokens")


# ───────────────────────── per-user LLM settings ─────────────────────────

class UserLLMSettings(TimestampMixin, Base):
    """One row per user: which provider, which model per stage, and their API key."""
    __tablename__ = "user_llm_settings"
    __table_args__ = (
        CheckConstraint(_in_list("provider", LLMProvider), name="ck_user_llm_settings_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    provider: Mapped[str] = mapped_column(String(30), nullable=False)

    planner_model: Mapped[str] = mapped_column(String(120), nullable=False)
    researcher_model: Mapped[str] = mapped_column(String(120), nullable=False)
    synthesizer_model: Mapped[str] = mapped_column(String(120), nullable=False)

    # Fernet token (url-safe base64 text). Never returned by the API.
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # So the UI can show "••••abcd" without decrypting.
    api_key_last4: Mapped[str] = mapped_column(String(4), nullable=False)

    user: Mapped[User] = relationship(back_populates="llm_settings")


# ───────────────────────── research jobs ─────────────────────────

class ResearchJobRow(TimestampMixin, Base):
    """Persistent version of the in-memory ResearchJob. Every job belongs to a user."""
    __tablename__ = "research_jobs"
    __table_args__ = (
        CheckConstraint(_in_list("status", JobStatus), name="ck_research_jobs_status"),
        Index("ix_research_jobs_user_created", "user_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    query: Mapped[str] = mapped_column(Text, nullable=False)
    queries: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=JobStatus.PLANNED.value, server_default=JobStatus.PLANNED.value, nullable=False
    )
    report: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False)
    iterations: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="research_jobs")