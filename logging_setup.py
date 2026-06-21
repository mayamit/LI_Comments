"""Centralised logging / crash-diagnostics setup.

Goal: when the app misbehaves or dies, leave enough evidence on disk to
explain *why* — even if the terminal running uvicorn is gone.

What this wires up:
- Rotating file logs at ``logs/app.log`` (plus console) so tracebacks persist.
- ``faulthandler`` writing native stacks to ``logs/faulthandler.log`` for
  segfaults / fatal C-level crashes that never produce a Python traceback.
- ``sys.excepthook`` to record any uncaught exception just before exit.
- An asyncio exception handler to surface errors in fire-and-forget tasks
  that the event loop would otherwise swallow silently.

Tunables (env):
    LOG_LEVEL   DEBUG | INFO (default) | WARNING | ERROR
    LOG_DIR     directory for log files (default ./logs)
"""
import faulthandler
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

_FAULT_FP = None  # keep the file object alive for the process lifetime

_FORMAT = "%(asctime)s %(levelname)s %(name)s [pid:%(process)d] %(message)s"


def _log_dir() -> Path:
    return Path(os.getenv("LOG_DIR", "./logs"))


def setup_logging() -> Path:
    """Configure root logging + crash handlers. Returns the app log path.

    Safe to call once at import time, before the event loop exists.
    """
    global _FAULT_FP

    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    fmt = logging.Formatter(_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)
    # Drop anything basicConfig / uvicorn may have installed so we don't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    app_log = log_dir / "app.log"
    file_handler = logging.handlers.RotatingFileHandler(
        app_log, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # httpx logs full request URLs at INFO, which leaks the APIFY_TOKEN query param.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Native-crash safety net: dumps C-level tracebacks (segfaults, fatal
    # errors) that bypass Python's exception machinery to a file that outlives
    # the dying process.
    _FAULT_FP = open(log_dir / "faulthandler.log", "a", encoding="utf-8")
    faulthandler.enable(file=_FAULT_FP, all_threads=True)
    # faulthandler.enable() already covers fatal signals (SIGSEGV/SIGABRT/...).
    # Additionally dump live Python stacks on SIGTERM — the signal used to kill
    # the process — so we can see *what it was doing* when it was told to die.
    # faulthandler.register is Unix-only — absent on Windows entirely.
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is not None and hasattr(faulthandler, "register"):
        try:
            faulthandler.register(sigterm, file=_FAULT_FP, all_threads=True, chain=True)
        except (ValueError, OSError, RuntimeError, AttributeError):
            pass  # not on the main thread / unsupported — non-fatal

    # Record uncaught exceptions before the interpreter tears down.
    def _excepthook(exc_type, exc, tb):
        logging.getLogger("uncaught").critical(
            "Uncaught exception — process will exit", exc_info=(exc_type, exc, tb)
        )
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    logging.getLogger(__name__).info(
        "Logging initialised: level=%s dir=%s", logging.getLevelName(level), log_dir
    )
    return app_log


def install_asyncio_exception_handler() -> None:
    """Surface exceptions from background tasks / callbacks.

    Must be called once the event loop is running (e.g. inside lifespan),
    otherwise unhandled task errors are logged by asyncio's default handler
    with no app context.
    """
    import asyncio

    log = logging.getLogger("asyncio")

    def handler(loop, context):
        message = context.get("message", "Unhandled exception in event loop")
        exc = context.get("exception")
        if exc is not None:
            log.error(message, exc_info=exc)
        else:
            log.error("%s | context=%r", message, context)

    asyncio.get_event_loop().set_exception_handler(handler)
