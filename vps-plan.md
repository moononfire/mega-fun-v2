# VPS Worker — Plan aplikacji serwerowej

## Rola w systemie

VPS Worker to **cichy wykonawca** — nie ma UI, nie jest widoczny dla klientów ani agency-platform.
Jedynym jego rozmówcą jest Next.js na Vercelu.

Jego odpowiedzialności:
1. Zakładanie i zarządzanie strukturą folderów per klient
2. Odpalanie skryptów Python z odpowiednim `client_slug`
3. Zbieranie logów i plików wynikowych
4. Informowanie Next.js (webhook) o zakończeniu runy
5. Serwowanie logów i plików na żądanie Next.js

VPS Worker **nie sprawdza tożsamości klientów** — to rola Next.js.
Worker tylko weryfikuje, że request pochodzi od Next.js (shared token).

---

## Stack technologiczny

| Warstwa | Technologia | Powód wyboru |
|---------|-------------|--------------|
| Framework | FastAPI (Python) | Ten sam język co skrypty, async, szybkie API |
| Serwer ASGI | Uvicorn | Standardowy dla FastAPI |
| Zarządzanie procesami | asyncio.create_subprocess_exec | Async spawn bez blokowania |
| Przechowywanie stanu runów | SQLite (lokalna, tylko dla workera) | Proste, bez zewnętrznych zależności |
| Auth | Bearer token w nagłówku | Shared secret z Next.js |
| Process manager | systemd | Restart przy awarii, logi do journald |
| Reverse proxy | Nginx | TLS termination, nagłówki |

**Dlaczego FastAPI a nie Express/Node?**
Skrypty są w Pythonie, FastAPI jest w Pythonie — jeden język na VPS, łatwiejsze debugowanie,
te same biblioteki dostępne. Nie trzeba instalować Node.js na VPS do workera.

---

## Struktura folderów na VPS

```
/home/deploy/
  worker/                    ← kod FastAPI Worker
    main.py
    db.py
    runner.py
    requirements.txt
    .env

  scripts/                   ← skrypty Python (istniejące + nowe)
    scrape_google_maps.py
    send_campaign.py
    scrape_emails.py
    ...

  clients/                   ← dane per klient (tworzone przez Worker API)
    {client_slug}/
      input/                 ← parametry/dane wejściowe dla skryptów
      output/                ← pliki wynikowe (CSV, JSON, etc.)
      logs/                  ← logi z każdego runa
      config.json            ← ustawienia klienta (API keys, SMTP, limity)

  worker.db                  ← SQLite Workera (runs, clients_local)
```

---

## Schemat bazy SQLite Workera

Worker ma własną małą bazę — tylko do śledzenia stanu uruchomionych procesów.
**Nie jest to główna baza danych systemu** — ta jest w Neon Postgres na Vercelu.

```sql
CREATE TABLE local_runs (
  id          TEXT PRIMARY KEY,      -- vpsRunId zwracany Next.js
  run_id      TEXT NOT NULL,         -- runId z Next.js (do webhook)
  client_slug TEXT NOT NULL,
  script      TEXT NOT NULL,
  status      TEXT DEFAULT 'running', -- running | done | error
  pid         INTEGER,               -- PID procesu Python
  log_path    TEXT,                  -- ścieżka do pliku logu
  output_files TEXT DEFAULT '[]',    -- JSON array ścieżek
  error_msg   TEXT,
  started_at  TEXT DEFAULT (datetime('now')),
  finished_at TEXT
);

CREATE TABLE local_clients (
  slug        TEXT PRIMARY KEY,
  status      TEXT DEFAULT 'active', -- active | suspended
  created_at  TEXT DEFAULT (datetime('now'))
);
```

---

## Worker API — endpointy

Wszystkie endpointy wymagają nagłówka: `Authorization: Bearer <VPS_WORKER_TOKEN>`

### POST `/clients`
Zakłada strukturę folderów dla nowego klienta.

Request body:
```json
{
  "slug": "firma-xyz",
  "name": "Firma XYZ",
  "config": {}
}
```

Flow:
1. Walidacja: czy slug nie istnieje już w `local_clients`
2. `mkdir -p /home/deploy/clients/{slug}/{input,output,logs}`
3. Zapis `config.json` z danymi klienta
4. INSERT do `local_clients`
5. Odpowiedź `201`

---

### DELETE `/clients/{slug}`
Usuwa folder klienta i rekord w local_clients.

Zabezpieczenie: sprawdza czy nie ma aktywnych runów (`status='running'`) dla klienta.
Jeśli tak — zwraca `409 Conflict`.

---

### PATCH `/clients/{slug}`
Aktualizuje status lub config klienta.

Request body:
```json
{
  "status": "suspended",
  "config": { "smtpLimit": 50 }
}
```

Jeśli `status: "suspended"` — Worker odrzuci nowe `/run` requesty dla tego klienta.

---

### POST `/run`
Uruchamia skrypt dla klienta. **Kluczowy endpoint.**

Request body:
```json
{
  "runId": "uuid-z-next-js",
  "clientSlug": "firma-xyz",
  "script": "send_campaign",
  "params": { "campaignId": "abc", "limit": 50 },
  "webhookUrl": "https://runner.riskydev.com/api/webhooks/run-complete"
}
```

Flow:
1. Sprawdź czy klient istnieje i status = 'active'
2. Sprawdź czy skrypt istnieje w `/home/deploy/scripts/{script}.py`
3. Wygeneruj `vpsRunId` (UUID)
4. INSERT do `local_runs` (status='running')
5. **Odpowiedz natychmiast** `202 Accepted` z `{ vpsRunId }`
6. W tle (asyncio.create_task): uruchom skrypt i obserwuj
7. Po zakończeniu: wywołaj webhook do Next.js

Response `202`:
```json
{ "vpsRunId": "vps-uuid-abc123" }
```

---

### GET `/runs/{vpsRunId}`
Status runa — używane przez Next.js do ewentualnego ręcznego sprawdzenia.

Response:
```json
{
  "vpsRunId": "...",
  "status": "done",
  "outputFiles": ["output/results.csv"],
  "errorMessage": null,
  "startedAt": "...",
  "finishedAt": "..."
}
```

---

### GET `/runs/{vpsRunId}/logs`
Zwraca zawartość pliku logu. Używane przez Next.js żeby pokazać logi klientowi.

Query params: `?tail=100` — ostatnie N linii (domyślnie wszystkie).

Response: plain text (Content-Type: text/plain)

---

### GET `/clients/{slug}/files`
Lista plików w `output/` klienta.

Response:
```json
[
  { "name": "results_2026-06-11.csv", "sizeBytes": 45231, "modifiedAt": "..." },
  { "name": "report.json", "sizeBytes": 1204, "modifiedAt": "..." }
]
```

---

### GET `/clients/{slug}/files/{filename}`
Pobieranie pliku. Next.js proxy — klient nigdy nie zna adresu VPS.

Zwraca plik jako `application/octet-stream` z `Content-Disposition: attachment`.

---

### DELETE `/clients/{slug}/files/{filename}`
Usuwa plik z `output/`.

---

### GET `/health`
Stan VPS — odpytywany przez `/api/admin/health` w Next.js.

Response:
```json
{
  "status": "ok",
  "diskUsedGB": 4.2,
  "diskFreeGB": 20.1,
  "activeClients": 12,
  "runningScripts": 2,
  "uptimeSeconds": 86400
}
```

---

## Uruchamianie skryptów Python — szczegóły

### Kod runnera (`runner.py`)

```python
import asyncio, json, os, uuid
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path("/home/deploy/scripts")
CLIENTS_DIR = Path("/home/deploy/clients")

async def execute_run(vps_run_id, run_id, client_slug, script, params, webhook_url, db):
    log_path = CLIENTS_DIR / client_slug / "logs" / f"{vps_run_id}.log"
    output_dir = CLIENTS_DIR / client_slug / "output"

    # Budowanie argumentów: script.py client_slug key=value key=value ...
    args = ["python3", str(SCRIPTS_DIR / f"{script}.py"), client_slug]
    for key, value in params.items():
        args.append(f"{key}={value}")

    output_files = []
    error_msg = None

    try:
        with open(log_path, "w") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(CLIENTS_DIR / client_slug)
            )
            db.update_run_pid(vps_run_id, proc.pid)
            await proc.wait()

        if proc.returncode == 0:
            # Zbierz pliki output utworzone po starcie runa
            output_files = [
                f.name for f in output_dir.iterdir()
                if f.is_file() and f.stat().st_mtime > start_time
            ]
            status = "done"
        else:
            status = "error"
            error_msg = f"Exit code: {proc.returncode}"

    except Exception as e:
        status = "error"
        error_msg = str(e)

    # Aktualizuj lokalną bazę
    db.finish_run(vps_run_id, status, output_files, error_msg)

    # Wyślij webhook do Next.js
    await notify_nextjs(webhook_url, run_id, vps_run_id, status, output_files, error_msg)
```

### Webhook do Next.js

```python
async def notify_nextjs(webhook_url, run_id, vps_run_id, status, output_files, error_msg):
    payload = {
        "runId": run_id,
        "vpsRunId": vps_run_id,
        "status": status,
        "outputFiles": output_files,
        "errorMessage": error_msg,
        "finishedAt": datetime.utcnow().isoformat()
    }
    # 3 próby z backoff
    for attempt in range(3):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    webhook_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {VPS_WORKER_TOKEN}"},
                    timeout=10
                )
                if r.status_code == 200:
                    return
        except Exception:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
```

---

## Zmiany w skryptach Python

Każdy skrypt musi przyjmować `client_slug` jako **pierwszy argument** i budować ścieżki przez niego.
To **jedyna wymagana zmiana** — logika skryptów pozostaje bez zmian.

```python
# === DODAĆ NA POCZĄTKU KAŻDEGO SKRYPTU ===
import sys, os
from pathlib import Path

CLIENT_SLUG = sys.argv[1]
BASE_DIR    = Path(f"/home/deploy/clients/{CLIENT_SLUG}")
INPUT_DIR   = BASE_DIR / "input"
OUTPUT_DIR  = BASE_DIR / "output"
LOG_DIR     = BASE_DIR / "logs"

# Parametry dodatkowe (np. query=restauracje, limit=100)
# Przekazywane jako key=value w argv[2:]
params = dict(arg.split("=", 1) for arg in sys.argv[2:] if "=" in arg)
# === KONIEC DODATKU ===

# reszta skryptu bez zmian...
```

---

## Zmienne środowiskowe Worker (`.env`)

```env
VPS_WORKER_TOKEN=<ten sam secret co w Next.js VPS_WORKER_TOKEN>
CLIENTS_DIR=/home/deploy/clients
SCRIPTS_DIR=/home/deploy/scripts
DATABASE_PATH=/home/deploy/worker.db
PORT=8001
```

---

## Konfiguracja systemd

Plik `/etc/systemd/system/mega-fun-worker.service`:

```ini
[Unit]
Description=Mega-Fun VPS Worker
After=network.target

[Service]
User=deploy
WorkingDirectory=/home/deploy/worker
ExecStart=/home/deploy/worker/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5
EnvironmentFile=/home/deploy/worker/.env

[Install]
WantedBy=multi-user.target
```

Worker słucha tylko na `127.0.0.1` — Nginx przekazuje ruch z zewnątrz po weryfikacji.

---

## Konfiguracja Nginx

```nginx
server {
    listen 443 ssl;
    server_name runner-worker.riskydev.com;

    # TLS cert (Let's Encrypt)
    ssl_certificate /etc/letsencrypt/live/runner-worker.riskydev.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/runner-worker.riskydev.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # Timeout dla długich runów — skrypt może działać kilka minut
        proxy_read_timeout 30s;
        proxy_connect_timeout 10s;
    }
}
```

---

## Fazy realizacji

### Faza 1 — Setup Worker (2h)
1. Utwórz folder `/home/deploy/worker/`
2. `python3 -m venv venv && pip install fastapi uvicorn httpx`
3. `main.py` — szkielet FastAPI z middleware auth (weryfikacja Bearer tokena)
4. `db.py` — inicjalizacja SQLite, funkcje CRUD
5. Endpoint `GET /health` — zwraca dysk + uptime
6. Konfiguracja systemd + Nginx
7. Test: `curl https://runner-worker.riskydev.com/health -H "Authorization: Bearer TOKEN"`

### Faza 2 — Zarządzanie klientami (1-2h)
1. `POST /clients` — mkdir + config.json + local_clients
2. `DELETE /clients/{slug}` — rmtree + sprawdzenie aktywnych runów
3. `PATCH /clients/{slug}` — update statusu i config.json
4. `GET /clients/{slug}/files` — lista plików output
5. `GET /clients/{slug}/files/{filename}` — streaming pliku
6. `DELETE /clients/{slug}/files/{filename}` — usunięcie

### Faza 3 — Uruchamianie skryptów (2-3h)
1. `runner.py` — asyncio subprocess executor
2. `POST /run` — async spawn + natychmiastowa odpowiedź
3. `GET /runs/{vpsRunId}` — odczyt statusu z local_runs
4. `GET /runs/{vpsRunId}/logs` — streaming/odczyt logu
5. Implementacja webhook z retry

### Faza 4 — Integracja z Next.js (1h)
*Wymaga gotowej Fazy 3 Next.js*

1. Test pełnego flow: Next.js POST /run → Worker odpaluje → webhook wraca
2. Test pobierania logów przez Next.js
3. Test pobierania plików przez Next.js proxy

### Faza 5 — Aktualizacja skryptów Python (1-2h)
1. Dodanie `client_slug` arg do każdego skryptu
2. Zmiana ścieżek na `/home/deploy/clients/{slug}/output/` itd.
3. Testy każdego skryptu z przykładowym slugiem
4. Weryfikacja że stara instancja Flask nadal działa (osobna baza, osobne ścieżki)

---

## Kwestie bezpieczeństwa

- Worker NIE jest publiczny bez tokena — każdy request weryfikowany
- Nginx limituje rozmiar requestów (`client_max_body_size 10M`)
- Skrypty uruchamiane jako user `deploy` bez sudo
- `client_slug` jest sanityzowany przed użyciem w ścieżce (tylko `[a-z0-9-]`)
- Webhook od VPS do Next.js też używa tego samego shared tokena — Next.js weryfikuje że webhook pochodzi od prawdziwego Workera
- Pliki serwowane przez Next.js proxy — klient nigdy nie zna adresu VPS
