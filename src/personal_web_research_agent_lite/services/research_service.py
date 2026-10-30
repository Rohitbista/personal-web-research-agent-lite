"""
Service layer between the API and agents.pipeline.

Location: personal_web_research_agent_lite/services/research_service.py  (replaces the old file)

- Jobs are persisted in PostgreSQL (research_jobs) and belong to a user.
- Provider / models / API key come from the user's saved LLM settings.
- DB access is async (asyncpg). The pipeline itself is blocking (httpx, time.sleep),
  so it runs in a worker thread via asyncio.to_thread; a semaphore bounds how many
  jobs run at once.
- Lifecycle: PLANNED -> QUEUED (claimed) -> RUNNING (slot acquired) -> COMPLETED/FAILED/CANCELLED.
- Cancellation is cooperative: a threading.Event per job, checked by the pipeline between steps.
- Opens its own short-lived DB sessions, so no connection is held during an LLM call.
- Knows nothing about FastAPI/HTTP.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from personal_web_research_agent_lite.agents import llm as llm_client
from personal_web_research_agent_lite.agents import pipeline
from personal_web_research_agent_lite.agents.llm import LLMConfig, LLMError
from personal_web_research_agent_lite.agents.state import Plan
from personal_web_research_agent_lite.database import job_repo, message_repo
from personal_web_research_agent_lite.database.models import (
    JobStatus,
    ResearchJobRow,
    ResearchMessage,
)
from personal_web_research_agent_lite.database.session import SessionLocal
from personal_web_research_agent_lite.services import llm_settings_service
from personal_web_research_agent_lite.services.llm_settings_service import LLMNotConfiguredError

logger = logging.getLogger(__name__)

MAX_QUERIES = 5
HISTORY_WINDOW = 10  # follow-up messages sent back to the LLM as context

__all__ = ["MAX_QUERIES", "JobStatus", "ResearchJobRow", "ResearchMessage", "ResearchService",
           "JobNotFoundError", "InvalidJobStateError", "LLMNotConfiguredError",
           "get_research_service", "research_service", "normalize_sources"]


class JobNotFoundError(Exception):
    pass


class InvalidJobStateError(Exception):
    pass


# ───────────────────────── helpers ─────────────────────────

def _clean_queries(queries: list[str]) -> list[str]:
    cleaned = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not cleaned:
        raise ValueError("At least one non-empty query is required.")
    return cleaned[:MAX_QUERIES]


def _fallback_title(query: str, limit: int = 60) -> str:
    q = " ".join(query.split())
    if len(q) <= limit:
        return q
    return q[:limit].rsplit(" ", 1)[0].rstrip(",;:.-") + "…"


def _clean_title(title: str | None) -> str | None:
    if not title:
        return None
    t = " ".join(str(title).split()).strip("\"'“”‘’ ")
    return t[:200] or None


def normalize_sources(sources) -> list[dict]:
    """Accept old rows (plain URL strings), dicts, or objects with .url/.title/.snippet and
    always return [{url, title, snippet}]."""
    out: list[dict] = []
    for s in sources or []:
        if isinstance(s, str):
            out.append({"url": s, "title": None, "snippet": None})
        elif isinstance(s, dict):
            if s.get("url"):
                out.append({"url": s["url"], "title": s.get("title"), "snippet": s.get("snippet")})
        elif getattr(s, "url", None):
            out.append({"url": s.url, "title": getattr(s, "title", None),
                        "snippet": getattr(s, "snippet", None)})
    return out


# User-safe messages. Raw provider exceptions are logged, never stored or returned.
_ERROR_MESSAGES = {
    "llm_auth": "Your LLM provider rejected your API key. Update it in your LLM settings and try again.",
    "llm_rate_limit": "Your LLM provider rate-limited the request. Wait a bit and try again.",
    "llm_error": "The LLM provider returned an error. Try again in a moment.",
    "internal": "Something went wrong while researching. Try again.",
}


def _classify(exc: Exception) -> str:
    if isinstance(exc, LLMError):
        if exc.is_auth_error:
            return "llm_auth"
        if getattr(exc, "is_rate_limit", False):  # optional attribute on your LLMError
            return "llm_rate_limit"
        return "llm_error"
    return "internal"


_FOLLOWUP_SYSTEM = """You answer follow-up questions about ONE finished research report.
Use ONLY the report and the numbered sources below; do not use outside knowledge or guess.
Be concise. When you rely on a source, cite it like [2].
If the report does not contain the answer, say so in one or two sentences and put, on the
LAST line, exactly: [[RESEARCH: <a self-contained web search question that would find it>]]
Otherwise never output that line."""

_RESEARCH_RE = re.compile(r"\[\[RESEARCH:\s*(.+?)\s*\]\]\s*$", re.S)


def _split_suggestion(answer: str) -> tuple[str, str | None]:
    answer = (answer or "").strip()
    m = _RESEARCH_RE.search(answer)
    if not m:
        return answer or "I couldn't produce an answer. Please try rephrasing.", None
    text = answer[:m.start()].rstrip() or "The report doesn't cover that."
    return text, m.group(1).strip()[:1000]


def _build_followup_messages(job: ResearchJobRow, history: list[ResearchMessage], question: str) -> list[dict]:
    sources = normalize_sources(job.sources)
    src_lines = "\n".join(f"[{i}] {s.get('title') or s['url']} - {s['url']}"
                          for i, s in enumerate(sources, 1)) or "(none)"
    system = (f"{_FOLLOWUP_SYSTEM}\n\nORIGINAL QUESTION:\n{job.query}\n\n"
              f"REPORT:\n{job.report}\n\nSOURCES:\n{src_lines}")
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": m.role, "content": m.content} for m in history]
    msgs.append({"role": "user", "content": question})
    return msgs


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "report"


def _render_markdown(job: ResearchJobRow) -> str:
    lines = [f"# {job.title or _fallback_title(job.query)}", "", f"> {job.query}", "",
             job.report or "", ""]
    sources = normalize_sources(job.sources)
    if sources:
        lines += ["## Sources", ""]
        lines += [f"{i}. [{s['title'] or s['url']}]({s['url']})" for i, s in enumerate(sources, 1)]
        lines.append("")
    return "\n".join(lines)


# ───────────────────────── service ─────────────────────────

class ResearchService:
    def __init__(self, session_factory: async_sessionmaker = SessionLocal, max_concurrent_jobs: int = 2):
        self._sf = session_factory
        # Bounded so a burst of requests can't blow through provider rate limits.
        self._sem = asyncio.Semaphore(max_concurrent_jobs)
        self._tasks: set[asyncio.Task] = set()  # strong refs so tasks aren't garbage-collected
        # One cancel flag per queued/running job (single-process deployments).
        self._cancel_events: dict[uuid.UUID, threading.Event] = {}

    # -- lifecycle --------------------------------------------------------
    async def recover_interrupted_jobs(self) -> int:
        """Jobs left QUEUED/RUNNING by a crash/restart would be stuck forever; mark them
        FAILED (the user can start them again). Single-process deployments only."""
        async with self._sf() as db:
            return await job_repo.fail_all_running(db, "Interrupted by a server restart. Start it again.")

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- planning ---------------------------------------------------------
    async def create_job(self, user_id: uuid.UUID, query: str, auto_start: bool = False,
                         parent_job_id: uuid.UUID | None = None) -> ResearchJobRow:
        """One LLM call (in a thread). Raises LLMNotConfiguredError / LLMError / JobNotFoundError
        (parent not found)."""
        async with self._sf() as db:
            cfg = await llm_settings_service.load_llm_config(db, user_id)
            if parent_job_id is not None and not await job_repo.exists(db, parent_job_id, user_id):
                raise JobNotFoundError(str(parent_job_id))

        plan = await asyncio.to_thread(pipeline.plan, query, cfg, trace_id=str(uuid.uuid4()))
        title = _clean_title(getattr(plan, "title", None)) or _fallback_title(query)

        async with self._sf() as db:
            job = await job_repo.add(db, user_id, query, plan.queries,
                                     title=title, parent_job_id=parent_job_id)
        if auto_start:
            return await self._start(user_id, job.id, None, cfg)
        return job

    # -- running ----------------------------------------------------------
    async def start_job(self, user_id: uuid.UUID, job_id: uuid.UUID,
                        queries: list[str] | None = None) -> ResearchJobRow:
        """Non-blocking: marks QUEUED and schedules the job in the background."""
        cleaned = _clean_queries(queries) if queries else None
        async with self._sf() as db:
            cfg = await llm_settings_service.load_llm_config(db, user_id)
        return await self._start(user_id, job_id, cleaned, cfg)

    async def _start(self, user_id: uuid.UUID, job_id: uuid.UUID,
                     queries: list[str] | None, cfg: LLMConfig) -> ResearchJobRow:
        async with self._sf() as db:
            job = await job_repo.claim_for_run(db, job_id, user_id, queries)
            if job is None:
                existing = await job_repo.get(db, job_id, user_id)
                if existing is None:
                    raise JobNotFoundError(str(job_id))
                raise InvalidJobStateError(f"Job is {existing.status}; it cannot be started.")
        self._cancel_events[job.id] = threading.Event()
        task = asyncio.create_task(self._run_job(job.id, cfg), name=f"research-job-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _record(self, fn, job_id: uuid.UUID, *args) -> None:
        """Run one repo write in its own session; never raises (used for best-effort writes)."""
        try:
            async with self._sf() as db:
                await fn(db, job_id, *args)
        except Exception:  # noqa: BLE001
            logger.exception("could not record state for job %s", job_id)

    async def _run_job(self, job_id: uuid.UUID, cfg: LLMConfig) -> None:
        cancel = self._cancel_events.get(job_id) or threading.Event()
        loop = asyncio.get_running_loop()

        def on_progress(stage: str) -> None:
            # Called from the pipeline's worker thread. Fire-and-forget; stale updates
            # after the job finishes are no-ops (set_stage only touches RUNNING rows).
            asyncio.run_coroutine_threadsafe(self._record(job_repo.set_stage, job_id, stage), loop)

        try:
            async with self._sem:
                if cancel.is_set():  # cancelled while waiting for a slot
                    await self._record(job_repo.mark_cancelled, job_id)
                    return
                async with self._sf() as db:
                    job = await job_repo.mark_running(db, job_id)
                if job is None:  # deleted, or cancelled while queued
                    return
                report = await asyncio.to_thread(
                    pipeline.research,
                    Plan(original_query=job.query, queries=list(job.queries)),
                    cfg,
                    trace_id=str(job_id),
                    on_progress=on_progress,
                    should_cancel=cancel.is_set,
                )
                if cancel.is_set():  # user cancelled while the last step was finishing
                    await self._record(job_repo.mark_cancelled, job_id)
                    return
                async with self._sf() as db:
                    await job_repo.mark_completed(db, job_id, report.report,
                                                  normalize_sources(report.sources), report.iterations)
        except pipeline.PipelineCancelled:
            await self._record(job_repo.mark_cancelled, job_id)
        except Exception as exc:  # noqa: BLE001 - a task must never die silently
            # (CancelledError at shutdown is a BaseException: the job stays RUNNING and is
            # marked FAILED by recover_interrupted_jobs() on the next start.)
            code = _classify(exc)
            logger.exception("research job %s failed (%s)", job_id, code)
            await self._record(job_repo.mark_failed, job_id, _ERROR_MESSAGES[code], code)
        finally:
            self._cancel_events.pop(job_id, None)

    async def cancel_job(self, user_id: uuid.UUID, job_id: uuid.UUID) -> ResearchJobRow:
        async with self._sf() as db:
            job = await job_repo.get(db, job_id, user_id)
            if job is None:
                raise JobNotFoundError(str(job_id))
            if job.status not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
                raise InvalidJobStateError(f"Job is {job.status}; only queued or running jobs can be cancelled.")

            event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()
            # Queued: flip straight to CANCELLED (atomic; loses harmlessly if it just started).
            # Running: the pipeline sees the flag at its next checkpoint and finishes as CANCELLED.
            # No live task (shouldn't happen): fix the row directly so it can't stay stuck.
            if job.status == JobStatus.QUEUED.value:
                await job_repo.cancel_if_queued(db, job_id, user_id)
            elif event is None:
                await job_repo.mark_cancelled(db, job_id)
            return await job_repo.get(db, job_id, user_id)

    # -- reads / housekeeping --------------------------------------------
    async def get_job(self, user_id: uuid.UUID, job_id: uuid.UUID) -> ResearchJobRow:
        async with self._sf() as db:
            job = await job_repo.get(db, job_id, user_id)
        if job is None:
            raise JobNotFoundError(str(job_id))
        return job

    async def list_jobs(self, user_id: uuid.UUID, limit: int = 20, cursor: str | None = None,
                        status: str | None = None, q: str | None = None
                        ) -> tuple[list[ResearchJobRow], str | None]:
        """Raises ValueError on a bad cursor."""
        async with self._sf() as db:
            return await job_repo.list_page(db, user_id, limit, cursor, status, q)

    async def rename_job(self, user_id: uuid.UUID, job_id: uuid.UUID, title: str) -> ResearchJobRow:
        async with self._sf() as db:
            job = await job_repo.rename(db, job_id, user_id, title)
        if job is None:
            raise JobNotFoundError(str(job_id))
        return job

    async def delete_job(self, user_id: uuid.UUID, job_id: uuid.UUID) -> None:
        # Stop a live pipeline first, otherwise it would keep burning the user's API key.
        event = self._cancel_events.get(job_id)
        async with self._sf() as db:
            if not await job_repo.delete_for_user(db, job_id, user_id):
                raise JobNotFoundError(str(job_id))
        if event is not None:  # only after we know the job is really the user's
            event.set()

    async def export_job(self, user_id: uuid.UUID, job_id: uuid.UUID) -> tuple[str, str]:
        """Returns (filename, markdown)."""
        job = await self.get_job(user_id, job_id)
        if job.status != JobStatus.COMPLETED.value or not job.report:
            raise InvalidJobStateError("Only completed research can be exported.")
        return f"{_slug(job.title or job.query)}.md", _render_markdown(job)

    # -- follow-up chat ---------------------------------------------------
    async def list_messages(self, user_id: uuid.UUID, job_id: uuid.UUID) -> list[ResearchMessage]:
        async with self._sf() as db:
            if not await job_repo.exists(db, job_id, user_id):
                raise JobNotFoundError(str(job_id))
            return await message_repo.list_for_job(db, job_id)

    async def ask_followup(self, user_id: uuid.UUID, job_id: uuid.UUID,
                           question: str) -> tuple[ResearchMessage, ResearchMessage]:
        """Answer from the stored report + sources with ONE LLM call (no web search).
        Raises JobNotFoundError, InvalidJobStateError, LLMNotConfiguredError, LLMError."""
        async with self._sf() as db:
            job = await job_repo.get(db, job_id, user_id)
            if job is None:
                raise JobNotFoundError(str(job_id))
            if job.status != JobStatus.COMPLETED.value or not job.report:
                raise InvalidJobStateError("Follow-up questions are available once the research is completed.")
            cfg = await llm_settings_service.load_llm_config(db, user_id)
            history = await message_repo.recent(db, job_id, HISTORY_WINDOW)
        # session closed: no DB connection is held during the LLM call

        messages = _build_followup_messages(job, history, question)
        msg = await asyncio.to_thread(
            llm_client.chat, config=cfg, model=cfg.synthesizer_model, temperature=0.2,
            max_tokens=1500, label="followup", trace_id=str(uuid.uuid4()), messages=messages)
        text, suggested = _split_suggestion(msg.get("content") or "")

        try:
            async with self._sf() as db:
                return await message_repo.add_pair(db, job_id, question, text, suggested)
        except IntegrityError:  # job was deleted while the LLM was answering
            raise JobNotFoundError(str(job_id)) from None


# Process-wide singleton (owns the background tasks).
research_service = ResearchService()


def get_research_service() -> ResearchService:
    """FastAPI dependency."""
    return research_service