"""Research job persistence. Every query is scoped by user_id unless it's an internal call.

Location: personal_web_research_agent_lite/database/job_repo.py  (replaces the old file)
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime

from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from personal_web_research_agent_lite.database.models import JobStatus, ResearchJobRow as Row

_STARTABLE = [JobStatus.PLANNED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value]
_ACTIVE = [JobStatus.QUEUED.value, JobStatus.RUNNING.value]


# ───────────────────────── create / read ─────────────────────────

async def add(db: AsyncSession, user_id: uuid.UUID, query: str, queries: list[str],
              title: str | None = None, parent_job_id: uuid.UUID | None = None) -> Row:
    row = Row(user_id=user_id, query=query, queries=queries, title=title, parent_job_id=parent_job_id)
    db.add(row)
    await db.commit()
    await db.refresh(row)  # load server defaults (created_at, status, ...)
    return row


async def get(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Row | None:
    stmt = select(Row).where(Row.id == job_id)
    if user_id is not None:
        stmt = stmt.where(Row.user_id == user_id)
    return await db.scalar(stmt)


async def exists(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    stmt = select(Row.id).where(Row.id == job_id, Row.user_id == user_id)
    return (await db.scalar(stmt)) is not None


# ───────────────────────── list (keyset pagination) ─────────────────────────

def _encode_cursor(created_at: datetime, job_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{job_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, _, jid = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        return datetime.fromisoformat(ts), uuid.UUID(jid)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid cursor.") from exc


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_page(db: AsyncSession, user_id: uuid.UUID, limit: int, cursor: str | None = None,
                    status: str | None = None, q: str | None = None) -> tuple[list[Row], str | None]:
    """Newest first. Returns (rows, next_cursor). Rows have report/sources DEFERRED:
    only read summary fields from them (the summary schema does)."""
    stmt = (select(Row).options(defer(Row.report), defer(Row.sources))
            .where(Row.user_id == user_id))
    if status:
        stmt = stmt.where(Row.status == status)
    if q:
        pat = f"%{_like_escape(q)}%"
        stmt = stmt.where(or_(Row.title.ilike(pat, escape="\\"), Row.query.ilike(pat, escape="\\")))
    if cursor:
        ts, jid = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(Row.created_at, Row.id) < tuple_(ts, jid))
    stmt = stmt.order_by(Row.created_at.desc(), Row.id.desc()).limit(limit + 1)

    rows = list((await db.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id)
    return rows, next_cursor


# ───────────────────────── update ─────────────────────────

async def rename(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID, title: str) -> Row | None:
    stmt = (update(Row).where(Row.id == job_id, Row.user_id == user_id)
            .values(title=title).returning(Row))
    row = (await db.execute(stmt)).scalar_one_or_none()
    await db.commit()
    return row


async def delete_for_user(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    result = await db.execute(delete(Row).where(Row.id == job_id, Row.user_id == user_id))
    await db.commit()
    return result.rowcount > 0


# ───────────────────────── state machine ─────────────────────────
# PLANNED/FAILED/CANCELLED -> QUEUED -> RUNNING -> COMPLETED | FAILED | CANCELLED

async def claim_for_run(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID,
                        queries: list[str] | None) -> Row | None:
    """Atomic (PLANNED|FAILED|CANCELLED) -> QUEUED in ONE statement, so a job can't be
    started twice. Returns None if the job doesn't exist, isn't the user's, or isn't startable."""
    values: dict = {
        "status": JobStatus.QUEUED.value, "stage": "queued",
        "error": None, "error_code": None, "started_at": None, "completed_at": None,
    }
    if queries:
        values["queries"] = queries
    stmt = (update(Row)
            .where(Row.id == job_id, Row.user_id == user_id, Row.status.in_(_STARTABLE))
            .values(**values).returning(Row))
    row = (await db.execute(stmt)).scalar_one_or_none()
    await db.commit()
    return row


async def mark_running(db: AsyncSession, job_id: uuid.UUID) -> Row | None:
    """QUEUED -> RUNNING once a worker slot is free. None if the job was deleted or
    cancelled while it waited."""
    stmt = (update(Row).where(Row.id == job_id, Row.status == JobStatus.QUEUED.value)
            .values(status=JobStatus.RUNNING.value, stage="starting", started_at=func.now())
            .returning(Row))
    row = (await db.execute(stmt)).scalar_one_or_none()
    await db.commit()
    return row


async def set_stage(db: AsyncSession, job_id: uuid.UUID, stage: str) -> None:
    await db.execute(update(Row).where(Row.id == job_id, Row.status == JobStatus.RUNNING.value)
                     .values(stage=stage[:60]))
    await db.commit()


async def cancel_if_queued(db: AsyncSession, job_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    result = await db.execute(
        update(Row).where(Row.id == job_id, Row.user_id == user_id, Row.status == JobStatus.QUEUED.value)
        .values(status=JobStatus.CANCELLED.value, stage=None, completed_at=func.now()))
    await db.commit()
    return result.rowcount > 0


async def mark_cancelled(db: AsyncSession, job_id: uuid.UUID) -> None:
    await db.execute(update(Row).where(Row.id == job_id, Row.status.in_(_ACTIVE)).values(
        status=JobStatus.CANCELLED.value, stage=None, completed_at=func.now()))
    await db.commit()


async def mark_completed(db: AsyncSession, job_id: uuid.UUID, report: str,
                         sources: list[dict], iterations: int) -> None:
    await db.execute(update(Row).where(Row.id == job_id).values(
        status=JobStatus.COMPLETED.value, stage=None, report=report, sources=sources,
        iterations=iterations, error=None, error_code=None, completed_at=func.now()))
    await db.commit()


async def mark_failed(db: AsyncSession, job_id: uuid.UUID, error: str, error_code: str) -> None:
    await db.execute(update(Row).where(Row.id == job_id).values(
        status=JobStatus.FAILED.value, stage=None, error=error[:1000], error_code=error_code,
        completed_at=func.now()))
    await db.commit()


async def fail_all_running(db: AsyncSession, reason: str) -> int:
    """Startup recovery (single-process only): anything QUEUED/RUNNING lost its task."""
    result = await db.execute(update(Row).where(Row.status.in_(_ACTIVE)).values(
        status=JobStatus.FAILED.value, stage=None, error=reason, error_code="interrupted",
        completed_at=func.now()))
    await db.commit()
    return result.rowcount