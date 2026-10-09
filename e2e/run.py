"""Run the end-to-end scenarios against a throwaway backend.

Needs the PostgreSQL container from mock-servivce running and both venvs installed.
Run with the MCP venv:

    .venv/bin/python -E e2e/run.py                      # decision + payment
    .venv/bin/python -E e2e/run.py --scenario chain     # one real Devnet purchase

The backend is started with mock-servivce/backend/.venv (override AUTARK_BACKEND_PYTHON)
on a new database autark_e2e_<random>, which is dropped afterwards unless --keep-database.
No scenario touches the main database.
"""
import argparse
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from scenarios import SCENARIOS, Checks  # noqa: E402

BACKEND_ROOT = HERE.parents[1] / "mock-servivce" / "backend"
READY_TIMEOUT_SECONDS = 180


def backend_python() -> str:
    override = os.environ.get("AUTARK_BACKEND_PYTHON")
    if override:
        return override
    for candidate in (BACKEND_ROOT / ".venv" / "Scripts" / "python.exe", BACKEND_ROOT / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    sys.exit("mock-servivce/backend/.venv not found; create it or set AUTARK_BACKEND_PYTHON.")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_backend(database: str, port: int, mode: str) -> subprocess.Popen:
    process = subprocess.Popen(
        [backend_python(), str(HERE / "backend_fixture.py"), "--database", database, "--port", str(port), "--mode", mode],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    ready = threading.Event()
    output: list[str] = []

    def read() -> None:
        for line in process.stdout:
            output.append(line)
            if line.strip() == "READY":
                ready.set()

    threading.Thread(target=read, daemon=True).start()
    if not ready.wait(READY_TIMEOUT_SECONDS) or process.poll() is not None:
        process.kill()
        sys.exit("Backend did not start:\n" + "".join(output[-30:]))
    return process


def drop_database(database: str) -> None:
    code = ("import os, sys; sys.path.insert(0, '.'); from psycopg import sql; from backend import database as d; "
            "c = d.connect_db(); c.autocommit = True; c.execute('SET statement_timeout = 0'); "
            f"c.execute(sql.SQL('DROP DATABASE IF EXISTS {{}} WITH (FORCE)').format(sql.Identifier({database!r})))")
    subprocess.run([backend_python(), "-c", code], cwd=BACKEND_ROOT.parent, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all", help="'all' runs decision and payment")
    parser.add_argument("--database", help="Reuse or create this database (must start with autark_e2e_)")
    parser.add_argument("--keep-database", action="store_true")
    options = parser.parse_args()
    names = ["decision", "payment"] if options.scenario == "all" else [options.scenario]
    database = options.database or "autark_e2e_" + uuid4().hex[:12]
    port = free_port()
    check = Checks()
    backend = start_backend(database, port, "chain" if names == ["chain"] else "test-payments")
    try:
        for name in names:
            print(f"\n== {name} (database {database}) ==", flush=True)
            with tempfile.TemporaryDirectory(prefix=f"autark-e2e-{name}-") as config_dir:
                asyncio.run(SCENARIOS[name](f"http://127.0.0.1:{port}", Path(config_dir), check))
    finally:
        backend.terminate()
        backend.wait(timeout=30)
        if options.keep_database:
            print(f"Kept database {database}.")
        else:
            drop_database(database)
    print("\nALL E2E CHECKS PASSED" if not check.failures else "\nFAILED: " + "; ".join(check.failures))
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(main())
