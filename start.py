#!/usr/bin/env python3
"""start.py — start the LI_Comments server if it isn't already running.

Cross-platform (macOS, Linux, Windows). Checks whether something is already
listening on the app's port. If so it warns and exits without touching it —
unless --force/--restart is given, in which case it stops the existing process
first. Otherwise it starts uvicorn (preferring the project's .venv) detached,
logging to logs/server.out.

Usage:
    ./start.py                  # start on 127.0.0.1:8000 (warn if already up)
    ./start.py --force          # stop any running server, then start
    ./start.py --restart        # alias for --force
    ./start.py --stop           # stop any running server and exit
    PORT=9000 ./start.py        # override port
    HOST=0.0.0.0 ./start.py

On Windows, invoke it as:  python start.py [--force]
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

HOST = os.getenv("HOST", "127.0.0.1")
PORT = os.getenv("PORT", "8000")
LOG_DIR = Path(os.getenv("LOG_DIR", "./logs"))
SERVER_OUT = LOG_DIR / "server.out"


def listeners(port: str) -> list[int]:
    """PIDs of processes listening on the given TCP port.

    Iterates per-process rather than calling the global psutil.net_connections,
    which requires root on macOS. We only need to find the server we started
    (owned by this user), so AccessDenied on other users' processes is skipped.
    """
    want = int(port)
    pids: list[int] = []
    for proc in psutil.process_iter(["pid"]):
        try:
            conns = (
                proc.net_connections(kind="inet")
                if hasattr(proc, "net_connections")
                else proc.connections(kind="inet")  # psutil < 6.0
            )
            for c in conns:
                if (
                    c.status == psutil.CONN_LISTEN
                    and c.laddr
                    and c.laddr.port == want
                ):
                    pids.append(proc.pid)
                    break
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return pids


def uvicorn_cmd() -> list[str]:
    """Argv prefix to run uvicorn, preferring the project's virtualenv."""
    candidates = [
        Path(".venv/bin/uvicorn"),          # POSIX venv
        Path(".venv/Scripts/uvicorn.exe"),  # Windows venv
    ]
    for c in candidates:
        if c.is_file():
            return [str(c)]
    from shutil import which

    found = which("uvicorn")
    if found:
        return [found]
    # Last resort: run via the current interpreter's module form.
    return [sys.executable, "-m", "uvicorn"]


def stop(pids: list[int]) -> None:
    print(f"Stopping existing server on port {PORT} (PID {' '.join(map(str, pids))})...")
    procs: list[psutil.Process] = []
    for pid in pids:
        try:
            procs.append(psutil.Process(pid))
        except psutil.NoSuchProcess:
            pass
    for p in procs:
        try:
            p.terminate()  # SIGTERM on POSIX, TerminateProcess on Windows
        except psutil.NoSuchProcess:
            pass
    # Wait up to ~10s for graceful exit, then escalate.
    _, alive = psutil.wait_procs(procs, timeout=10)
    if alive:
        print("Process didn't exit gracefully — killing.")
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=3)


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
    parser.add_argument(
        "-s", "--stop",
        dest="stop",
        action="store_true",
        help="stop any running server on the port and exit",
    )
    args = parser.parse_args()

    if args.stop:
        running = listeners(PORT)
        if running:
            stop(running)
            return 0
        print(f"No server running on port {PORT}.")
        return 0

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
    print(f"Starting server on http://{HOST}:{PORT}  (using {' '.join(uvicorn)})")
    out = open(SERVER_OUT, "a")

    # Detach so the server survives this script / terminal exit.
    detach_kwargs: dict = {}
    if os.name == "nt":
        detach_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        detach_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [*uvicorn, "main:app", "--host", HOST, "--port", PORT],
        stdout=out,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        **detach_kwargs,
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
