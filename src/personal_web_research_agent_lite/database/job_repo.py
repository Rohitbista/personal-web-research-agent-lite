"""Research job persistence. Every query is scoped by user_id unless it's an internal call."""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_web_research_agent_lite.database.models import JobStatus, ResearchJobRow as Row


async def add(db: AsyncSession, user_id: uuid.UUID, query: str, queries: list[str]) -> Row:
    row = Row(user_id=user_id, query=query, queries=queries)
    db.add(row)
    await db.commit()
    await db.refresh(row)  # load server defaults (created_at, status, ...)
    return row


async def get(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Row | None:
    stmt = select(Row).where(Row.id == job_id)
    if user_id is not None:
        stmt = stmt.where(Row.user_id == user_id)
    return await db.scalar(stmt)


async def list_for_user(db: AsyncSession, user_id: uuid.UUID, limit: int) -> list[Row]:
    stmt = select(Row).where(Row.user_id == user_id).order_by(Row.created_at.desc()).limit(limit)
    return list((await db.scalars(stmt)).all())


async def delete_for_user(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    result = await db.execute(delete(Row).where(Row.id == job_id, Row.user_id == user_id))
    await db.commit()
    return result.rowcount > 0


async def claim_for_run(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID,
                        queries: list[str] | None) -> Row | None:
    """Atomic PLANNED/FAILED -> RUNNING in ONE statement, so a job can't be started twice.
    Returns None if the job doesn't exist, isn't the user's, or isn't startable."""
    values: dict = {"status": JobStatus.RUNNING.value, "error": None}
    if queries:
        values["queries"] = queries
    stmt = (
        update(Row)
        .where(Row.id == job_id, Row.user_id == user_id,
               Row.status.in_([JobStatus.PLANNED.value, JobStatus.FAILED.value]))
        .values(**values)
        .returning(Row)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    await db.commit()
    return row


async def mark_completed(db: AsyncSession, job_id: uuid.UUID, report: str,
                         sources: list[str], iterations: int) -> None:
    await db.execute(update(Row).where(Row.id == job_id).values(
        status=JobStatus.COMPLETED.value, report=report, sources=sources,
        iterations=iterations, error=None))
    await db.commit()


async def mark_failed(db: AsyncSession, job_id: uuid.UUID, error: str) -> None:
    await db.execute(update(Row).where(Row.id == job_id).values(
        status=JobStatus.FAILED.value, error=error[:1000]))
    await db.commit()


async def fail_all_running(db: AsyncSession, reason: str) -> int:
    result = await db.execute(update(Row).where(Row.status == JobStatus.RUNNING.value).values(
        status=JobStatus.FAILED.value, error=reason))
    await db.commit()
    return result.rowcount