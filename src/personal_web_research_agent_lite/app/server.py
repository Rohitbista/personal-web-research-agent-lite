import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from personal_web_research_agent_lite.app.routers.auth import router as auth_router
from personal_web_research_agent_lite.app.routers.llm_settings import router as llm_settings_router
from personal_web_research_agent_lite.app.routers.research import router as research_router
from personal_web_research_agent_lite.config.settings import settings
from personal_web_research_agent_lite.database.session import engine
from personal_web_research_agent_lite.services.research_service import research_service

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=15.0)  # used for Brevo emails
    recovered = await research_service.recover_interrupted_jobs()
    if recovered:
        logger.warning("Marked %d interrupted research job(s) as failed", recovered)
    yield
    await research_service.shutdown()  # cancel running background jobs cleanly
    await app.state.http_client.aclose()
    await engine.dispose()


app = FastAPI(title="Personal Web Research Agent (lite)", lifespan=lifespan)

if settings.frontend_base_url:
    parsed = urlparse(settings.frontend_base_url)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"{parsed.scheme}://{parsed.netloc}"],
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )

app.include_router(auth_router)
app.include_router(llm_settings_router)
app.include_router(research_router)


@app.get("/")
def root():
    return {"message": "Personal web research agent lite version is up and running", "success": True}


def main():
    uvicorn.run(
        "personal_web_research_agent_lite.app.server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )