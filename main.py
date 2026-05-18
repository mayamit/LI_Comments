import logging
import os
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from agent import run_fetch
from database import init_db
from routers import admin

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

REQUIRED_ENV = ["ANTHROPIC_API_KEY", "APIFY_TOKEN"]


def check_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        sys.stderr.write(
            "ERROR: Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in the values.\n"
        )
        raise SystemExit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_env()
    await init_db()
    scheduler = AsyncIOScheduler()
    hour = int(os.getenv("FETCH_SCHEDULE_HOUR", "6"))
    minute = int(os.getenv("FETCH_SCHEDULE_MINUTE", "0"))
    scheduler.add_job(
        run_fetch,
        CronTrigger(hour=hour, minute=minute),
        kwargs={"trigger": "scheduled"},
        id="daily_fetch",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    logging.getLogger(__name__).info(
        "Scheduler started — daily fetch at %02d:%02d", hour, minute
    )
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="LI_Comments", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(admin.router)


@app.get("/")
async def index():
    return RedirectResponse(url="/admin")


@app.get("/health")
async def health():
    return {"status": "ok"}
