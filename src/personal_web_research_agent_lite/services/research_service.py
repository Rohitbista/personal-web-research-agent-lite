"""
Service layer: sits between the API and agents.pipeline.

- Owns job state (in-memory for now; swap InMemoryJobStore for Redis/DB later
  by implementing the same few methods).
- Runs research in a small worker pool so HTTP requests return immediately and
  the client polls for the result.
- Knows nothing about FastAPI/HTTP.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum

from personal_web_research_agent_lite.agents import pipeline
from personal_web_research_agent_lite.agents.state import Plan

logger = logging.getLogger(__name__)

MAX_QUERIES = 5


# ───────────────────────── models & errors ─────────────────────────

class JobStatus(str, Enum):
    PLANNED = "planned"      # queries generated, waiting for approval/edit
    RUNNING = "running"      # research + synthesis in progress
    COMPLETED = "completed"
    FAILED = "failed"


class JobNotFoundError(Exception):
    pass


class InvalidJobStateError(Exception):
    pass


@dataclass
class ResearchJob:
    id: str
    query: str
    queries: list[str]
    status: JobStatus = JobStatus.PLANNED
    report: str | None = None
    sources: list[str] = field(default_factory=list)
    iterations: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def snapshot(self) -> "ResearchJob":
        """Detached copy, so callers never see a half-updated job."""
        return replace(self, queries=list(self.queries), sources=list(self.sources))


# ───────────────────────── in-memory store ─────────────────────────

class InMemoryJobStore:
    """Thread-safe, TTL + size bounded. RUNNING jobs are never evicted."""

    def __init__(self, ttl_seconds: int = 3600, max_items: int = 500):
        self._ttl = ttl_seconds
        self._max = max_items
        self._items: OrderedDict[str, ResearchJob] = OrderedDict()
        self._lock = threading.Lock()

    def _evict(self) -> None:  # caller holds the lock
        now = time.time()
        for key in [k for k, j in self._items.items()
                    if j.status != JobStatus.RUNNING and now - j.updated_at > self._ttl]:
            del self._items[key]
        while len(self._items) > self._max:
            for key, job in self._items.items():
                if job.status != JobStatus.RUNNING:
                    del self._items[key]
                    break
            else:
                break  # everything left is running

    def add(self, job: ResearchJob) -> None:
        with self._lock:
            self._items[job.id] = job
            self._evict()

    def get(self, job_id: str) -> ResearchJob | None:
        with self._lock:
            self._evict()
            job = self._items.get(job_id)
            return job.snapshot() if job else None

    def list(self, limit: int = 50) -> list[ResearchJob]:
        with self._lock:
            self._evict()
            jobs = sorted(self._items.values(), key=lambda j: j.created_at, reverse=True)
            return [j.snapshot() for j in jobs[:limit]]

    def update(self, job_id: str, **fields) -> ResearchJob | None:
        """Returns None if the job was deleted/evicted meanwhile."""
        with self._lock:
            job = self._items.get(job_id)
            if job is None:
                return None
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = time.time()
            return job.snapshot()

    def claim_for_run(self, job_id: str, queries: list[str] | None) -> ResearchJob:
        """Atomically PLANNED/FAILED -> RUNNING, so a job can't be started twice."""
        with self._lock:
            job = self._items.get(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            if job.status not in (JobStatus.PLANNED, JobStatus.FAILED):
                raise InvalidJobStateError(f"Job is {job.status.value}; it cannot be started.")
            if queries:
                job.queries = queries
            job.status = JobStatus.RUNNING
            job.error = None
            job.updated_at = time.time()
            return job.snapshot()

    def delete(self, job_id: str) -> bool:
        with self._lock:
            return self._items.pop(job_id, None) is not None


# ───────────────────────── service ─────────────────────────

def _clean_queries(queries: list[str]) -> list[str]:
    cleaned = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not cleaned:
        raise ValueError("At least one non-empty query is required.")
    return cleaned[:MAX_QUERIES]


class ResearchService:
    def __init__(self, store: InMemoryJobStore | None = None, max_concurrent_jobs: int = 2):
        self._store = store or InMemoryJobStore()
        # Bounded so a burst of requests can't blow through Groq rate limits.
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent_jobs,
                                            thread_name_prefix="research-job")

    # -- planning ---------------------------------------------------------
    def create_job(self, query: str, auto_start: bool = False) -> ResearchJob:
        """Blocking (one LLM call). Raises LLMError if the planner fails."""
        job_id = uuid.uuid4().hex
        plan = pipeline.plan(query, trace_id=job_id)
        self._store.add(ResearchJob(id=job_id, query=query, queries=plan.queries))
        if auto_start:
            return self.start_job(job_id)
        return self._store.get(job_id)  # type: ignore[return-value]

    # -- running ----------------------------------------------------------
    def start_job(self, job_id: str, queries: list[str] | None = None) -> ResearchJob:
        """Non-blocking: marks RUNNING and hands the job to the worker pool."""
        cleaned = _clean_queries(queries) if queries else None
        job = self._store.claim_for_run(job_id, cleaned)
        self._executor.submit(self._run_job, job.id)
        return job

    def _run_job(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None:
            return
        try:
            report = pipeline.research(
                Plan(original_query=job.query, queries=job.queries), trace_id=job_id
            )
            self._store.update(
                job_id,
                status=JobStatus.COMPLETED,
                report=report.report,
                sources=report.sources,
                iterations=report.iterations,
            )
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            logger.exception("research job %s failed", job_id)
            self._store.update(job_id, status=JobStatus.FAILED, error=str(exc))

    # -- reads / housekeeping --------------------------------------------
    def get_job(self, job_id: str) -> ResearchJob:
        job = self._store.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def list_jobs(self, limit: int = 50) -> list[ResearchJob]:
        return self._store.list(limit)

    def delete_job(self, job_id: str) -> None:
        if not self._store.delete(job_id):
            raise JobNotFoundError(job_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


# Process-wide singleton (state lives in this process's memory).
research_service = ResearchService()


def get_research_service() -> ResearchService:
    """FastAPI dependency."""
    return research_service