"""Per-user LLM provider, model and API key. The key is write-only: it is never returned."""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from personal_web_research_agent_lite.agents import llm
from personal_web_research_agent_lite.app.deps import get_current_user
from personal_web_research_agent_lite.database.models import LLMProvider, User, UserLLMSettings
from personal_web_research_agent_lite.database.session import get_db
from personal_web_research_agent_lite.services import llm_settings_service as svc

router = APIRouter(prefix="/settings/llm", tags=["llm-settings"], dependencies=[Depends(get_current_user)])

_STAGES = ("planner_model", "researcher_model", "synthesizer_model")


# ───────────────────────── schemas ─────────────────────────

class LLMSettingsUpdate(BaseModel):
    provider: LLMProvider
    api_key: str | None = Field(
        default=None, min_length=8, max_length=500,
        description="Required on first save or when changing provider; omit to keep the stored key.")
    model: str | None = Field(default=None, description="Shortcut: use one model for every stage.")
    planner_model: str | None = None
    researcher_model: str | None = None
    synthesizer_model: str | None = None

    @field_validator("model", *_STAGES)
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        v = v.strip() if v else None
        if v is not None and not (1 <= len(v) <= 120):
            raise ValueError("model name must be 1-120 characters")
        return v or None

    @model_validator(mode="after")
    def _fill_stage_models(self):
        for name in _STAGES:
            if getattr(self, name) is None:
                setattr(self, name, self.model)
            if getattr(self, name) is None:
                raise ValueError(f"Provide `model`, or all of {', '.join(_STAGES)} (missing {name}).")
        return self


class LLMSettingsResponse(BaseModel):
    provider: str
    planner_model: str
    researcher_model: str
    synthesizer_model: str
    api_key_hint: str
    updated_at: datetime

    @classmethod
    def from_row(cls, row: UserLLMSettings) -> "LLMSettingsResponse":
        return cls(provider=row.provider, planner_model=row.planner_model,
                   researcher_model=row.researcher_model, synthesizer_model=row.synthesizer_model,
                   api_key_hint=f"••••{row.api_key_last4}", updated_at=row.updated_at)


class StageTestResult(BaseModel):
    stage: str
    model: str
    ok: bool
    error: str | None = None


class LLMTestResponse(BaseModel):
    ok: bool
    results: list[StageTestResult]


# ───────────────────────── routes ─────────────────────────

@router.get("/providers")
async def list_providers() -> dict[str, list[str]]:
    """Providers and suggested model ids (suggestions only; any model id is accepted)."""
    return svc.SUGGESTED_MODELS


@router.get("", response_model=LLMSettingsResponse)
async def get_llm_settings(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await svc.get_settings(db, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "LLM settings have not been configured yet.")
    return LLMSettingsResponse.from_row(row)


@router.put("", response_model=LLMSettingsResponse)
async def save_llm_settings(body: LLMSettingsUpdate, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    try:
        row = await svc.upsert_settings(
            db, user.id, provider=body.provider.value,
            planner_model=body.planner_model, researcher_model=body.researcher_model,
            synthesizer_model=body.synthesizer_model, api_key=body.api_key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return LLMSettingsResponse.from_row(row)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_llm_settings(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not await svc.delete_settings(db, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "LLM settings have not been configured yet.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/test", response_model=LLMTestResponse)
async def test_llm_settings(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Sends a tiny prompt per stage so a bad key / model name shows up now, not mid-research."""
    try:
        cfg = await svc.load_llm_config(db, user.id)
    except svc.LLMNotConfiguredError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    results = []
    for stage, model in (("planner", cfg.planner_model), ("researcher", cfg.researcher_model),
                         ("synthesizer", cfg.synthesizer_model)):
        try:
            await asyncio.to_thread(
                llm.chat, config=cfg, model=model, temperature=0, max_tokens=8, label=stage,
                messages=[{"role": "user", "content": "Reply with OK."}])
            results.append(StageTestResult(stage=stage, model=model, ok=True))
        except llm.LLMError as exc:
            results.append(StageTestResult(stage=stage, model=model, ok=False, error=str(exc)))
    return LLMTestResponse(ok=all(r.ok for r in results), results=results)