import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from database import init_db

load_dotenv()

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
    yield


app = FastAPI(title="LI_Comments", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}
