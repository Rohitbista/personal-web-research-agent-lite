"""HTTP layer for the research agent. Thin: auth -> validate -> call service -> shape response.

Location: personal_web_research_agent_lite/app/routers/research.py  (replaces the old file)
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from personal_web_research_agent_lite.agents.llm import LLMError
from personal_web_research_agent_lite.app.deps import get_current_user
from personal_web_research_agent_lite.database.models import User
from personal_web_research_agent_lite.services.research_service import (
    MAX_QUERIES,
    InvalidJobStateError,
    JobNotFoundError,
    JobStatus,
    LLMNotConfiguredError,
    ResearchJobRow,
    ResearchMessage,
    ResearchService,
    get_research_service,
    normalize_sources,
)

router = APIRouter(prefix="/research", tags=["research"])


# ───────────────────────── schemas ─────────────────────────

def _strip_queries(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    cleaned = [q.strip() for q in v if q and q.strip()]
    if not cleaned:
        raise ValueError("queries must contain at least one non-empty string")
    return cleaned


class CreateResearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=1000, description="The research question.")
    auto_start: bool = Field(
        default=False,
        description="If true, skip the review step and start researching immediately.",
    )
    parent_job_id: uuid.UUID | None = Field(
        default=None,
        description="Set when this research was spawned from a follow-up on another job.",
    )

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("query is too short")
        return v


class StartResearchRequest(BaseModel):
    queries: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_QUERIES,
        description="Optional edited search queries. Omit to use the planned ones.",
    )

    _clean = field_validator("queries")(_strip_queries)


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("title cannot be empty")
        return v


class FollowupRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question cannot be empty")
        return v


class SourceOut(BaseModel):
    url: str
    title: str | None = None
    snippet: str | None = None


class ResearchJobSummary(BaseModel):
    """Lightweight item for the sidebar / list: no report, no sources."""
    job_id: str
    title: str
    status: JobStatus
    stage: str | None = None
    error_code: str | None = None
    parent_job_id: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def from_job(cls, job: ResearchJobRow) -> "ResearchJobSummary":
        return cls(
            job_id=str(job.id), title=job.title or job.query[:80], status=JobStatus(job.status),
            stage=job.stage, error_code=job.error_code,
            parent_job_id=str(job.parent_job_id) if job.parent_job_id else None,
            created_at=job.created_at, updated_at=job.updated_at, completed_at=job.completed_at,
        )


class JobListResponse(BaseModel):
    items: list[ResearchJobSummary]
    next_cursor: str | None = None


class ResearchJobResponse(BaseModel):
    job_id: str
    title: str
    status: JobStatus
    stage: str | None = None
    query: str
    queries: list[str]
    report: str | None = None
    sources: list[SourceOut] = []
    iterations: int = 0
    error: str | None = None
    error_code: str | None = None
    parent_job_id: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_job(cls, job: ResearchJobRow) -> "ResearchJobResponse":
        return cls(
            job_id=str(job.id), title=job.title or job.query[:80], status=JobStatus(job.status),
            stage=job.stage, query=job.query, queries=list(job.queries), report=job.report,
            sources=[SourceOut(**s) for s in normalize_sources(job.sources)],
            iterations=job.iterations, error=job.error, error_code=job.error_code,
            parent_job_id=str(job.parent_job_id) if job.parent_job_id else None,
            created_at=job.created_at, updated_at=job.updated_at,
            started_at=job.started_at, completed_at=job.completed_at,
        )


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    suggested_query: str | None = None  # set => offer a "Research this further" button
    created_at: datetime

    @classmethod
    def from_row(cls, m: ResearchMessage) -> "MessageOut":
        return cls(id=str(m.id), role=m.role, content=m.content,
                   suggested_query=m.suggested_query, created_at=m.created_at)


class FollowupResponse(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut


# ───────────────────────── error mapping ─────────────────────────

def _not_configured() -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST,
                         "Set up your LLM provider, models and API key first (PUT /settings/llm).")


def _llm_error(exc: LLMError) -> HTTPException:
    if exc.is_auth_error:
        return HTTPException(status.HTTP_400_BAD_REQUEST,
                             "Your LLM provider rejected your API key. Update it via PUT /settings/llm.")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"LLM provider error: {exc}")


_NOT_FOUND = "Research job not found."


# ───────────────────────── routes ─────────────────────────
# Async handlers: DB access is async, and the blocking pipeline runs in worker
# threads inside the service, so the event loop is never blocked.

@router.post("", response_model=ResearchJobResponse, status_code=status.HTTP_201_CREATED)
async def create_research(body: CreateResearchRequest,
                          user: User = Depends(get_current_user),
                          service: ResearchService = Depends(get_research_service)):
    """Plan the search queries + a title. Review/edit them, then call /{job_id}/start."""
    try:
        job = await service.create_job(user.id, body.query, auto_start=body.auto_start,
                                       parent_job_id=body.parent_job_id)
    except LLMNotConfiguredError:
        raise _not_configured()
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Parent research job not found.")
    except LLMError as exc:
        raise _llm_error(exc)
    return ResearchJobResponse.from_job(job)


@router.get("", response_model=JobListResponse)
async def list_research(limit: int = Query(20, ge=1, le=100),
                        cursor: str | None = Query(None, description="next_cursor from the previous page"),
                        status_filter: JobStatus | None = Query(None, alias="status"),
                        q: str | None = Query(None, max_length=100, description="Search title/query"),
                        user: User = Depends(get_current_user),
                        service: ResearchService = Depends(get_research_service)):
    """Newest first. Summaries only (no report); fetch GET /research/{id} for the full job."""
    try:
        rows, next_cursor = await service.list_jobs(
            user.id, limit, cursor,
            status_filter.value if status_filter else None,
            q.strip() if q and q.strip() else None)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return JobListResponse(items=[ResearchJobSummary.from_job(j) for j in rows], next_cursor=next_cursor)


@router.post("/{job_id}/start", response_model=ResearchJobResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def start_research(job_id: uuid.UUID, body: StartResearchRequest | None = None,
                         user: User = Depends(get_current_user),
                         service: ResearchService = Depends(get_research_service)):
    """Queue the job. Poll GET /research/{job_id}: status goes queued -> running (see `stage`)."""
    try:
        job = await service.start_job(user.id, job_id, body.queries if body else None)
    except LLMNotConfiguredError:
        raise _not_configured()
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    except InvalidJobStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return ResearchJobResponse.from_job(job)


@router.post("/{job_id}/cancel", response_model=ResearchJobResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def cancel_research(job_id: uuid.UUID, user: User = Depends(get_current_user),
                          service: ResearchService = Depends(get_research_service)):
    """Cancel a queued/running job. A running job may take a moment to reach `cancelled`
    (the pipeline stops at its next checkpoint), so keep polling."""
    try:
        return ResearchJobResponse.from_job(await service.cancel_job(user.id, job_id))
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    except InvalidJobStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("/{job_id}", response_model=ResearchJobResponse)
async def get_research(job_id: uuid.UUID, user: User = Depends(get_current_user),
                       service: ResearchService = Depends(get_research_service)):
    try:
        return ResearchJobResponse.from_job(await service.get_job(user.id, job_id))
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)


@router.patch("/{job_id}", response_model=ResearchJobResponse)
async def rename_research(job_id: uuid.UUID, body: RenameRequest,
                          user: User = Depends(get_current_user),
                          service: ResearchService = Depends(get_research_service)):
    try:
        return ResearchJobResponse.from_job(await service.rename_job(user.id, job_id, body.title))
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_research(job_id: uuid.UUID, user: User = Depends(get_current_user),
                          service: ResearchService = Depends(get_research_service)):
    """Also stops the job if it is still queued/running."""
    try:
        await service.delete_job(user.id, job_id)
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── follow-up chat (answers from the stored report; no new web search) ──

@router.get("/{job_id}/messages", response_model=list[MessageOut])
async def list_messages(job_id: uuid.UUID, user: User = Depends(get_current_user),
                        service: ResearchService = Depends(get_research_service)):
    try:
        return [MessageOut.from_row(m) for m in await service.list_messages(user.id, job_id)]
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)


@router.post("/{job_id}/messages", response_model=FollowupResponse,
             status_code=status.HTTP_201_CREATED)
async def ask_followup(job_id: uuid.UUID, body: FollowupRequest,
                       user: User = Depends(get_current_user),
                       service: ResearchService = Depends(get_research_service)):
    """Ask a question about a COMPLETED report. If the report can't answer it, the assistant
    message carries `suggested_query`: the UI can offer 'Research this further'
    (POST /research with query=suggested_query and parent_job_id=this job)."""
    try:
        user_msg, asst_msg = await service.ask_followup(user.id, job_id, body.question)
    except LLMNotConfiguredError:
        raise _not_configured()
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    except InvalidJobStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except LLMError as exc:
        raise _llm_error(exc)
    return FollowupResponse(user_message=MessageOut.from_row(user_msg),
                            assistant_message=MessageOut.from_row(asst_msg))


# ── export ──

@router.get("/{job_id}/export")
async def export_research(job_id: uuid.UUID, user: User = Depends(get_current_user),
                          service: ResearchService = Depends(get_research_service)):
    """Download the completed report as Markdown."""
    try:
        filename, markdown = await service.export_job(user.id, job_id)
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    except InvalidJobStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return Response(content=markdown, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})