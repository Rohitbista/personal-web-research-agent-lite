"""Async engine + session factory (SQLAlchemy asyncio, asyncpg driver)."""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from personal_web_research_agent_lite.config.settings import settings


def _async_url(url: str) -> str:
    """Accept plain postgresql:// URLs too, but always use asyncpg."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


engine = create_async_engine(_async_url(settings.database_url),
                             pool_pre_ping=True, pool_size=10, max_overflow=10)

# expire_on_commit=False is REQUIRED with asyncio: expired attributes would trigger
# implicit lazy loads, which can't run outside an `await`. It also keeps rows readable
# after the session closes.
SessionLocal = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionLocal() as db:
        yield db