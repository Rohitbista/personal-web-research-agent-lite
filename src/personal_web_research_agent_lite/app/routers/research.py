"""HTTP layer for the research agent. Thin: validate -> call service -> shape response."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from personal_web_research_agent_lite.agents.llm import LLMError
from personal_web_research_agent_lite.services.research_service import (
    MAX_QUERIES,
    InvalidJobStateError,
    JobNotFoundError,
    JobStatus,
    ResearchJob,
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
    def from_job(cls, job: ResearchJob) -> "ResearchJobResponse":
        ts = lambda t: datetime.fromtimestamp(t, tz=timezone.utc)  # noqa: E731
        return cls(
            job_id=job.id, status=job.status, query=job.query, queries=job.queries,
            report=job.report, sources=job.sources, iterations=job.iterations,
            error=job.error, created_at=ts(job.created_at), updated_at=ts(job.updated_at),
        )


# ───────────────────────── routes ─────────────────────────
# Plain `def` handlers on purpose: the pipeline is blocking (httpx, time.sleep),
# so FastAPI runs them in its threadpool instead of freezing the event loop.

@router.post("", response_model=ResearchJobResponse, status_code=status.HTTP_201_CREATED)
def create_research(body: CreateResearchRequest,
                    service: ResearchService = Depends(get_research_service)):
    """Plan the search queries. Review/edit them, then call /{job_id}/start."""
    try:
        job = service.create_job(body.query, auto_start=body.auto_start)
    except LLMError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"LLM provider error: {exc}")
    return ResearchJobResponse.from_job(job)


@router.post("/{job_id}/start", response_model=ResearchJobResponse,
             status_code=status.HTTP_202_ACCEPTED)
def start_research(job_id: str, body: StartResearchRequest | None = None,
                   service: ResearchService = Depends(get_research_service)):
    """Start researching in the background. Poll GET /research/{job_id} for the result."""
    try:
        job = service.start_job(job_id, body.queries if body else None)
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Research job not found.")
    except InvalidJobStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return ResearchJobResponse.from_job(job)


@router.get("/{job_id}", response_model=ResearchJobResponse)
def get_research(job_id: str, service: ResearchService = Depends(get_research_service)):
    try:
        return ResearchJobResponse.from_job(service.get_job(job_id))
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Research job not found.")


@router.get("", response_model=list[ResearchJobResponse])
def list_research(limit: int = Query(20, ge=1, le=100),
                  service: ResearchService = Depends(get_research_service)):
    return [ResearchJobResponse.from_job(j) for j in service.list_jobs(limit)]


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_research(job_id: str, service: ResearchService = Depends(get_research_service)):
    try:
        service.delete_job(job_id)
    except JobNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Research job not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)