"""
FastAPI application entrypoint.

What this file does:
- Creates the FastAPI app instance and mounts all API routers.
- Ensures database tables exist (SQLite by default).
- Starts the background scheduler that:
  - processes scheduled email sends
  - generates due follow-up drafts (1-3-7 cadence logic)

How to run (Windows / venv):
- .\\venv\\Scripts\\python.exe main.py
Then open: http://localhost:8000/system/status
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import (
    company_router,
    customers_router,
    emails_router,
    followups_router,
    leads_router,
    strategy_router,
    system_router,
    value_content_router,
)
from app.core.scheduler import start_scheduler
from app.database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan hook.

    Starts the background scheduler at startup and shuts it down gracefully.
    """
    scheduler = start_scheduler()
    yield
    scheduler.shutdown()


def create_app() -> FastAPI:
    """
    Builds the FastAPI app and registers routers.

    Note:
    - `Base.metadata.create_all` is used for demo/dev convenience. In production,
      you'd typically use migrations instead.
    """
    Base.metadata.create_all(bind=engine)
    application = FastAPI(title="AI B2B Cold Email Automation", lifespan=lifespan)
    application.include_router(customers_router)
    application.include_router(company_router)
    application.include_router(value_content_router)
    application.include_router(strategy_router)
    application.include_router(emails_router)
    application.include_router(followups_router)
    application.include_router(leads_router)
    application.include_router(system_router)
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
