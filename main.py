import logging
import os
import shutil
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
from routers import dashboard
from routers import history
from routers import posted
from routers import tones as tones_router

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full request URLs at INFO, which leaks the APIFY_TOKEN query param.
logging.getLogger("httpx").setLevel(logging.WARNING)

REQUIRED_ENV = ["APIFY_TOKEN"]


def check_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        sys.stderr.write(
            "ERROR: Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in the values.\n"
        )
        raise SystemExit(1)
    cli = os.getenv("CLAUDE_CLI", "claude")
    if not shutil.which(cli):
        logging.warning(
            "Claude CLI '%s' not found on PATH. Comment generation will fail "
            "until this is installed or CLAUDE_CLI is set.",
            cli,
        )


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
app.include_router(tones_router.router)
app.include_router(dashboard.router)
app.include_router(history.router)
app.include_router(posted.router)


@app.get("/")
async def index():
    return RedirectResponse(url="/dashboard")


@app.get("/health")
async def health():
    return {"status": "ok"}
