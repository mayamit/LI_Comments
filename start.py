#!/usr/bin/env python3
"""start.py — start the LI_Comments server if it isn't already running.

Python equivalent of start.sh. Checks whether something is already listening
on the app's port. If so it warns and exits without touching it — unless
--force/--restart is given, in which case it stops the existing process first.
Otherwise it starts uvicorn (preferring the project's .venv) detached, logging
to logs/server.out.

Usage:
    ./start.py                  # start on 127.0.0.1:8000 (warn if already up)
    ./start.py --force          # stop any running server, then start
    ./start.py --restart        # alias for --force
    PORT=9000 ./start.py        # override port
    HOST=0.0.0.0 ./start.py
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HOST = os.getenv("HOST", "127.0.0.1")
PORT = os.getenv("PORT", "8000")
LOG_DIR = Path(os.getenv("LOG_DIR", "./logs"))
SERVER_OUT = LOG_DIR / "server.out"


def listeners(port: str) -> list[int]:
    """PIDs listening on the given TCP port (via lsof)."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("ERROR: 'lsof' not found — required to detect a running server.")
    return [int(p) for p in out.split()]


def uvicorn_cmd() -> str:
    venv = Path(".venv/bin/uvicorn")
    if venv.is_file() and os.access(venv, os.X_OK):
        return str(venv)
    from shutil import which

    found = which("uvicorn")
    if not found:
        sys.exit("ERROR: uvicorn not found (.venv/bin/uvicorn or on PATH).")
    return found


def stop(pids: list[int]) -> None:
    print(f"Stopping existing server on port {PORT} (PID {' '.join(map(str, pids))})...")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Wait up to ~10s for the port to free, then escalate to SIGKILL.
    for _ in range(20):
        time.sleep(0.5)
        if not listeners(PORT):
            return
    print("Process didn't exit gracefully — sending SIGKILL.")
    for pid in listeners(PORT):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(1)


def main() -> int:
    os.chdir(Path(__file__).resolve().parent)

    parser = argparse.ArgumentParser(
        description="Start the LI_Comments server if it isn't already running.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Env overrides: HOST, PORT, LOG_DIR",
    )
    parser.add_argument(
        "-f", "--force", "--restart",
        dest="force",
        action="store_true",
        help="stop any running server on the port, then start",
    )
    args = parser.parse_args()

    uvicorn = uvicorn_cmd()

    running = listeners(PORT)
    if running:
        if args.force:
            stop(running)
        else:
            print(
                f"WARNING: server already running on port {PORT} "
                f"(PID {' '.join(map(str, running))}) — not starting.",
                file=sys.stderr,
            )
            print("         Restart it with:  ./start.py --force", file=sys.stderr)
            return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Starting server on http://{HOST}:{PORT}  (using {uvicorn})")
    out = open(SERVER_OUT, "a")
    proc = subprocess.Popen(
        [uvicorn, "main:app", "--host", HOST, "--port", PORT],
        stdout=out,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach: survives this script / terminal exit
    )

    # Give it a moment, then confirm it actually came up.
    time.sleep(2)
    if proc.poll() is None:
        print(
            f"Started (PID {proc.pid}). Console output -> {SERVER_OUT} ; "
            f"app logs -> {LOG_DIR / 'app.log'}"
        )
        return 0
    print(
        f"ERROR: server exited immediately (code {proc.returncode}). "
        f"Last lines of {SERVER_OUT}:",
        file=sys.stderr,
    )
    tail = SERVER_OUT.read_text(errors="replace").splitlines()[-20:]
    print("\n".join(tail), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
