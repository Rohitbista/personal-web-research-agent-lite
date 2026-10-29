"""Per-user LLM provider / model / API key management."""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from personal_web_research_agent_lite.agents.llm import LLMConfig
from personal_web_research_agent_lite.database import auth_repo
from personal_web_research_agent_lite.database.models import UserLLMSettings
from personal_web_research_agent_lite.services import security

# Suggestions for the UI dropdown only -- NOT enforced. Model names change often,
# so users may type any model id their provider supports.
SUGGESTED_MODELS: dict[str, list[str]] = {
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b", "openai/gpt-oss-20b"],
    "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4.1"],
    "gemini": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
    "anthropic": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
}


class LLMNotConfiguredError(Exception):
    pass


async def get_settings(db: AsyncSession, user_id: uuid.UUID) -> UserLLMSettings | None:
    return await auth_repo.get_llm_settings(db, user_id)


async def upsert_settings(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    provider: str,
    planner_model: str,
    researcher_model: str,
    synthesizer_model: str,
    api_key: str | None,
) -> UserLLMSettings:
    """api_key=None keeps the stored key (change models without re-typing it),
    except for a first-time setup or a provider change, where a key is required."""
    api_key = api_key.strip() if api_key else None
    row = await auth_repo.get_llm_settings(db, user_id)

    if row is None:
        if not api_key:
            raise ValueError("api_key is required when setting up your LLM for the first time.")
        row = UserLLMSettings(user_id=user_id)
        db.add(row)
    elif not api_key and provider != row.provider:
        raise ValueError("api_key is required when switching provider.")

    row.provider = provider
    row.planner_model = planner_model
    row.researcher_model = researcher_model
    row.synthesizer_model = synthesizer_model
    if api_key:
        row.api_key_encrypted = security.encrypt_secret(api_key)
        row.api_key_last4 = api_key[-4:]
    await db.commit()
    await db.refresh(row)
    return row


async def delete_settings(db: AsyncSession, user_id: uuid.UUID) -> bool:
    return await auth_repo.delete_llm_settings(db, user_id)


async def load_llm_config(db: AsyncSession, user_id: uuid.UUID) -> LLMConfig:
    """Decrypts the key into an in-memory LLMConfig for the pipeline."""
    row = await auth_repo.get_llm_settings(db, user_id)
    if row is None:
        raise LLMNotConfiguredError("No LLM settings saved yet.")
    try:
        key = security.decrypt_secret(row.api_key_encrypted)
    except ValueError as exc:
        raise LLMNotConfiguredError("Your saved API key can't be read; please save it again.") from exc
    return LLMConfig(
        provider=row.provider, api_key=key,
        planner_model=row.planner_model,
        researcher_model=row.researcher_model,
        synthesizer_model=row.synthesizer_model,
    )