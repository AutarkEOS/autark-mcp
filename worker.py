#!/usr/bin/env python3
"""Offer this machine's idle time to other agents through the Autark marketplace.

A background process, not an MCP server: MCP tools only run when a model calls them,
and an idle agent calls nothing. The worker polls the marketplace for queued jobs of
its own compute offers, runs them, and returns the result. It is paid per second.

It only works while the human consented: `sharing.enabled` in the agent config. The
MCP server starts it; `python -E worker.py` starts it by hand. It keeps its own vendor
account, separate from the buying agent: the login and the payout key in the system
credential store (vault.py), and only public details in worker.json next to the agent config.
Sales are paid to that payout key, which never leaves this machine.

Jobs come from strangers. A script runs in a Docker container with no network; a
written task is handed to the command in AUTARK_WORKER_AGENT_COMMAND as data.
"""

import argparse
import asyncio
import base64
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_config import AgentConfigError, load_config, state_path
from client import MockShopApiError, Session
from settings import api_base_url
import vault

POLL_SECONDS = float(os.environ.get("AUTARK_WORKER_POLL_SECONDS", "3"))
HEARTBEAT_SECONDS = 10
# A second worker defers to one whose status file is fresher than this.
ALIVE_SECONDS = 20
MAX_OUTPUT_BYTES = 65536
SCRIPT_IMAGE = os.environ.get("AUTARK_WORKER_IMAGE", "python:3.12-alpine")
OFFER_URLS = {"script": "worker:script", "agent": "worker:agent"}
# The agent command gets a minimal environment: enough to start and find its own login, none of this machine's secrets.
AGENT_ENVIRONMENT = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "PROGRAMDATA", "USERNAME", "USER", "LANG", "LC_ALL", "TERM",
)


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + message, flush=True)


def write_json(path: Path, data: Any, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.touch()
    if private or os.name != "nt":
        # Restricted before anything is written; os.replace keeps the permissions.
        vault.restrict_to_owner(temporary)
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_status() -> dict[str, Any] | None:
    """The running worker's last report, or None when no worker is alive."""
    status = read_json(state_path("worker-status.json"))
    if not isinstance(status, dict) or time.time() - status.get("updated_at", 0) > ALIVE_SECONDS or status.get("state") == "stopped":
        return None
    return status


def seconds_since_activity() -> float:
    """Time since the MCP server last handled a tool call. No record means the agent was never used here."""
    try:
        return max(0.0, time.time() - state_path("last-activity").stat().st_mtime)
    except OSError:
        return float("inf")


def clean_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def truncate(*parts: bytes) -> tuple[list[str], bool]:
    """Share one output budget between the parts, first part first."""
    remaining, texts, truncated = MAX_OUTPUT_BYTES, [], False
    for part in parts:
        text = clean_text(part)
        encoded = text.encode()
        if len(encoded) > remaining:
            text, truncated = encoded[:remaining].decode("utf-8", errors="ignore"), True
        remaining -= len(text.encode())
        texts.append(text)
    return texts, truncated


async def run_process(arguments: list[str], stdin: bytes, limit: float, *, cwd: str | None = None,
                      environment: dict[str, str] | None = None, on_timeout=None) -> tuple[bytes, bytes, int, bool, str]:
    """Run to completion or stop at `limit` seconds. Returns stdout, stderr, exit code, timed_out, elapsed text."""
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *arguments, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd, env=environment)
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=limit)
    except asyncio.TimeoutError:
        timed_out = True
        if on_timeout is not None:
            await on_timeout()
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except asyncio.TimeoutError:
            stdout, stderr = b"", b""
    elapsed = min(time.monotonic() - started, limit) if timed_out else time.monotonic() - started
    code = process.returncode if process.returncode is not None else 124
    return stdout, stderr, 124 if timed_out else code, timed_out, f"{elapsed:.3f}"


async def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        _out, _err, code, timed_out, _elapsed = await run_process(["docker", "info"], b"", 20)
        if code != 0 or timed_out:
            return False
        _out, _err, code, _timed_out, _elapsed = await run_process(["docker", "image", "inspect", SCRIPT_IMAGE], b"", 20)
        if code != 0:
            log(f"Pulling {SCRIPT_IMAGE} once, before any job is accepted.")
            _out, _err, code, _timed_out, _elapsed = await run_process(["docker", "pull", SCRIPT_IMAGE], b"", 600)
        return code == 0
    except OSError:
        return False


async def run_script(source: bytes, limit: float) -> dict[str, Any]:
    """A stranger's Python in a throwaway container: no network, no host files, read-only, capped CPU, memory and processes."""
    name = "autark-job-" + uuid4().hex[:12]

    async def stop() -> None:
        await run_process(["docker", "kill", name], b"", 15)

    stdout, stderr, code, timed_out, elapsed = await run_process(
        ["docker", "run", "--rm", "-i", "--name", name, "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=16m",
         "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "64", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "--user", "65534:65534", "--workdir", "/tmp", SCRIPT_IMAGE, "python", "-I", "-"],
        source, limit, on_timeout=stop)
    (out, err), truncated = truncate(stdout, stderr)
    return {"stdout": out, "stderr": err, "exit_code": code, "timed_out": timed_out, "output_truncated": truncated,
            "elapsed_compute_seconds": elapsed}


async def run_task(task: bytes, limit: float, command: list[str]) -> dict[str, Any]:
    """Hand the task text to the local agent command on stdin, in an empty directory."""
    environment = {name: os.environ[name] for name in AGENT_ENVIRONMENT if name in os.environ}
    with tempfile.TemporaryDirectory(prefix="autark-task-") as directory:
        try:
            stdout, stderr, code, timed_out, elapsed = await run_process(command, task, limit, cwd=directory, environment=environment)
        except OSError as error:
            stdout, stderr, code, timed_out, elapsed = b"", f"The agent command could not start: {error}".encode(), 127, False, "0.000"
    # The buyer asked for the answer; stderr only explains a failure.
    (out, err), truncated = truncate(stdout, stderr if code != 0 else b"")
    output = out if not err else (out + "\n" if out else "") + err
    return {"output": output, "exit_code": code, "timed_out": timed_out, "output_truncated": truncated,
            "elapsed_compute_seconds": elapsed}


def agent_command() -> list[str] | None:
    """AUTARK_WORKER_AGENT_COMMAND as a JSON list of arguments, or a plain command line such as `claude -p`."""
    raw = os.environ.get("AUTARK_WORKER_AGENT_COMMAND", "").strip()
    if not raw:
        return None
    if raw.startswith("["):
        try:
            arguments = json.loads(raw)
        except ValueError:
            arguments = None
        if not isinstance(arguments, list) or not arguments or any(not isinstance(item, str) or not item for item in arguments):
            log("AUTARK_WORKER_AGENT_COMMAND is not a JSON list of strings; agent tasks are not served.")
            return None
    else:
        # Windows paths contain backslashes, so quotes are stripped by hand instead of POSIX unescaping.
        arguments = [item[1:-1] if len(item) > 1 and item[0] == item[-1] and item[0] in "\"'" else item
                     for item in shlex.split(raw, posix=False)] if os.name == "nt" else shlex.split(raw)
    resolved = shutil.which(arguments[0])
    return [resolved or arguments[0], *arguments[1:]]


class Worker:
    def __init__(self) -> None:
        self.session = Session()
        self.api_url = api_base_url()
        self.offers: dict[str, str] = {}
        self.unavailable: dict[str, str] = {}
        self.busy_with: str | None = None
        self.last_error: str | None = None
        self.command = agent_command()

    # --- account --------------------------------------------------------

    def _saved(self) -> dict[str, Any]:
        """Public details of the vendor account per marketplace: username, payout address and offers. No secrets."""
        state = read_json(state_path("worker.json"))
        return state if isinstance(state, dict) else {}

    def _save(self, account: dict[str, Any]) -> None:
        public = {key: value for key, value in account.items() if key in ("username", "payout_address", "offers")}
        write_json(state_path("worker.json"), {**self._saved(), self.api_url: public}, private=True)

    def _secret(self) -> dict[str, Any] | None:
        """The login for this marketplace from the credential store. A password an older worker left in worker.json moves there."""
        secret = vault.load(self.api_url)
        legacy = self._saved().get(self.api_url)
        if isinstance(legacy, dict) and "password" in legacy:
            if secret is None or secret.get("username") != legacy.get("username"):
                secret = {**(secret or {}), "username": legacy["username"], "password": legacy["password"]}
                vault.save(self.api_url, secret)
            self._save(legacy)
            log(f"Moved the worker password out of worker.json into {vault.store_name()}.")
        return secret

    async def _sign_in(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        _status, answer = await self.session.request("POST", path, json_body=body, auth=False)
        self.session._store_token(answer)
        return answer

    async def sign_in(self) -> dict[str, Any]:
        """Sign in with the stored login, or register a vendor that is paid to a key made here and never sent anywhere."""
        secret = self._secret()
        if secret is not None:
            try:
                await self._sign_in("/api/auth/login", {"username": secret["username"], "password": secret["password"]})
                return {**(self._saved().get(self.api_url) or {}), "username": secret["username"]}
            except MockShopApiError as error:
                if error.status not in (401, 403):
                    raise
                log("The stored worker account cannot sign in to this marketplace any more; registering a new one.")
        host = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().lower())[:30].strip("-") or "machine"
        username, seed = f"worker-{host}-{secrets.token_hex(3)}", vault.new_payout_seed()
        retired = [*(secret or {}).get("retired_payout_seeds", []), *([secret["payout_seed"]] if (secret or {}).get("payout_seed") else [])]
        new = {"username": username, "password": secrets.token_urlsafe(24), "payout_seed": seed, "retired_payout_seeds": retired}
        # Stored before the marketplace knows the account: a crash cannot leave an account nobody can sign in to,
        # and keys an earlier account was paid to are kept.
        vault.save(self.api_url, new)
        await self._sign_in("/api/auth/register", {
            "username": username, "password": new["password"], "role": "vendor", "payout_pubkey": vault.payout_address(seed),
            "payout_signature": vault.sign(seed, f"Autark payout address for {username}".encode())})
        account = {"username": username, "payout_address": vault.payout_address(seed), "offers": {}}
        self._save(account)
        log(f"Registered vendor account {username}. Sales are paid to {account['payout_address']}; its key stays in {vault.store_name()}.")
        return account

    async def call(self, method: str, path: str, **arguments: Any) -> tuple[int, dict[str, Any]]:
        """An authenticated request; a session that expired is renewed once."""
        try:
            return await self.session.request(method, path, auth=True, **arguments)
        except MockShopApiError as error:
            if error.status != 401:
                raise
            self.session.token = None
            await self.sign_in()
            return await self.session.request(method, path, auth=True, **arguments)

    async def publish(self, sharing: dict[str, Any]) -> None:
        """One compute offer per shared mode, at the configured rate per second."""
        account = self._saved().get(self.api_url) or {}
        saved = dict(account.get("offers") or {})
        _status, listing = await self.call("GET", "/api/vendor/services")
        # A withdrawn offer takes no jobs; a mode shared again gets a new one.
        existing = {service["id"]: service for service in listing.get("services", []) if not service.get("archived_at")}
        host = socket.gethostname()
        for mode in sorted(set(saved) - set(sharing["modes"])):
            await self.withdraw(saved.pop(mode))
        for mode in sharing["modes"]:
            offer = existing.get(saved.get(mode))
            if offer is None:
                _status, created = await self.call("POST", "/api/vendor/services", idempotency_key=f"worker-{uuid4()}", json_body={
                    "kind": "compute", "url": OFFER_URLS[mode], "price_minor": sharing["rate_minor_per_second"],
                    "name": f"Idle {'agent' if mode == 'agent' else 'machine'} ({host})",
                    "description": ("An idle agent on another machine works on a written task." if mode == "agent"
                                    else "Runs a Python script in an isolated container on an idle machine, without network access.")
                    + " Billed per second, measured by the marketplace."})
                offer = created["service"]
                log(f"Published {mode} offer {offer['id']} at {offer['price_minor']} minor/s.")
            elif offer["price_minor"] != sharing["rate_minor_per_second"]:
                _status, updated = await self.call("PATCH", f"/api/vendor/services/{offer['id']}", json_body={
                    "revision": offer["revision"], "price_minor": sharing["rate_minor_per_second"]})
                offer = updated["service"]
                log(f"Offer {offer['id']} now costs {offer['price_minor']} minor/s.")
            saved[mode] = offer["id"]
        self._save({**account, "offers": saved})
        self.offers = {mode: saved[mode] for mode in sharing["modes"]}

    async def withdraw(self, offer: str) -> None:
        try:
            await self.call("DELETE", f"/api/vendor/services/{offer}")
            log(f"Withdrew offer {offer}.")
        except MockShopApiError as error:
            if error.status not in (404, 410):
                raise

    async def withdraw_all(self) -> None:
        """Sharing was turned off: take this machine's offers out of the catalog instead of leaving them offline."""
        account = self._saved().get(self.api_url) or {}
        if not account.get("offers"):
            return
        try:
            await self.sign_in()
            for offer in account["offers"].values():
                await self.withdraw(offer)
            self._save({**account, "offers": {}})
        except (MockShopApiError, vault.VaultError) as error:
            log(f"The offers stay listed as offline; withdrawing them failed: {error}")

    # --- work -----------------------------------------------------------

    def report(self, state: str) -> None:
        write_json(state_path("worker-status.json"), {
            "pid": os.getpid(), "state": state, "updated_at": time.time(), "api_url": self.api_url, "offers": self.offers,
            "payout_address": (self._saved().get(self.api_url) or {}).get("payout_address"),
            "unavailable": self.unavailable, "busy_with": self.busy_with, "last_error": self.last_error})

    async def keep_reporting(self) -> None:
        """While a job runs the offers stay online, and a second worker still sees this one alive."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            self.report("busy")
            for offer in self.offers.values():
                try:
                    await self.call("POST", f"/api/worker/{offer}/heartbeat")
                except MockShopApiError:
                    pass

    async def serve(self, mode: str, job: dict[str, Any]) -> None:
        self.busy_with = job["run_id"]
        self.report("busy")
        log(f"Job {job['run_id']} ({job['job_type']}), up to {job['max_compute_seconds']} s.")
        payload, limit = base64.b64decode(job["payload_base64"]), float(Decimal(job["max_compute_seconds"]))
        reporting = asyncio.create_task(self.keep_reporting())
        try:
            result = await (run_task(payload, limit, self.command) if mode == "agent" else run_script(payload, limit))
        finally:
            reporting.cancel()
        try:
            _status, answer = await self.call("POST", f"/api/worker/runs/{job['run_id']}/result", json_body=result)
            run = answer.get("run", {})
            log(f"Job {job['run_id']} returned: billed {run.get('billed_seconds')} s, {run.get('charge_minor')} minor.")
        except MockShopApiError as error:
            self.last_error = f"Result of {job['run_id']} was not accepted: {error.message}"
            log(self.last_error)
        finally:
            self.busy_with = None

    async def run(self, once: bool = False) -> int:
        current = read_status()
        if current and current.get("pid") != os.getpid():
            log(f"A worker is already running (pid {current.get('pid')}).")
            return 0
        self.report("starting")
        published_for = None
        while True:
            try:
                config, readable = load_config(), True
            except AgentConfigError as error:
                log(f"Agent config is unreadable, so nothing is shared: {error}")
                config, readable = None, False
            sharing = (config or {}).get("sharing")
            if not sharing or not sharing["enabled"]:
                if readable:
                    await self.withdraw_all()
                log("Sharing is not enabled in the agent config. Stopping.")
                self.report("stopped")
                return 0
            try:
                if published_for != (sharing["modes"], sharing["rate_minor_per_second"]):
                    await self.prepare(sharing)
                    published_for = (sharing["modes"], sharing["rate_minor_per_second"])
                idle = seconds_since_activity() >= sharing["idle_after_seconds"]
                if idle:
                    # Claiming also reports this offer as online; an active agent stays silent and drops out of the catalog.
                    for mode, offer in self.offers.items():
                        if mode in self.unavailable:
                            continue
                        status, answer = await self.call("POST", f"/api/worker/{offer}/claim")
                        if status == 200:
                            await self.serve(mode, answer["job"])
                self.report("idle" if idle else "agent-active")
            except vault.VaultError as error:
                self.last_error = str(error)
                log(self.last_error)
                self.report("stopped")
                return 1
            except MockShopApiError as error:
                self.last_error = error.message
                log(f"Marketplace: {error.message}")
                if error.status == 410:
                    # An offer was withdrawn elsewhere; publish again on the next round.
                    published_for = None
                self.report("error")
            if once:
                return 0
            await asyncio.sleep(POLL_SECONDS)

    async def prepare(self, sharing: dict[str, Any]) -> None:
        await self.sign_in()
        await self.publish(sharing)
        self.unavailable = {}
        if "script" in self.offers and not await docker_ready():
            # No unsandboxed fallback: without Docker a stranger's script is not run at all.
            self.unavailable["script"] = "Docker is not running, so scripts cannot be isolated."
        if "agent" in self.offers and self.command is None:
            self.unavailable["agent"] = "Set AUTARK_WORKER_AGENT_COMMAND to the command that answers a task read from stdin."
        for mode, reason in self.unavailable.items():
            log(f"Not serving {mode} jobs: {reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="Poll once and exit (used by tests).")
    parser.add_argument("--export-payout-key", action="store_true",
                        help="Print the payout key as a Solana keypair JSON array, to import into a wallet. Keep the output secret.")
    options = parser.parse_args()
    if options.export_payout_key:
        secret = vault.load(api_base_url()) or {}
        if not secret.get("payout_seed"):
            print("This worker has no payout key of its own on this marketplace; its sales go to a wallet the marketplace keeps.", file=sys.stderr)
            return 1
        print(json.dumps(vault.solana_keypair(secret["payout_seed"])))
        return 0
    # The buying agent's login is not this process's business.
    os.environ.pop("MOCK_SHOP_USERNAME", None)
    os.environ.pop("MOCK_SHOP_PASSWORD", None)
    try:
        return asyncio.run(Worker().run(once=options.once))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
