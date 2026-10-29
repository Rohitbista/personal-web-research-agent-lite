"""
Service layer between the API and agents.pipeline.

- Jobs are persisted in PostgreSQL (research_jobs) and belong to a user.
- Provider / models / API key come from the user's saved LLM settings.
- DB access is async (asyncpg). The pipeline itself is blocking (httpx, time.sleep),
  so it runs in a worker thread via asyncio.to_thread; a semaphore bounds how many
  jobs run at once.
- Opens its own short-lived DB sessions, so no connection is held during an LLM call.
- Knows nothing about FastAPI/HTTP.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker

from personal_web_research_agent_lite.agents import pipeline
from personal_web_research_agent_lite.agents.llm import LLMConfig
from personal_web_research_agent_lite.agents.state import Plan
from personal_web_research_agent_lite.database import job_repo
from personal_web_research_agent_lite.database.models import JobStatus, ResearchJobRow
from personal_web_research_agent_lite.database.session import SessionLocal
from personal_web_research_agent_lite.services import llm_settings_service
from personal_web_research_agent_lite.services.llm_settings_service import LLMNotConfiguredError

logger = logging.getLogger(__name__)

MAX_QUERIES = 5

__all__ = ["MAX_QUERIES", "JobStatus", "ResearchJobRow", "ResearchService", "JobNotFoundError",
           "InvalidJobStateError", "LLMNotConfiguredError", "get_research_service", "research_service"]


class JobNotFoundError(Exception):
    pass


class InvalidJobStateError(Exception):
    pass


def _clean_queries(queries: list[str]) -> list[str]:
    cleaned = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not cleaned:
        raise ValueError("At least one non-empty query is required.")
    return cleaned[:MAX_QUERIES]


class ResearchService:
    def __init__(self, session_factory: async_sessionmaker = SessionLocal, max_concurrent_jobs: int = 2):
        self._sf = session_factory
        # Bounded so a burst of requests can't blow through provider rate limits.
        self._sem = asyncio.Semaphore(max_concurrent_jobs)
        self._tasks: set[asyncio.Task] = set()  # strong refs so tasks aren't garbage-collected

    # -- lifecycle --------------------------------------------------------
    async def recover_interrupted_jobs(self) -> int:
        """Jobs left RUNNING by a crash/restart would be stuck forever; mark them FAILED
        (the user can start them again). Single-process deployments only."""
        async with self._sf() as db:
            return await job_repo.fail_all_running(db, "Interrupted by a server restart. Start it again.")

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- planning ---------------------------------------------------------
    async def create_job(self, user_id: uuid.UUID, query: str, auto_start: bool = False) -> ResearchJobRow:
        """One LLM call (in a thread). Raises LLMNotConfiguredError / LLMError."""
        async with self._sf() as db:
            llm = await llm_settings_service.load_llm_config(db, user_id)

        plan = await asyncio.to_thread(pipeline.plan, query, llm, trace_id=str(uuid.uuid4()))

        async with self._sf() as db:
            job = await job_repo.add(db, user_id, query, plan.queries)
        if auto_start:
            return await self._start(user_id, job.id, None, llm)
        return job

    # -- running ----------------------------------------------------------
    async def start_job(self, user_id: uuid.UUID, job_id: uuid.UUID,
                        queries: list[str] | None = None) -> ResearchJobRow:
        """Non-blocking: marks RUNNING and schedules the job in the background."""
        cleaned = _clean_queries(queries) if queries else None
        async with self._sf() as db:
            llm = await llm_settings_service.load_llm_config(db, user_id)
        return await self._start(user_id, job_id, cleaned, llm)

    async def _start(self, user_id: uuid.UUID, job_id: uuid.UUID,
                     queries: list[str] | None, llm: LLMConfig) -> ResearchJobRow:
        async with self._sf() as db:
            job = await job_repo.claim_for_run(db, job_id, user_id, queries)
            if job is None:
                existing = await job_repo.get(db, job_id, user_id)
                if existing is None:
                    raise JobNotFoundError(str(job_id))
                raise InvalidJobStateError(f"Job is {existing.status}; it cannot be started.")
        task = asyncio.create_task(self._run_job(job.id, llm), name=f"research-job-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run_job(self, job_id: uuid.UUID, llm: LLMConfig) -> None:
        try:
            async with self._sem:
                async with self._sf() as db:
                    job = await job_repo.get(db, job_id)
                if job is None:  # deleted meanwhile
                    return
                report = await asyncio.to_thread(
                    pipeline.research,
                    Plan(original_query=job.query, queries=list(job.queries)),
                    llm,
                    trace_id=str(job_id),
                )
                async with self._sf() as db:
                    await job_repo.mark_completed(db, job_id, report.report,
                                                  report.sources, report.iterations)
        except Exception as exc:  # noqa: BLE001 - a task must never die silently
            # (CancelledError at shutdown is a BaseException: the job stays RUNNING and is
            # marked FAILED by recover_interrupted_jobs() on the next start.)
            logger.exception("research job %s failed", job_id)
            try:
                async with self._sf() as db:
                    await job_repo.mark_failed(db, job_id, str(exc))
            except Exception:  # noqa: BLE001
                logger.exception("could not record failure for job %s", job_id)

    # -- reads / housekeeping --------------------------------------------
    async def get_job(self, user_id: uuid.UUID, job_id: uuid.UUID) -> ResearchJobRow:
        async with self._sf() as db:
            job = await job_repo.get(db, job_id, user_id)
        if job is None:
            raise JobNotFoundError(str(job_id))
        return job

    async def list_jobs(self, user_id: uuid.UUID, limit: int = 20) -> list[ResearchJobRow]:
        async with self._sf() as db:
            return await job_repo.list_for_user(db, user_id, limit)

    async def delete_job(self, user_id: uuid.UUID, job_id: uuid.UUID) -> None:
        async with self._sf() as db:
            if not await job_repo.delete_for_user(db, job_id, user_id):
                raise JobNotFoundError(str(job_id))


# Process-wide singleton (owns the background tasks).
research_service = ResearchService()


def get_research_service() -> ResearchService:
    """FastAPI dependency."""
    return research_service