"""Follow-up messages on research jobs.

Location: personal_web_research_agent_lite/database/message_repo.py  (NEW file)

Callers must verify the job belongs to the user first (job_repo.get / job_repo.exists);
this module is scoped by job_id only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_web_research_agent_lite.database.models import MessageRole, ResearchMessage as Msg


async def add_pair(db: AsyncSession, job_id: uuid.UUID, question: str, answer: str,
                   suggested_query: str | None = None) -> tuple[Msg, Msg]:
    """Insert the user question and the assistant answer in one commit.

    created_at is set explicitly (1 ms apart): server-side now() is the transaction start
    time, so both rows would otherwise tie and ordering would be ambiguous."""
    t0 = datetime.now(timezone.utc)
    user_msg = Msg(job_id=job_id, role=MessageRole.USER.value, content=question, created_at=t0)
    asst_msg = Msg(job_id=job_id, role=MessageRole.ASSISTANT.value, content=answer,
                   suggested_query=suggested_query, created_at=t0 + timedelta(milliseconds=1))
    db.add_all([user_msg, asst_msg])
    await db.commit()
    return user_msg, asst_msg


async def list_for_job(db: AsyncSession, job_id: uuid.UUID, limit: int = 200) -> list[Msg]:
    """Oldest first."""
    stmt = select(Msg).where(Msg.job_id == job_id).order_by(Msg.created_at.asc()).limit(limit)
    return list((await db.scalars(stmt)).all())


async def recent(db: AsyncSession, job_id: uuid.UUID, n: int) -> list[Msg]:
    """The last `n` messages, returned oldest first (ready to feed to the LLM)."""
    stmt = select(Msg).where(Msg.job_id == job_id).order_by(Msg.created_at.desc()).limit(n)
    return list(reversed((await db.scalars(stmt)).all()))