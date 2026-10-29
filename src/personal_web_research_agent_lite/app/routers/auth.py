"""Auth HTTP layer: register, verify email, login, password reset, me."""
from __future__ import annotations

from datetime import datetime

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from personal_web_research_agent_lite.app.deps import get_current_user, get_http_client
from personal_web_research_agent_lite.database.models import User
from personal_web_research_agent_lite.database.session import get_db
from personal_web_research_agent_lite.services import auth_service, llm_settings_service
from personal_web_research_agent_lite.services.auth_service import (
    AccountDisabledError,
    EmailAlreadyRegisteredError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
    InvalidTokenError,
    ResendCooldownError,
)
from personal_web_research_agent_lite.services.email_service import (
    send_password_reset_email,
    send_verification_email,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_Password = Field(min_length=8, max_length=128)


# ───────────────────────── schemas ─────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = _Password


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class EmailRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = _Password


class MessageResponse(BaseModel):
    message: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    is_verified: bool
    has_llm_settings: bool
    created_at: datetime


# ───────────────────────── routes ─────────────────────────
# Async handlers; CPU-bound argon2 hashing is pushed to a thread inside auth_service.

@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, background: BackgroundTasks,
             client: httpx.AsyncClient = Depends(get_http_client), db: AsyncSession = Depends(get_db)):
    try:
        user, raw = await auth_service.register(db, body.email, body.password)
    except EmailAlreadyRegisteredError:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")
    background.add_task(send_verification_email, client, user.email, raw)
    return MessageResponse(message="Account created. Check your email for a verification link.")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(body: TokenRequest, db: AsyncSession = Depends(get_db)):
    try:
        await auth_service.verify_email(db, body.token)
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return MessageResponse(message="Email verified. You can now log in.")


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(body: EmailRequest, background: BackgroundTasks,
                        client: httpx.AsyncClient = Depends(get_http_client),
                        db: AsyncSession = Depends(get_db)):
    try:
        result = await auth_service.resend_verification(db, body.email)
    except ResendCooldownError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc),
                            headers={"Retry-After": str(exc.retry_after)})
    if result:
        user, raw = result
        background.add_task(send_verification_email, client, user.email, raw)
    return MessageResponse(message="If that account exists and isn't verified, a new email is on its way.")


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    try:
        token, expires_in = await auth_service.login(db, body.email, body.password)
    except InvalidCredentialsError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.",
                            headers={"WWW-Authenticate": "Bearer"})
    except EmailNotVerifiedError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Email not verified.")
    except AccountDisabledError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled.")
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/forgot-password", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: EmailRequest, background: BackgroundTasks,
                    client: httpx.AsyncClient = Depends(get_http_client),
                    db: AsyncSession = Depends(get_db)):
    result = await auth_service.request_password_reset(db, body.email)
    if result:
        user, raw = result
        background.add_task(send_password_reset_email, client, user.email, raw)
    # Same answer whether or not the account exists.
    return MessageResponse(message="If that account exists, a password reset email is on its way.")


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    try:
        await auth_service.reset_password(db, body.token, body.new_password)
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return MessageResponse(message="Password updated. You can now log in.")


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return UserResponse(
        id=str(user.id), email=user.email, role=user.role, is_verified=user.is_verified,
        has_llm_settings=(await llm_settings_service.get_settings(db, user.id)) is not None,
        created_at=user.created_at,
    )