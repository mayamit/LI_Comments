import logging
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from agent import run_fetch
from database import init_db
from discover import run_discovery
from logging_setup import install_asyncio_exception_handler, setup_logging
from routers import admin
from routers import dashboard
from routers import discover
from routers import history
from routers import image_proxy
from routers import posted
from routers import tag_admin
from routers import tones as tones_router

load_dotenv()
setup_logging()

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
    install_asyncio_exception_handler()
    await init_db()
    scheduler = AsyncIOScheduler()
    schedule_enabled = os.getenv("FETCH_SCHEDULE_ENABLED", "0").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if schedule_enabled:
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
        logging.getLogger(__name__).info(
            "Scheduler — daily fetch at %02d:%02d", hour, minute
        )

    discovery_enabled = os.getenv("DISCOVERY_SCHEDULE_ENABLED", "0").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if discovery_enabled:
        d_hour = int(os.getenv("DISCOVERY_SCHEDULE_HOUR", "7"))
        d_minute = int(os.getenv("DISCOVERY_SCHEDULE_MINUTE", "0"))
        scheduler.add_job(
            run_discovery,
            CronTrigger(hour=d_hour, minute=d_minute),
            kwargs={"trigger": "scheduled"},
            id="daily_discovery",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logging.getLogger(__name__).info(
            "Scheduler — daily discovery at %02d:%02d", d_hour, d_minute
        )

    scheduler.start()
    if not schedule_enabled and not discovery_enabled:
        logging.getLogger(__name__).info(
            "Scheduler started — automatic fetch and discovery disabled "
            "(set FETCH_SCHEDULE_ENABLED / DISCOVERY_SCHEDULE_ENABLED=1); use 'Run Now'"
        )
    else:
        logging.getLogger(__name__).info("Scheduler started")
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="LI_Comments", lifespan=lifespan)

_request_log = logging.getLogger("request")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with status + duration so hangs, slow runs (e.g. a
    long 'Run Now'), and 5xx errors are visible on disk after the fact."""
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        dur_ms = (time.monotonic() - start) * 1000
        _request_log.exception(
            "%s %s -> unhandled exception after %.0fms",
            request.method,
            request.url.path,
            dur_ms,
        )
        raise
    dur_ms = (time.monotonic() - start) * 1000
    level = logging.INFO
    if response.status_code >= 500 or dur_ms > 1000:
        level = logging.WARNING
    _request_log.log(
        level,
        "%s %s -> %d (%.0fms)",
        request.method,
        request.url.path,
        response.status_code,
        dur_ms,
    )
    return response


app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(admin.router)
app.include_router(tag_admin.router)
app.include_router(tones_router.router)
app.include_router(dashboard.router)
app.include_router(discover.router)
app.include_router(history.router)
app.include_router(posted.router)
app.include_router(image_proxy.router)


@app.get("/")
async def index():
    return RedirectResponse(url="/dashboard")


@app.get("/health")
async def health():
    return {"status": "ok"}
