"""Shared FastAPI dependencies."""
import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from personal_web_research_agent_lite.database import auth_repo
from personal_web_research_agent_lite.database.models import User
from personal_web_research_agent_lite.database.session import get_db
from personal_web_research_agent_lite.services import security

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Not authenticated.", headers={"WWW-Authenticate": "Bearer"}
    )
    if creds is None:
        raise unauthorized
    user_id = security.decode_access_token(creds.credentials)
    if user_id is None:
        raise unauthorized
    user = await auth_repo.get_user_by_id(db, user_id)
    if user is None or not user.is_active or not user.is_verified:
        raise unauthorized
    # Release the pooled connection now: slow handlers (LLM calls) must not hold it.
    # (expire_on_commit=False keeps `user` readable.)
    await db.commit()
    return user


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client