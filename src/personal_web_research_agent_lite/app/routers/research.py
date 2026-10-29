"""HTTP layer for the research agent. Thin: auth -> validate -> call service -> shape response."""
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
    ResearchService,
    get_research_service,
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


class ResearchJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    query: str
    queries: list[str]
    report: str | None = None
    sources: list[str] = []
    iterations: int = 0
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_job(cls, job: ResearchJobRow) -> "ResearchJobResponse":
        return cls(
            job_id=str(job.id), status=JobStatus(job.status), query=job.query,
            queries=list(job.queries), report=job.report, sources=list(job.sources),
            iterations=job.iterations, error=job.error,
            created_at=job.created_at, updated_at=job.updated_at,
        )


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
    """Plan the search queries. Review/edit them, then call /{job_id}/start."""
    try:
        job = await service.create_job(user.id, body.query, auto_start=body.auto_start)
    except LLMNotConfiguredError:
        raise _not_configured()
    except LLMError as exc:
        raise _llm_error(exc)
    return ResearchJobResponse.from_job(job)


@router.post("/{job_id}/start", response_model=ResearchJobResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def start_research(job_id: uuid.UUID, body: StartResearchRequest | None = None,
                   user: User = Depends(get_current_user),
                   service: ResearchService = Depends(get_research_service)):
    """Start researching in the background. Poll GET /research/{job_id} for the result."""
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


@router.get("/{job_id}", response_model=ResearchJobResponse)
async def get_research(job_id: uuid.UUID, user: User = Depends(get_current_user),
                 service: ResearchService = Depends(get_research_service)):
    try:
        return ResearchJobResponse.from_job(await service.get_job(user.id, job_id))
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)


@router.get("", response_model=list[ResearchJobResponse])
async def list_research(limit: int = Query(20, ge=1, le=100),
                  user: User = Depends(get_current_user),
                  service: ResearchService = Depends(get_research_service)):
    return [ResearchJobResponse.from_job(j) for j in await service.list_jobs(user.id, limit)]


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_research(job_id: uuid.UUID, user: User = Depends(get_current_user),
                    service: ResearchService = Depends(get_research_service)):
    try:
        await service.delete_job(user.id, job_id)
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)