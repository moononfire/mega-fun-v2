# Script Runner — Plan systemu (Next.js + VPS Worker)

## Wizja systemu

System składa się z dwóch aplikacji współpracujących przez HTTPS:

```
agency-platform (Vercel)
    ↓ Admin API (Bearer token)
Next.js Script Runner (Vercel)
    ↓ Worker API (Bearer token)
VPS Worker (FastAPI, Python)
    ↓ subprocess
Skrypty Python (/home/deploy/scripts/)
    ↓ pliki wynikowe
/home/deploy/clients/{slug}/output/
```

**Next.js na Vercelu** — centrum dowodzenia. Trzy role jednocześnie:
1. Punkt wejścia z agency-platform (`/api/setup`) — tworzenie klientów
2. Silnik orkiestracji — zleca uruchamianie skryptów VPS Worker, śledzi wyniki w Neon
3. UI dla klientów — każdy klient loguje się i zarządza swoimi kampaniami

**VPS Worker (FastAPI)** — cichy wykonawca bez UI. Jedynym rozmówcą jest Next.js.
Odpowiedzialności: foldery per klient, subprocess skryptów Python, logi, pliki, webhook po zakończeniu.

**VPS Worker NIE sprawdza tożsamości klientów** — to rola Next.js. Worker tylko weryfikuje że request pochodzi od Next.js (shared token).

---

## Integracja z agency-platform

Runner jest zwykłym produktem w agency-platform — zarejestrowanym jak każdy inny (hair-style itp.).
**Żadnych zmian w schemacie ani kodzie agency-platform nie potrzeba.**

Jedyna zmiana w agency-platform: dodanie `slug` do body wywołania `/api/setup` (jedna linia w `tenants.ts`):
```ts
// src/app/actions/tenants.ts — istniejące wywołanie /api/setup
body: JSON.stringify({
  tenantId,
  slug,        // ← dodać — runner potrzebuje slug do nazwy folderu na VPS
  adminName,
  adminEmail,
  adminPassword,
  services: ...,
})
```

Gdy agency-platform tworzy tenanta dla runnera:
1. Woła `POST runner.riskydev.com/api/setup` z `{ tenantId, slug, adminName, adminEmail, adminPassword }`
2. Runner tworzy klienta w Neon + woła VPS Worker żeby założył folder
3. Runner odpowiada `200` — agency-platform kończy onboarding

`slug` z agency-platform (`tenants.slug`) = `clients.slug` w runnerze = nazwa folderu na VPS. Muszą być identyczne.

Rejestracja produktu w agency-platform (jednorazowo przez UI):
```
name:            "Mega-fun Script Runner"
vercelProjectId: <id projektu na Vercelu>
vercelToken:     <token>
baseDomain:      runner.riskydev.com
appUrl:          https://runner.riskydev.com
```

---

# CZĘŚĆ 1 — Next.js Script Runner (Vercel)

## Stack technologiczny

| Warstwa | Technologia |
|---------|-------------|
| Framework | Next.js App Router, TypeScript |
| Baza danych | Neon (Postgres serverless) |
| ORM | Drizzle ORM |
| Auth klientów | własny JWT (jose) — per-workspace, bez OAuth |
| UI | Tailwind CSS + shadcn/ui |
| Fetch do VPS | natywny fetch z timeout i retry |
| Deploy | Vercel (auto z gałęzi main) |

---

## Schemat bazy danych (Neon Postgres)

```sql
CREATE TABLE clients (
  id          TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        TEXT UNIQUE NOT NULL,
  name        TEXT NOT NULL,
  email       TEXT,
  status      TEXT DEFAULT 'active',   -- active | suspended
  config      JSONB DEFAULT '{}',
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE client_auth (
  id            TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
  client_slug   TEXT NOT NULL REFERENCES clients(slug),
  password_hash TEXT NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE runs (
  id            TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
  client_slug   TEXT NOT NULL REFERENCES clients(slug),
  script        TEXT NOT NULL,
  status        TEXT DEFAULT 'pending',  -- pending | running | done | error
  params        JSONB DEFAULT '{}',
  vps_run_id    TEXT,
  output_files  JSONB DEFAULT '[]',
  error_message TEXT,
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE campaigns (
  id            TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
  client_slug   TEXT NOT NULL REFERENCES clients(slug),
  name          TEXT NOT NULL,
  status        TEXT DEFAULT 'draft',   -- draft | scheduled | running | done | paused
  script        TEXT DEFAULT 'send_campaign',
  config        JSONB DEFAULT '{}',
  last_run_id   TEXT REFERENCES runs(id),
  scheduled_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now()
);
```

---

## Punkt wejścia z agency-platform: `/api/setup`

```
POST /api/setup
x-agency-secret: <AGENCY_API_SECRET>
```

To jest jedyny endpoint który agency-platform woła — taki sam mechanizm jak w hair-style i innych produktach.

Request (wysyłany przez agency-platform):
```json
{
  "tenantId": "uuid-z-agency",
  "slug": "firma-xyz",
  "adminName": "Jan Kowalski",
  "adminEmail": "jan@firmaxyz.pl",
  "adminPassword": "haslo123",
  "services": ""
}
```

Flow wewnętrzny:
1. Weryfikacja `x-agency-secret`
2. INSERT do `clients` (slug, name=adminName, email=adminEmail, status='active')
3. INSERT do `client_auth` (bcryptjs hash adminPassword)
4. `POST {VPS_WORKER_URL}/clients` — Worker zakłada folder klienta na VPS
5. Jeśli VPS zwróci błąd → rollback w Postgres, odpowiedź `500`
6. Odpowiedź `200 { ok: true }`

## Wewnętrzne endpointy zarządzania (`/api/admin/*`)

Używane przez własne UI runnera lub ręcznie — **nie przez agency-platform**.
Wymagają nagłówka: `Authorization: Bearer <ADMIN_API_TOKEN>`.

### GET `/api/admin/clients/:slug`
Status klienta + ostatnie 10 runów + rozmiar plików.

### PATCH `/api/admin/clients/:slug`
Zmiana statusu lub config. Gdy `status: "suspended"`:
- UPDATE `clients.status` w Postgres
- `PATCH {VPS_WORKER_URL}/clients/:slug { status: "suspended" }` — Worker blokuje nowe runy
- Middleware blokuje login zawieszonym klientom

### DELETE `/api/admin/clients/:slug`
Wymaga nagłówka `X-Confirm-Delete: yes`.
Flow: sprawdź brak aktywnych runów → DELETE z Postgres → `DELETE {VPS_WORKER_URL}/clients/:slug`

### GET `/api/admin/health`
Agreguje: stan Postgres + odpowiedź `GET {VPS_WORKER_URL}/health`.

---

## Wewnętrzne API (dla UI klientów)

Endpointy pod `/api/*`, wymagają ważnego JWT cookie.
**Każde zapytanie do DB jest scopowane do `client_slug` wyciągniętego z JWT** — klienci nie widzą nawzajem swoich danych.

| Metoda | Endpoint | VPS Worker call |
|--------|----------|-----------------|
| GET | `/api/runs` | — |
| POST | `/api/runs` | `POST /run` |
| GET | `/api/runs/:id` | opcjonalnie `GET /runs/:vpsRunId` |
| GET | `/api/runs/:id/logs` | `GET /runs/:vpsRunId/logs` |
| GET | `/api/files` | `GET /clients/:slug/files` |
| GET | `/api/files/:filename` | `GET /clients/:slug/files/:filename` (proxy) |
| DELETE | `/api/files/:filename` | `DELETE /clients/:slug/files/:filename` |
| GET | `/api/campaigns` | — |
| POST | `/api/campaigns` | — |
| PATCH | `/api/campaigns/:id` | — |
| POST | `/api/campaigns/:id/run` | `POST /run` |

---

## Webhook od VPS Worker

```
POST /api/webhooks/run-complete
Authorization: Bearer <VPS_WORKER_TOKEN>  ← Next.js weryfikuje token
```

Body:
```json
{
  "runId": "uuid-z-next-js",
  "vpsRunId": "vps-uuid",
  "status": "done",
  "outputFiles": ["results_2026-06-11.csv"],
  "errorMessage": null,
  "finishedAt": "2026-06-11T12:05:00Z"
}
```

Next.js aktualizuje `runs` w Postgres. UI polluje `GET /api/runs/:id` co 3s.

---

## Flow uruchamiania skryptu (end-to-end)

```
1.  Klient klika "Uruchom" w UI
2.  POST /api/runs (Next.js Route Handler)
3.  Next.js: weryfikuje JWT, wyciąga client_slug
4.  Next.js: INSERT runs (status='pending')
5.  Next.js: POST {VPS_WORKER_URL}/run
    body: { runId, clientSlug, script, params, webhookUrl: NEXTJS_URL+'/api/webhooks/run-complete' }
6.  VPS: odpowiada natychmiast 202 { vpsRunId }
7.  Next.js: UPDATE runs SET vps_run_id, status='running', started_at
8.  Next.js: odpowiada klientowi { runId, status: 'running' }
9.  UI: polling GET /api/runs/:id co 3 sekundy
10. VPS: skrypt kończy działanie → wywołuje webhook (3 próby z backoff)
11. Next.js webhook: UPDATE runs SET status, finished_at, output_files
12. UI: kolejny polling zwraca status='done' → pokazuje wyniki i pliki
```

Obsługa błędu VPS:
- Jeśli VPS nie odpowiada w 10s → `runs.status = 'error'`, `error_message = 'VPS unavailable'`
- Endpoint `/api/admin/runs/:id/sync` do ręcznej synchronizacji statusu z `GET /runs/:vpsRunId`

---

## Auth klientów (JWT)

- Strona `/login` — formularz: `slug` + hasło
- Server Action: weryfikuje bcrypt hash z `client_auth`, sprawdza `clients.status`
- Jeśli `status = 'suspended'` → błąd "Konto zawieszone, skontaktuj się z agencją"
- Po zalogowaniu: JWT cookie (httpOnly, 7 dni, podpisany `JWT_SECRET`)
- Middleware Next.js (`middleware.ts`) chroni `/dashboard/*` — weryfikuje JWT i sprawdza status klienta przy każdym requeście

---

## UI klientów

### `/login`
Formularz slug + hasło. Komunikat blokady dla zawieszonych kont.

### `/dashboard`
Kafelki: liczba runów, ostatni run, rozmiar plików output.
Tabela ostatnich 10 runów ze statusem.

### `/dashboard/runs`
Pełna historia runów. Filtrowanie po statusie, skrypcie, dacie.
Szczegóły runa: parametry, logi (polling `GET /api/runs/:id/logs`), pliki wynikowe.

### `/dashboard/campaigns`
Lista kampanii. Formularz tworzenia z dynamicznymi parametrami per skrypt.
Historia runów kampanii. Przycisk "Uruchom teraz".

### `/dashboard/files`
Lista plików z `output/`. Pobieranie przez Next.js proxy (klient nie zna adresu VPS).
Usuwanie pliku.

### `/dashboard/settings`
Zmiana hasła. Dane klienta (read-only). Edytowalne pola config.

---

## Zmienne środowiskowe (Vercel)

```env
DATABASE_URL=postgresql://...neon.tech/...

# Weryfikacja requestów z agency-platform (ten sam secret co AGENCY_API_SECRET w agency-platform)
AGENCY_API_SECRET=<secret>

# Wewnętrzne endpointy /api/admin/* (opcjonalne, do ręcznego zarządzania)
ADMIN_API_TOKEN=<secret>

# VPS Worker — Next.js → VPS
VPS_WORKER_URL=https://runner-worker.riskydev.com
VPS_WORKER_TOKEN=<secret>

# JWT dla klientów
JWT_SECRET=<secret>

# Własny URL (do budowania webhookUrl wysyłanego do VPS)
NEXTJS_URL=https://runner.riskydev.com
```

---

## Fazy realizacji — Next.js

| # | Zadanie | Czas |
|---|---------|------|
| 1 | create-next-app + Neon + Drizzle schema + migracja | 1-2h |
| 2 | Middleware auth + strona /login + JWT cookie (jose + bcryptjs) | 1h |
| 3 | Serwis `vpsClient.ts` (fetch wrapper z auth, timeout, retry) | 1h |
| 4 | `POST /api/setup` — tworzenie klienta w DB + wywołanie VPS Worker | 1h |
| 5 | POST /api/runs + webhook endpoint + polling GET /api/runs/:id | 2h |
| 6 | Wewnętrzne endpointy /api/admin/* (GET/PATCH/DELETE clients, health) | 1h |
| 7 | Layout dashboard + strona /dashboard + /dashboard/runs | 2h |
| 8 | Proxy plików + logi runa | 1h |
| 9 | Strona /dashboard/campaigns + formularze | 2h |
| 10 | Dodanie `slug` do body /api/setup w agency-platform (tenants.ts — 1 linia) | 15min |
| 11 | Test end-to-end z agency-platform | 1h |

---

# CZĘŚĆ 2 — VPS Worker (FastAPI)

## Stack technologiczny

| Warstwa | Technologia |
|---------|-------------|
| Framework | FastAPI (Python) |
| Serwer ASGI | Uvicorn |
| Async subprocess | asyncio.create_subprocess_exec |
| Baza lokalna | SQLite (tylko stan runów) |
| Auth | Bearer token |
| Process manager | systemd |
| Reverse proxy | Nginx (TLS termination) |

FastAPI w Pythonie — ten sam język co skrypty. Bez Node.js na VPS.

---

## Struktura folderów na VPS

```
/home/deploy/
  worker/
    main.py
    db.py
    runner.py
    requirements.txt
    .env

  scripts/
    scrape_google_maps.py
    send_campaign.py
    scrape_emails.py
    ...

  clients/
    {client_slug}/
      input/
      output/
      logs/
      config.json

  worker.db
```

---

## Schemat SQLite Workera

Tylko do śledzenia stanu procesów. Główna baza jest w Neon.

```sql
CREATE TABLE local_runs (
  id           TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL,        -- runId z Next.js (do webhook)
  client_slug  TEXT NOT NULL,
  script       TEXT NOT NULL,
  status       TEXT DEFAULT 'running',
  pid          INTEGER,
  log_path     TEXT,
  output_files TEXT DEFAULT '[]',
  error_msg    TEXT,
  started_at   TEXT DEFAULT (datetime('now')),
  finished_at  TEXT
);

CREATE TABLE local_clients (
  slug       TEXT PRIMARY KEY,
  status     TEXT DEFAULT 'active',
  created_at TEXT DEFAULT (datetime('now'))
);
```

---

## Worker API — endpointy

Wszystkie wymagają: `Authorization: Bearer <VPS_WORKER_TOKEN>`

### POST `/clients`
Tworzy strukturę folderów dla klienta.
```json
{ "slug": "firma-xyz", "name": "Firma XYZ", "config": {} }
```
Flow: walidacja slug → mkdir `{input,output,logs}` → zapis `config.json` → INSERT `local_clients` → `201`

### DELETE `/clients/{slug}`
Sprawdza brak aktywnych runów (`409` jeśli są). Usuwa folder i rekord.

### PATCH `/clients/{slug}`
Aktualizuje status i/lub config.json.
Jeśli `status: "suspended"` — nowe requesty `/run` dla tego klienta zwracają `403`.

### POST `/run` ⭐
Kluczowy endpoint. Odpowiada natychmiast `202`, skrypt działa w tle.

Request:
```json
{
  "runId": "uuid-z-next-js",
  "clientSlug": "firma-xyz",
  "script": "send_campaign",
  "params": { "campaignId": "abc" },
  "webhookUrl": "https://runner.riskydev.com/api/webhooks/run-complete"
}
```

Flow:
1. Sprawdź `local_clients.status` — jeśli `suspended` → `403`
2. Sprawdź że `/home/deploy/scripts/{script}.py` istnieje → `400` jeśli nie
3. Wygeneruj `vpsRunId`
4. INSERT `local_runs` (status='running')
5. Odpowiedź `202 { vpsRunId }`
6. `asyncio.create_task(execute_run(...))` — działa w tle

### GET `/runs/{vpsRunId}`
Status runa z `local_runs`.

### GET `/runs/{vpsRunId}/logs`
Zawartość pliku logu. Query param `?tail=100` — ostatnie N linii.
Response: `text/plain`

### GET `/clients/{slug}/files`
Lista plików w `output/`.
```json
[{ "name": "results.csv", "sizeBytes": 45231, "modifiedAt": "..." }]
```

### GET `/clients/{slug}/files/{filename}`
Streaming pliku. `Content-Disposition: attachment`.
Next.js proxy'uje ten endpoint — klient nie zna adresu VPS.

### DELETE `/clients/{slug}/files/{filename}`
Usuwa plik z `output/`.

### GET `/health`
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

## Runner — uruchamianie skryptów

```python
# runner.py
async def execute_run(vps_run_id, run_id, client_slug, script, params, webhook_url, db):
    log_path = Path(f"/home/deploy/clients/{client_slug}/logs/{vps_run_id}.log")
    output_dir = Path(f"/home/deploy/clients/{client_slug}/output")
    start_time = time.time()

    args = ["python3", f"/home/deploy/scripts/{script}.py", client_slug]
    for key, value in params.items():
        args.append(f"{key}={value}")

    try:
        with open(log_path, "w") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=f"/home/deploy/clients/{client_slug}"
            )
            db.update_run_pid(vps_run_id, proc.pid)
            await proc.wait()

        if proc.returncode == 0:
            output_files = [
                f.name for f in output_dir.iterdir()
                if f.is_file() and f.stat().st_mtime > start_time
            ]
            status, error_msg = "done", None
        else:
            status, error_msg = "error", f"Exit code: {proc.returncode}"
    except Exception as e:
        status, error_msg = "error", str(e)

    db.finish_run(vps_run_id, status, output_files, error_msg)
    await notify_nextjs(webhook_url, run_id, vps_run_id, status, output_files, error_msg)


async def notify_nextjs(webhook_url, run_id, vps_run_id, status, output_files, error_msg):
    payload = {
        "runId": run_id, "vpsRunId": vps_run_id, "status": status,
        "outputFiles": output_files, "errorMessage": error_msg,
        "finishedAt": datetime.utcnow().isoformat()
    }
    for attempt in range(3):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    webhook_url, json=payload,
                    headers={"Authorization": f"Bearer {VPS_WORKER_TOKEN}"},
                    timeout=10
                )
                if r.status_code == 200:
                    return
        except Exception:
            await asyncio.sleep(2 ** attempt)
```

---

## Zmiany w skryptach Python

Jedyna wymagana zmiana — dodanie obsługi `client_slug` i ścieżek na początku każdego skryptu:

```python
# === DODAĆ NA POCZĄTKU KAŻDEGO SKRYPTU ===
import sys
from pathlib import Path

CLIENT_SLUG = sys.argv[1]
BASE_DIR    = Path(f"/home/deploy/clients/{CLIENT_SLUG}")
INPUT_DIR   = BASE_DIR / "input"
OUTPUT_DIR  = BASE_DIR / "output"

# Parametry dodatkowe: key=value w argv[2:]
params = dict(arg.split("=", 1) for arg in sys.argv[2:] if "=" in arg)
# === KONIEC DODATKU — reszta skryptu bez zmian ===
```

---

## Zmienne środowiskowe VPS (`.env`)

```env
VPS_WORKER_TOKEN=<ten sam secret co w Next.js VPS_WORKER_TOKEN>
NEXTJS_URL=https://runner.riskydev.com
CLIENTS_DIR=/home/deploy/clients
SCRIPTS_DIR=/home/deploy/scripts
DATABASE_PATH=/home/deploy/worker.db
PORT=8001
```

---

## Konfiguracja systemd

`/etc/systemd/system/mega-fun-worker.service`:
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

## Konfiguracja Nginx

```nginx
server {
    listen 443 ssl;
    server_name runner-worker.riskydev.com;

    ssl_certificate /etc/letsencrypt/live/runner-worker.riskydev.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/runner-worker.riskydev.com/privkey.pem;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
        proxy_connect_timeout 10s;
    }
}
```

---

## Bezpieczeństwo

- Każdy request weryfikowany Bearer tokenem
- `client_slug` sanityzowany przed użyciem w ścieżce — tylko `[a-z0-9-]`
- Skrypty uruchamiane jako user `deploy` bez sudo
- Webhook Next.js→VPS i VPS→Next.js używają tego samego shared tokena (weryfikacja obu kierunków)
- Pliki serwowane wyłącznie przez Next.js proxy — klient nigdy nie zna adresu VPS Worker

---

## Fazy realizacji — VPS Worker

| # | Zadanie | Czas |
|---|---------|------|
| 1 | Setup: venv, FastAPI, middleware auth, GET /health, systemd, Nginx | 2h |
| 2 | Zarządzanie klientami: POST/DELETE/PATCH /clients, GET/DELETE /files | 2h |
| 3 | Runner: asyncio subprocess, POST /run, GET /runs/:id, GET /runs/:id/logs, webhook | 3h |
| 4 | Integracja z Next.js — test full flow | 1h |
| 5 | Aktualizacja skryptów Python — dodanie client_slug arg, test każdego | 2h |
