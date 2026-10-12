#!/usr/bin/env python3
"""Sell this machine's idle AI agent on the Autark marketplace with one command.

    python3 seller.py --agent-command '["claude","-p","--tools","","--strict-mcp-config","--no-session-persistence","--model","haiku"]'
    py -3 seller.py --agent-command "..."        (Windows)

Without --agent-command it uses AUTARK_WORKER_AGENT_COMMAND, or that Claude Code command when `claude` is installed.

In order, it:
1. creates .venv and installs requirements.txt (again only when the file changes);
2. on the first run, writes the consent to share agent tasks to ~/autark-seller/agent-config.json;
3. checks the system credential store, where the worker keeps its login and payout key;
4. sends a test prompt through the agent command, the way the worker will run it;
5. starts worker.py in the foreground. On its first start the worker registers a vendor account
   that is paid to a key made on this machine, and publishes one `agent` offer.

Stop selling: set "enabled": false in the agent config, then run this again. Ctrl+C alone leaves the offer listed as offline.
Consent stays a human decision: this script writes it only when no config exists, and never turns sharing back on.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
DEFAULT_API_URL = "https://autark.duckdns.org"
DEFAULT_CONFIG = Path("~/autark-seller/agent-config.json")
CLAUDE_COMMAND = ["claude", "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--model", "haiku"]
TEST_PROMPT = b"Answer with one word: OK"


class SellerError(Exception):
    pass


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def prepare_venv() -> None:
    """The worker's dependencies, installed once and again only when requirements.txt changes."""
    if not venv_python().exists():
        print(f"Creating {VENV} ...", flush=True)
        if subprocess.call([sys.executable, "-m", "venv", str(VENV)]) != 0:
            raise SellerError("Could not create .venv. On Debian/Ubuntu: sudo apt install python3-venv")
    requirements = ROOT / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = VENV / "autark-requirements.sha256"
    if not marker.exists() or marker.read_text(encoding="utf-8").strip() != digest:
        print("Installing requirements ...", flush=True)
        if subprocess.call([str(venv_python()), "-E", "-m", "pip", "install", "-q", "-r", str(requirements)]) != 0:
            raise SellerError("pip could not install requirements.txt; see the output above.")
        marker.write_text(digest + "\n", encoding="utf-8")


def ensure_consent(path: Path, rate: int) -> str:
    """Write the consent to share agent tasks on the first run; afterwards only read it."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        sharing = {"enabled": True, "modes": ["agent"], "rate_minor_per_second": rate, "idle_after_seconds": 0}
        path.write_text(json.dumps({"sharing": sharing}) + "\n", encoding="utf-8")
        return f"Wrote the consent to {path}: agent tasks at {rate} minor/s. Set \"enabled\": false there to stop selling."
    from agent_config import AgentConfigError, load_config
    try:
        sharing = (load_config() or {}).get("sharing")
    except AgentConfigError as error:
        raise SellerError(f"{path} is unreadable: {error}") from None
    if not sharing or not sharing["enabled"]:
        raise SellerError(f"Sharing is off in {path}. Only a human turns it on: set \"enabled\": true there, then run this again.")
    if "agent" not in sharing["modes"]:
        raise SellerError(f"{path} does not share agent tasks. Only a human adds a mode: put \"agent\" in sharing.modes, then run this again.")
    return f"Using the consent in {path}: {', '.join(sharing['modes'])} at {sharing['rate_minor_per_second']} minor/s."


def choose_agent_command(argument: str | None, environ: dict[str, str], which=shutil.which) -> str:
    """The command that answers a task on stdin: the argument, then the environment, then Claude Code if installed."""
    if argument:
        return argument
    if environ.get("AUTARK_WORKER_AGENT_COMMAND", "").strip():
        return environ["AUTARK_WORKER_AGENT_COMMAND"]
    if which("claude"):
        return json.dumps(CLAUDE_COMMAND)
    raise SellerError("No agent command. Pass --agent-command with a command that reads a task on stdin and prints only the "
                      "answer, without tools, e.g. '[\"ollama\",\"run\",\"llama3.2\"]'. More examples: M2M-TEST.md, A1.")


def check_credential_store() -> str:
    import vault
    try:
        return vault.store_name()
    except vault.VaultError as error:
        hint = ("\nWithout a desktop session, run it under an unlocked GNOME Keyring:\n  dbus-run-session -- bash -c "
                "'read -rs -p \"Store password: \" P; echo; printf %s \"$P\" | gnome-keyring-daemon --unlock --components=secrets "
                ">/dev/null; unset P; python3 seller.py'" if sys.platform.startswith("linux") else "")
        raise SellerError(f"{error}{hint}") from None


def test_agent_command() -> None:
    """One prompt through the same runner, environment and empty directory a buyer's task gets."""
    import worker
    command = worker.agent_command()
    if command is None:
        raise SellerError("AUTARK_WORKER_AGENT_COMMAND is not a JSON list of strings or a command line.")
    print(f"Testing the agent command {Path(command[0]).name} ...", flush=True)
    result = asyncio.run(worker.run_task(TEST_PROMPT, 180, command))
    worker.log_text("Test answer", result["output"])
    if result["exit_code"] != 0 or result["timed_out"] or not result["output"].strip():
        raise SellerError("The agent command did not answer the test prompt. Buyers would get this output. Fix the command "
                          "(signed in? on PATH? quiet output?) or pass --skip-agent-test to start anyway.")


def marketplace_environment(api_url: str) -> dict[str, str]:
    """A remote marketplace is trusted by name and needs https; a local one needs nothing extra."""
    host = (urlsplit(api_url).hostname or "").lower()
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    environment = {"MOCK_SERVICE_API_URL": api_url}
    if not local:
        environment["MOCK_SERVICE_TRUSTED_HOSTS"] = host
    return environment


def run(arguments: argparse.Namespace) -> int:
    config = arguments.config.expanduser().resolve()
    os.environ.update({
        **marketplace_environment(arguments.api_url),
        "AUTARK_AGENT_CONFIG": str(config),
        "AUTARK_WORKER_AGENT_COMMAND": choose_agent_command(arguments.agent_command, dict(os.environ)),
    })
    from settings import ConfigError, api_base_url
    try:
        api_base_url()
    except ConfigError as error:
        raise SellerError(f"--api-url {arguments.api_url}: {error}") from None
    print(ensure_consent(config, arguments.rate), flush=True)
    print(f"Credential store: {check_credential_store()}", flush=True)
    if not arguments.skip_agent_test:
        test_agent_command()
    print(f"Starting the worker against {arguments.api_url}. Leave this terminal open; Ctrl+C stops it.", flush=True)
    import worker
    sys.argv = [str(ROOT / "worker.py")]
    return worker.main()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-command", help="JSON list of arguments, or a command line, that answers a task read from stdin.")
    parser.add_argument("--rate", type=int, default=100, help="Price in minor units per second, used only when the consent is first written. Default 100.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Agent config of the seller. Default ~/autark-seller/agent-config.json.")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help=f"Marketplace URL. Default {DEFAULT_API_URL}.")
    parser.add_argument("--skip-agent-test", action="store_true", help="Start without sending the test prompt.")
    arguments = parser.parse_args()
    try:
        if not in_venv():
            if sys.version_info < (3, 10):
                raise SellerError("Python 3.10 or newer is needed.")
            prepare_venv()
            child = subprocess.Popen([str(venv_python()), "-E", str(Path(__file__).resolve()), *sys.argv[1:]])
            while True:
                try:
                    return child.wait()
                except KeyboardInterrupt:
                    pass  # The worker gets the same Ctrl+C and exits on its own.
        if arguments.rate < 1:
            raise SellerError("--rate must be at least 1.")
        return run(arguments)
    except SellerError as error:
        print(f"seller.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
