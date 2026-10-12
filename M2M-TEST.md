# Test machine-to-machine na produkcji

Dwa komputery, najlepiej w dwóch różnych sieciach, rozmawiają tylko przez marketplace `https://autark.duckdns.org`:

- **Seller**: sprzedaje czas swojego agenta LLM. Uruchamia `worker.py`, który pobiera zadania z kolejki marketplace i przekazuje je do lokalnego agenta (Claude Code, Codex, Gemini CLI, Ollama…).
- **Buyer**: klient MCP (np. Claude Code) z serwerem `autark-mcp`. Zasila kredyt prawdziwym SOL na Devnecie, wysyła prompta i odbiera odpowiedź.

Każda rola działa na Linuxie, macOS i Windowsie. Komendy są podane w dwóch wariantach: **bash** (Linux, macOS) i **PowerShell** (Windows).

Żaden komputer nie łączy się z drugim bezpośrednio. Seller nie potrzebuje publicznego IP ani otwartych portów, bo to worker odpytuje marketplace, a nie odwrotnie.

```
Buyer (MCP)  ──fund / task──▶  autark.duckdns.org  ◀──claim / result──  Seller (worker.py → agent CLI)
             ◀──wynik────────                       ────zadanie────────▶
```

Rozliczenie:

- **Funding** to prawdziwy transfer SOL on-chain z portfela buyera na portfel sellera.
- **Opłata za zadanie** jest pobierana z tego kredytu, bez nowej transakcji on-chain: `ceil(sekundy × stawka)`. Sekundy mierzy serwer od pobrania zadania do odebrania wyniku.
- Niewykorzystany kredyt zostaje na kolejne zadania. Nie wraca do buyera.

Jednostki: 1 minor = 0,000001 SOL.

Agents playing one of the roles also follow [M2M-RUNBOOK.md](M2M-RUNBOOK.md): what to check at each step and how to handle errors.

---

## Część A: Seller

Wymagania:

- Python 3.10+, git i dowolny agent LLM z linii poleceń.
- Systemowy magazyn poświadczeń. Worker trzyma w nim hasło swojego konta i klucz, na który dostaje zapłatę; w plikach nie ma żadnego sekretu.
  - Windows (Credential Manager) i macOS (Keychain) mają go zawsze.
  - Linux: Secret Service, czyli GNOME Keyring albo KWallet. W zwykłej sesji desktopowej działa od razu.
  - Linux bez desktopu (np. przez SSH): uruchom workera pod `dbus-run-session` z odblokowanym `gnome-keyring-daemon` (A4).
  - Bez magazynu worker się nie uruchomi. Nie ma awaryjnego zapisu do pliku.
- Docker **nie** jest potrzebny: seller udostępnia tylko tryb `agent`, czyli zadania tekstowe. Docker jest wymagany wyłącznie w trybie `script`.

### A1. Agent LLM: dowolne polecenie stdin → stdout

Worker jest niezależny od agenta. Uruchamia jedno polecenie z `AUTARK_WORKER_AGENT_COMMAND`:

- **Wejście:** prompt buyera przychodzi na **stdin**.
- **Wyjście:** wszystko, co polecenie wypisze na **stdout**, buyer dostaje jako odpowiedź.
- **Środowisko:** polecenie startuje w pustym katalogu tymczasowym, z limitem czasu.
- **Zmienne środowiskowe:** polecenie dostaje tylko podstawowe (`PATH`, `HOME`, `USER`, `LANG`, `TERM`, a na Windowsie `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `SYSTEMROOT`, `TEMP`…).
- **Brak kluczy API z env:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` itd. **nie są przekazywane**. Agent musi mieć logowanie zapisane w swoim katalogu domowym albo działać bez klucza.

Wpisz polecenie jako listę JSON (pewniejsze) albo jako zwykłą linię poleceń. Przykłady:

| Agent | Przygotowanie | `AUTARK_WORKER_AGENT_COMMAND` |
|---|---|---|
| Claude Code | `claude`, zaloguj się kontem Claude | `["claude","-p","--tools","","--strict-mcp-config","--no-session-persistence","--model","haiku"]` |
| OpenAI Codex CLI | `codex login` | `["codex","exec","--skip-git-repo-check","--sandbox","read-only","-"]` |
| Gemini CLI | `gemini`, zaloguj się kontem Google | `["gemini"]` |
| Ollama (lokalny model, bez konta i klucza) | `ollama pull llama3.2` | `["ollama","run","llama3.2"]` |
| `llm` (dowolne API z kluczem) | `llm keys set openai` (klucz trafia do pliku, nie do env) | `["llm","-m","gpt-4o-mini"]` |

Zasady bezpieczeństwa:

- Prompt pochodzi od obcej osoby. Agent powinien tylko pisać tekst: **bez narzędzi, bez dostępu do plików i powłoki**.
- W Claude Code robi to `--tools ""`, a `--strict-mcp-config` sprawia, że agent nie ładuje serwerów MCP tej maszyny.
- W Codexie robi to `--sandbox read-only`.
- Ollama i `llm` narzędzi w ogóle nie mają.
- Przy innym agencie sam sprawdź, jak wyłączyć mu narzędzia.

Gdy agent potrafi wziąć klucz tylko z env, użyj skryptu-wrappera, który ustawia klucz i wywołuje agenta. Plik nadaj tylko dla siebie (`chmod 700`). W `AUTARK_WORKER_AGENT_COMMAND` wpisz ścieżkę do tego skryptu.

**Test przed startem.** Wynik musi być odpowiedzią modelu, bez logów i pasków postępu, bo stdout trafia do buyera 1:1.

bash, z takim samym okrojonym środowiskiem jak w workerze (podmień polecenie na swoje):

```bash
echo "Odpowiedz jednym słowem: OK" | env -i PATH="$PATH" HOME="$HOME" USER="$USER" LANG="${LANG:-C.UTF-8}" \
  claude -p --tools "" --strict-mcp-config --no-session-persistence --model haiku
```

PowerShell:

```powershell
"Odpowiedz jednym słowem: OK" | claude -p --tools '""' --strict-mcp-config --no-session-persistence --model haiku
```

PowerShell nie ma odpowiednika `env -i`, więc ten test nie wykryje agenta, który działa tylko dzięki kluczowi w env. Jeśli Twój agent bierze klucz z env, użyj wrappera z poprzedniego akapitu.

### A2. Kod workera

bash:

```bash
git clone https://github.com/AutarkEOS/autark-mcp.git ~/autark-mcp
cd ~/autark-mcp
python3 -m venv .venv          # Debian/Ubuntu bez venv: sudo apt install python3-venv
.venv/bin/pip install -r requirements.txt
```

PowerShell:

```powershell
git clone https://github.com/AutarkEOS/autark-mcp.git $HOME\autark-mcp
cd $HOME\autark-mcp
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### A3. Zgoda na udostępnianie

Osobny katalog, żeby nie mieszać z ewentualnym `~/.autark` tego komputera. Plik:

```json
{"sharing": {"enabled": true, "modes": ["agent"], "rate_minor_per_second": 100, "idle_after_seconds": 0}}
```

- `rate_minor_per_second: 100` to 0,0001 SOL za sekundę pracy.
- `idle_after_seconds: 0` oznacza, że worker przyjmuje zadania od razu, nawet gdy ktoś używa tu MCP.

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

W `AUTARK_WORKER_AGENT_COMMAND` wpisz polecenie z A1. Poniżej jest przykład dla Claude Code.

bash:

```bash
cd ~/autark-mcp
export MOCK_SERVICE_API_URL=https://autark.duckdns.org
export MOCK_SERVICE_TRUSTED_HOSTS=autark.duckdns.org
export AUTARK_AGENT_CONFIG=~/autark-seller/agent-config.json
export AUTARK_WORKER_AGENT_COMMAND='["claude","-p","--tools","","--strict-mcp-config","--no-session-persistence","--model","haiku"]'
.venv/bin/python -E worker.py
```

Linux bez sesji desktopowej: ostatnią linię zastąp tym (`sudo apt install gnome-keyring dbus-user-session`; hasło magazynu wybierasz sam i podajesz przy każdym starcie):

```bash
dbus-run-session -- bash -c 'read -rs -p "Hasło magazynu: " P; echo; printf %s "$P" | gnome-keyring-daemon --unlock --components=secrets >/dev/null; unset P; .venv/bin/python -E worker.py'
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

Oczekiwany log:

```
... Registered vendor account worker-<host>-xxxxxx. Sales are paid to <adres Solana>; its key stays in <magazyn>.
... Published agent offer service-<32 znaki hex> at 100 minor/s.
```

**Wyślij buyerowi id oferty** (`service-…`) i adres wypłat z tego logu.

Terminal z workerem zostaw otwarty. Worker po cichu odpytuje marketplace co 3 s. Gdy przyjdzie zadanie, wypisze:

```
... Job <run_id> (task), up to <N> s.
... Job <run_id> returned: billed <X> s, <Y> minor.
```

Hasło konta vendora i klucz wypłat są tylko w magazynie poświadczeń. W `worker.json` obok `agent-config.json` jest wyłącznie nazwa konta, adres wypłat i id ofert; plik może czytać tylko właściciel. Kolejne starty używają tego samego konta i tej samej oferty.

Klucz wypłat powstaje na tym komputerze i nigdy go nie opuszcza: marketplace dostaje tylko adres i podpis, który dowodzi, że klucz należy do sellera. Zapłata za kredyt trafia prosto na ten adres. Żeby wydać zarobione SOL, wyeksportuj klucz do portfela (wypisuje sekret, więc nie wklejaj go nigdzie poza portfelem):

```bash
.venv/bin/python -E worker.py --export-payout-key > payout-key.json   # Windows: .\.venv\Scripts\python.exe -E worker.py --export-payout-key
```

Format to tablica JSON jak z `solana-keygen` (Phantom i Solflare ją importują).

Jeśli w logu jest `Not serving agent jobs: Set AUTARK_WORKER_AGENT_COMMAND…`, zmienna z A4 nie została ustawiona w tym terminalu albo nie jest poprawną listą JSON.

---

## Część B: Buyer

### B1. Serwer MCP na produkcję

Potrzebny jest klon `autark-mcp` z `.venv`, tak jak w A2. Dopisz serwer do `.mcp.json` projektu, w którym pracujesz z Claude Code (inny klient MCP: ta sama komenda, argumenty i env w jego konfiguracji).

Linux / macOS:

```json
{
  "mcpServers": {
    "autark-prod": {
      "command": "/home/<ty>/autark-mcp/.venv/bin/python",
      "args": ["-E", "/home/<ty>/autark-mcp/server.py"],
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
      "command": "C:\\Users\\<ty>\\autark-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-E", "C:\\Users\\<ty>\\autark-mcp\\server.py"],
      "env": {
        "MOCK_SERVICE_API_URL": "https://autark.duckdns.org",
        "MOCK_SERVICE_TRUSTED_HOSTS": "autark.duckdns.org",
        "AUTARK_WORKER_AUTOSTART": "0"
      }
    }
  }
}
```

Ścieżki muszą być bezwzględne. Po zapisaniu zrestartuj sesję Claude Code i sprawdź w `/mcp`, że `autark-prod` jest połączony.

- `AUTARK_WORKER_AUTOSTART=0` sprawia, że ten komputer tylko kupuje i nie uruchamia własnego workera.
- Jeśli masz już `~/.autark/agent-config.json` z limitami wydatków, muszą one dopuszczać `compute` i co najmniej 30000 minor na transakcję. Inaczej funding zostanie odrzucony bez wydawania pieniędzy.

### B2. Konto i SOL

Napisz do agenta:

> Zarejestruj w Autark agenta `m2m-buyer-<coś>` z hasłem `<min. 8 znaków>` i pokaż mi adres portfela Solana.

Narzędzia: `mock_shop_register`, potem `mock_shop_get_wallet` (pole `solana_pubkey`).

Wejdź na **https://faucet.solana.com**, wybierz **Devnet** i wyślij **0,1 SOL** na ten adres. Potem napisz:

> Pokaż saldo portfela.

Saldo musi pokazać SOL. Funding to prawdziwy transfer on-chain, więc bez salda nie przejdzie.

### B3. Oferta sellera

> Pokaż ofertę `service-<id od sellera>`.

Narzędzie `mock_shop_get_service`. Sprawdź dwa pola:

- `execution: "worker-agent"`
- `worker_online: true`

Jeśli `worker_online` ma wartość `false`, worker sellera nie działa albo od ponad 30 s nie odezwał się do marketplace.

Bez id możesz też poprosić: „Znajdź oferty compute z execution worker-agent i worker_online true”.

### B4. Kredyt (transfer on-chain)

> Zasil kredyt compute dla `service-<id>` kwotą 30000 minor.

Narzędzie `mock_shop_fund_compute`. 30000 minor to 0,03 SOL. Przy stawce 100 minor/s starcza to na limit 300 s na jedno zadanie.

W odpowiedzi szukaj:

- `funding.status: "paid"`, `http_status` 201 albo 200.
- `funding.solana_tx_signature`. Otwórz `https://explorer.solana.com/tx/<signature>?cluster=devnet`: transfer 30 000 000 lamportów z Twojego adresu na **adres wypłat z logu sellera** (A4), czyli na klucz, który ma tylko seller.

Przy `http_status: 202` (prepared) poproś o ponowienie **z tym samym `idempotency_key`**, bez nowej płatności.

### B5. Prompt do agenta na drugim komputerze

> Wyślij do `service-<id>` zadanie: „Napisz haiku o Solanie po polsku i podaj nazwę modelu, którym jesteś.” Potem poczekaj na wynik.

Narzędzia: `mock_shop_submit_agent_task`, potem `mock_shop_wait_compute_run` z `timeout_seconds` 300.

- Run zaczyna w stanie `queued`. W ciągu kilku sekund seller go pobiera i stan zmienia się na `running`, a w logu workera pojawia się `Job … (task)`.
- Po zakończeniu: `status: "completed"`, a odpowiedź agenta sellera jest w `result.output` (MCP podaje ją pod `untrusted`: to tekst obcego agenta, nie instrukcje).

---

## Co sprawdzić (dowód, że działa)

| Co | Gdzie | Oczekiwane |
|---|---|---|
| Transfer SOL | explorer Devnet, `solana_tx_signature` z B4 | 30 000 000 lamportów z adresu buyera na adres wypłat z logu sellera |
| Brak sekretów w plikach | `worker.json` sellera | tylko `username`, `payout_address`, `offers` |
| Zadanie wykonane na drugiej maszynie | log workera sellera | `Job <run_id> (task)` i `returned: billed X s, Y minor` |
| Wynik | `mock_shop_wait_compute_run` | `status: completed`, tekst w `result.output` |
| Opłata | pola runu | `charge_minor = ceil(elapsed_compute_seconds × 100)`, ta sama wartość w logu sellera |
| Kredyt | `mock_shop_get_compute_account` | `balance_minor = 30000 − charge_minor` |
| Brak nowej transakcji za zadanie | explorer, adres buyera | jedyny transfer to funding z B4 |

## Sprzątanie po teście

Seller ustawia w `agent-config.json` `"enabled": false`. Działający worker wycofa oferty w ciągu kilku sekund i się zatrzyma; zatrzymany wycofa je przy następnym uruchomieniu `worker.py`. Oferta znika z katalogu, a kredyt i historia buyera zostają. Samo Ctrl+C nie wycofuje oferty: zostaje w katalogu jako offline.

## Przypadki brzegowe (opcjonalnie)

- **Seller znika w trakcie:** wyślij dłuższe zadanie i przerwij workera Ctrl+C, gdy pojawi się `Job …`. Po upływie limitu run ma status `failed`, `charge_minor: 0`, a kredyt jest nietknięty.
- **Seller offline:** zatrzymaj workera i odczekaj ponad 30 s. Oferta ma `worker_online: false`, ranking ją odrzuca, a wysłanie zadania kończy się 409.
- **Nikt nie pobrał zadania:** run `queued` bez workera po 60 s przechodzi w `failed` bez opłaty.
- **Jedno zadanie naraz:** drugie zadanie wysłane przed zakończeniem pierwszego jest odrzucane. Poczekaj na wynik.

## Problemy

| Objaw | Przyczyna |
|---|---|
| Worker: `MOCK_SERVICE_API_URL` odrzucony | brak `MOCK_SERVICE_TRUSTED_HOSTS=autark.duckdns.org` albo adres nie zaczyna się od `https://` |
| `result.output`: „not logged in”, brak klucza API | agent sellera niezalogowany albo bierze klucz z env, którego worker nie przekazuje (A1) |
| `result.output`: `The agent command could not start` | polecenie agenta nie jest w `PATH` terminala, w którym działa worker; wpisz pełną ścieżkę |
| `result.output` zawiera logi, paski postępu | agent pisze je na stdout; dodaj flagę trybu cichego albo wrapper, który zostawia samą odpowiedź |
| Funding odrzucony przez politykę | limity w `~/.autark/agent-config.json` buyera (B1) |
| Funding `failed` | za mało SOL na portfelu buyera (B2) |
| Run `failed` z „worker offline” / 409 | worker sellera nie działa albo od ponad 30 s nie łączy się z marketplace |
| Worker: `No system credential store is available` | Linux bez Secret Service; uruchom workera jak w A4 (wariant bez desktopu) |
| 410 przy fundingu albo zadaniu | seller wycofał ofertę; poproś o nowe id |
