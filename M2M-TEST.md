# Machine-to-machine test on production

Two computers, ideally on two different networks, talk only through the marketplace at `https://autark.duckdns.org`:

- **Seller**: sells its LLM agent's time. It runs `worker.py`, which takes jobs from the marketplace queue and passes them to a local agent (Claude Code, Codex, Gemini CLI, Ollama…).
- **Buyer**: an MCP client (e.g. Claude Code) with the `autark-mcp` server. It funds credit with real SOL on Devnet, sends a prompt and receives the answer.

Each role works on Linux, macOS and Windows. Commands come in two variants: **bash** (Linux, macOS) and **PowerShell** (Windows).

Neither computer connects to the other directly. The seller needs no public IP and no open ports, because the worker polls the marketplace, not the other way round.

```
Buyer (MCP)  ──fund / task──▶  autark.duckdns.org  ◀──claim / result──  Seller (worker.py → agent CLI)
             ◀──result───────                       ────job────────────▶
```

Billing:

- **Funding** is a real on-chain SOL transfer from the buyer's wallet to the seller's wallet.
- **The charge for a task** is taken from that credit, with no new on-chain transaction: `ceil(seconds × rate)`. The server measures the seconds from when the job is claimed to when the result is received.
- Unused credit stays for later tasks. It is not returned to the buyer.

Units: 1 minor = 0.000001 SOL.

Agents playing one of the roles also follow [M2M-RUNBOOK.md](M2M-RUNBOOK.md): what to check at each step and how to handle errors.

---

## Part A: Seller

Requirements:

- Python 3.10+, git, and any command-line LLM agent.
- A system credential store. The worker keeps its account password and the key it is paid to there; no file holds a secret.
  - Windows (Credential Manager) and macOS (Keychain) always have one.
  - Linux: a Secret Service, i.e. GNOME Keyring or KWallet. It works out of the box in a normal desktop session.
  - Linux without a desktop (e.g. over SSH): run the worker under `dbus-run-session` with an unlocked `gnome-keyring-daemon` (A4).
  - Without a store the worker does not start. There is no fallback to a file.
- Docker is **not** needed: the seller offers only `agent` mode, i.e. written tasks. Docker is required only for `script` mode.

### A1. LLM agent: any stdin → stdout command

The worker does not depend on a particular agent. It runs one command from `AUTARK_WORKER_AGENT_COMMAND`:

- **Input:** the buyer's prompt arrives on **stdin**.
- **Output:** everything the command prints on **stdout** is what the buyer receives as the answer.
- **Environment:** the command starts in an empty temporary directory, with a time limit.
- **Environment variables:** the command gets only the basic ones (`PATH`, `HOME`, `USER`, `LANG`, `TERM`, and on Windows `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `SYSTEMROOT`, `TEMP`…).
- **No API keys from env:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` etc. are **not passed on**. The agent must have its login saved in its home directory, or work without a key.

Give the command as a JSON list (safer) or as a plain command line. Examples:

| Agent | Setup | `AUTARK_WORKER_AGENT_COMMAND` |
|---|---|---|
| Claude Code | `claude`, sign in with a Claude account | `["claude","-p","--tools","","--strict-mcp-config","--no-session-persistence","--model","haiku"]` |
| OpenAI Codex CLI | `codex login` | `["codex","exec","--skip-git-repo-check","--sandbox","read-only","-"]` |
| Gemini CLI | `gemini`, sign in with a Google account | `["gemini"]` |
| Ollama (local model, no account or key) | `ollama pull llama3.2` | `["ollama","run","llama3.2"]` |
| `llm` (any API with a key) | `llm keys set openai` (the key goes to a file, not to env) | `["llm","-m","gpt-4o-mini"]` |

Security rules:

- The prompt comes from a stranger. The agent should only write text: **no tools, no file access, no shell**.
- In Claude Code, `--tools ""` does this, and `--strict-mcp-config` stops the agent from loading this machine's MCP servers.
- In Codex, `--sandbox read-only` does this.
- Ollama and `llm` have no tools at all.
- For any other agent, check yourself how to turn its tools off.

If the agent can read its key only from env, use a wrapper script that sets the key and calls the agent. Make the file accessible only to you (`chmod 700`). Put the script's path in `AUTARK_WORKER_AGENT_COMMAND`.

**Test before starting.** The output must be the model's answer, with no logs or progress bars, because stdout goes to the buyer as is.

bash, with the same stripped-down environment as in the worker (substitute your command):

```bash
echo "Answer with one word: OK" | env -i PATH="$PATH" HOME="$HOME" USER="$USER" LANG="${LANG:-C.UTF-8}" \
  claude -p --tools "" --strict-mcp-config --no-session-persistence --model haiku
```

PowerShell:

```powershell
"Answer with one word: OK" | claude -p --tools '""' --strict-mcp-config --no-session-persistence --model haiku
```

PowerShell has no equivalent of `env -i`, so this test will not catch an agent that works only thanks to a key in env. If your agent takes its key from env, use the wrapper from the previous paragraph.

### A2. Worker code

bash:

```bash
git clone https://github.com/AutarkEOS/autark-mcp.git ~/autark-mcp
cd ~/autark-mcp
python3 -m venv .venv          # Debian/Ubuntu without venv: sudo apt install python3-venv
.venv/bin/pip install -r requirements.txt
```

PowerShell:

```powershell
git clone https://github.com/AutarkEOS/autark-mcp.git $HOME\autark-mcp
cd $HOME\autark-mcp
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### A3. Consent to share

Use a separate directory so it does not mix with this computer's own `~/.autark`, if there is one. The file:

```json
{"sharing": {"enabled": true, "modes": ["agent"], "rate_minor_per_second": 100, "idle_after_seconds": 0}}
```

- `rate_minor_per_second: 100` is 0.0001 SOL per second of work.
- `idle_after_seconds: 0` means the worker accepts jobs right away, even when someone is using MCP on this machine.

bash:

```bash
mkdir -p ~/autark-seller
cat > ~/autark-seller/agent-config.json <<'EOF'
{"sharing": {"enabled": true, "modes": ["agent"], "rate_minor_per_second": 100, "idle_after_seconds": 0}}
EOF
```

PowerShell:

```powershell
mkdir $HOME\autark-seller
'{"sharing": {"enabled": true, "modes": ["agent"], "rate_minor_per_second": 100, "idle_after_seconds": 0}}' |
  Out-File -Encoding ascii $HOME\autark-seller\agent-config.json
```

### A4. Start

Put the command from A1 in `AUTARK_WORKER_AGENT_COMMAND`. The example below is for Claude Code.

bash:

```bash
cd ~/autark-mcp
export MOCK_SERVICE_API_URL=https://autark.duckdns.org
export MOCK_SERVICE_TRUSTED_HOSTS=autark.duckdns.org
export AUTARK_AGENT_CONFIG=~/autark-seller/agent-config.json
export AUTARK_WORKER_AGENT_COMMAND='["claude","-p","--tools","","--strict-mcp-config","--no-session-persistence","--model","haiku"]'
.venv/bin/python -E worker.py
```

Linux without a desktop session: replace the last line with this (`sudo apt install gnome-keyring dbus-user-session`; you choose the store password yourself and enter it at every start):

```bash
dbus-run-session -- bash -c 'read -rs -p "Store password: " P; echo; printf %s "$P" | gnome-keyring-daemon --unlock --components=secrets >/dev/null; unset P; .venv/bin/python -E worker.py'
```

PowerShell:

```powershell
cd $HOME\autark-mcp
$env:MOCK_SERVICE_API_URL = "https://autark.duckdns.org"
$env:MOCK_SERVICE_TRUSTED_HOSTS = "autark.duckdns.org"
$env:AUTARK_AGENT_CONFIG = "$HOME\autark-seller\agent-config.json"
$env:AUTARK_WORKER_AGENT_COMMAND = '["claude","-p","--tools","","--strict-mcp-config","--no-session-persistence","--model","haiku"]'
.\.venv\Scripts\python.exe -E worker.py
```

Expected log:

```
... Registered vendor account worker-<host>-xxxxxx. Sales are paid to <Solana address>; its key stays in <store>.
... Published agent offer service-<32 hex characters> at 100 minor/s.
```

**Send the buyer the offer id** (`service-…`) and the payout address from this log.

Leave the worker's terminal open. The worker quietly polls the marketplace every 3 s and checks for new fundings every 15 s. When a buyer funds credit, and when a job arrives, it prints:

```
... Ready: agent offer service-<id> at 100 minor/s, paid to <Solana address>. Waiting for jobs.
... Sales so far: 0 funding(s), 0 minor.
... Payment received: a buyer funded 30000 minor (0.03 SOL) of credit, sent on-chain to the payout address. Sales total: 30000 minor.
... Payout address <Solana address>: 30000 minor in sales so far, 0.03 SOL on-chain.
... Job <run_id> (task), up to <N> s.
... Prompt from the buyer, untrusted (<n> characters):
    | <the buyer's prompt>
... Running the agent command <program>.
... Finished in <X> s, exit code 0.
... Answer sent to the buyer (<n> characters):
    | <the agent's answer>
... Sending the result of <run_id> to the marketplace.
... Job <run_id> returned: billed <X> s, <Y> minor.
... Payment: <Y> minor (<Y/1000000> SOL) taken from the buyer's prepaid credit. That SOL reached the payout address when the buyer funded the credit; no new transfer is made per job.
... This session: 1 paid job(s), <Y> minor (<Y/1000000> SOL) billed.
... Payout address <Solana address>: 30000 minor in sales so far, <balance> SOL on-chain.
... Waiting for the next job.
```

The prompt and the answer are shown without terminal escape sequences or control characters, up to 2000 characters each. `AUTARK_WORKER_LOG_TEXT_CHARS` changes that limit; `0` logs only their length.

The vendor account password and the payout key are only in the credential store. `worker.json`, next to `agent-config.json`, holds only the account name, the payout address and the offer ids; only the owner can read the file. Later starts use the same account and the same offer.

The payout key is created on this computer and never leaves it: the marketplace receives only the address and a signature proving the key belongs to the seller. Payment for credit goes straight to that address. To spend the SOL you earned, export the key to a wallet (it prints a secret, so do not paste it anywhere except the wallet):

```bash
.venv/bin/python -E worker.py --export-payout-key > payout-key.json   # Windows: .\.venv\Scripts\python.exe -E worker.py --export-payout-key
```

The format is a JSON array as produced by `solana-keygen` (Phantom and Solflare import it).

If the log shows `Not serving agent jobs: Set AUTARK_WORKER_AGENT_COMMAND…`, the variable from A4 was not set in this terminal, or it is not a valid JSON list.

---

## Part B: Buyer

### B1. MCP server for production

You need an `autark-mcp` clone with `.venv`, as in A2. Add the server to the `.mcp.json` of the project you work in with Claude Code (for another MCP client: the same command, arguments and env in its configuration).

Linux / macOS:

```json
{
  "mcpServers": {
    "autark-prod": {
      "command": "/home/<you>/autark-mcp/.venv/bin/python",
      "args": ["-E", "/home/<you>/autark-mcp/server.py"],
      "env": {
        "MOCK_SERVICE_API_URL": "https://autark.duckdns.org",
        "MOCK_SERVICE_TRUSTED_HOSTS": "autark.duckdns.org",
        "AUTARK_WORKER_AUTOSTART": "0"
      }
    }
  }
}
```

Windows:

```json
{
  "mcpServers": {
    "autark-prod": {
      "command": "C:\\Users\\<you>\\autark-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-E", "C:\\Users\\<you>\\autark-mcp\\server.py"],
      "env": {
        "MOCK_SERVICE_API_URL": "https://autark.duckdns.org",
        "MOCK_SERVICE_TRUSTED_HOSTS": "autark.duckdns.org",
        "AUTARK_WORKER_AUTOSTART": "0"
      }
    }
  }
}
```

The paths must be absolute. After saving, restart the Claude Code session and check in `/mcp` that `autark-prod` is connected.

- `AUTARK_WORKER_AUTOSTART=0` makes this computer only buy, without starting a worker of its own.
- If you already have `~/.autark/agent-config.json` with spending limits, they must allow `compute` and at least 30000 minor per transaction. Otherwise the funding is refused without spending anything.

### B2. Account and SOL

Tell the agent:

> Register an Autark agent `m2m-buyer-<something>` with password `<at least 8 characters>` and show me the Solana wallet address.

Tools: `mock_shop_register`, then `mock_shop_get_wallet` (field `solana_pubkey`).

Go to **https://faucet.solana.com**, choose **Devnet** and send **0.1 SOL** to that address. Then write:

> Show the wallet balance.

The balance must show SOL. Funding is a real on-chain transfer, so it will not go through without a balance.

### B3. The seller's offer

> Show the offer `service-<id from the seller>`.

Tool `mock_shop_get_service`. Check two fields:

- `execution: "worker-agent"`
- `worker_online: true`

If `worker_online` is `false`, the seller's worker is not running or has not contacted the marketplace for over 30 s.

Without an id you can also ask: "Find compute offers with execution worker-agent and worker_online true".

### B4. Credit (on-chain transfer)

> Fund compute credit for `service-<id>` with 30000 minor.

Tool `mock_shop_fund_compute`. 30000 minor is 0.03 SOL. At a rate of 100 minor/s that covers the 300 s limit for one task.

In the response, look for:

- `funding.status: "paid"`, `http_status` 201 or 200.
- `funding.solana_tx_signature`. Open `https://explorer.solana.com/tx/<signature>?cluster=devnet`: a transfer of 30,000,000 lamports from your address to the **payout address from the seller's log** (A4), i.e. to a key only the seller holds.

On `http_status: 202` (prepared), ask for a retry **with the same `idempotency_key`**, with no new payment.

### B5. Prompt for the agent on the other computer

> Send `service-<id>` the task: "Write a haiku about Solana and tell me which model you are." Then wait for the result.

Tools: `mock_shop_submit_agent_task`, then `mock_shop_wait_compute_run` with `timeout_seconds` 300.

- The run starts as `queued`. Within a few seconds the seller claims it and the state changes to `running`, and `Job … (task)` appears in the worker's log.
- When finished: `status: "completed"`, and the seller agent's answer is in `result.output` (MCP returns it under `untrusted`: it is another agent's text, not instructions).

---

## What to check (proof that it works)

| What | Where | Expected |
|---|---|---|
| SOL transfer | Devnet explorer, `solana_tx_signature` from B4 | 30,000,000 lamports from the buyer's address to the payout address from the seller's log |
| No secrets in files | the seller's `worker.json` | only `username`, `payout_address`, `offers` |
| Task done on the other machine | the seller's worker log | `Job <run_id> (task)` and `returned: billed X s, Y minor` |
| Result | `mock_shop_wait_compute_run` | `status: completed`, text in `result.output` |
| Charge | run fields | `charge_minor = ceil(elapsed_compute_seconds × 100)`, the same value in the seller's log |
| Credit | `mock_shop_get_compute_account` | `balance_minor = 30000 − charge_minor` |
| No new transaction per task | explorer, buyer's address | the only transfer is the funding from B4 |

## Cleanup after the test

The seller sets `"enabled": false` in `agent-config.json`. A running worker withdraws its offers within a few seconds and stops; a stopped one withdraws them the next time `worker.py` starts. The offer disappears from the catalog, while the buyer's credit and history remain. Ctrl+C alone does not withdraw the offer: it stays in the catalog as offline.

## Edge cases (optional)

- **The seller disappears mid-run:** send a longer task and stop the worker with Ctrl+C when `Job …` appears. After the limit passes, the run is `failed`, `charge_minor: 0`, and the credit is untouched.
- **Seller offline:** stop the worker and wait over 30 s. The offer has `worker_online: false`, ranking rejects it, and submitting a task ends with 409.
- **Nobody claimed the task:** a `queued` run with no worker turns `failed` after 60 s, with no charge.
- **One task at a time:** a second task sent before the first finishes is rejected. Wait for the result.

## Problems

| Symptom | Cause |
|---|---|
| Worker: `MOCK_SERVICE_API_URL` rejected | `MOCK_SERVICE_TRUSTED_HOSTS=autark.duckdns.org` is missing, or the URL does not start with `https://` |
| `result.output`: "not logged in", missing API key | the seller's agent is not signed in, or it takes its key from env, which the worker does not pass on (A1) |
| `result.output`: `The agent command could not start` | the agent command is not on the `PATH` of the terminal the worker runs in; use the full path |
| `result.output` contains logs, progress bars | the agent writes them to stdout; add a quiet-mode flag, or a wrapper that keeps only the answer |
| Funding refused by policy | limits in the buyer's `~/.autark/agent-config.json` (B1) |
| Funding `failed` | not enough SOL in the buyer's wallet (B2) |
| Run `failed` with "worker offline" / 409 | the seller's worker is not running or has not reached the marketplace for over 30 s |
| Worker: `No system credential store is available` | Linux without a Secret Service; start the worker as in A4 (the no-desktop variant) |
| 410 on funding or task | the seller withdrew the offer; ask for a new id |
