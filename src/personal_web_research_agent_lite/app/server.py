from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/")
def root():
    return {"message": "Personal web research agent lite version is up and running", "success":True}

def main():
    uvicorn.run(
        "personal_web_research_agent_lite.app.server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
