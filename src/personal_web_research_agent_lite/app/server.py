from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn

from personal_web_research_agent_lite.app.routers.research import router as research_router
from personal_web_research_agent_lite.services.research_service import research_service


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    research_service.shutdown()  # stop the background worker pool cleanly


app = FastAPI(title="Personal Web Research Agent (lite)", lifespan=lifespan)
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