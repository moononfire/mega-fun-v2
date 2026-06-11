# TODO — Integracja mega-fun (script runner) z agency platform

## Kontekst

Mega-fun zostaje podzielony na dwie warstwy:
- **Next.js script runner** — nowa aplikacja (UI + API), działa na VPS
- **Skrypty Python na VPS** — bez zmian, przyjmują `client_slug` jako argument

Agency platform zarządza script runnerem jak każdym innym produktem: tworzy klientów, odpytuje status, aktywuje/zawiesza.

---

## 1. Zmiany w agency-platform

### 1.1 Schemat bazy — rozszerzenie `products`

Aktualna tabela zakłada deployment Vercelowy. Dodać kolumny dla VPS:

```sql
ALTER TABLE products ADD COLUMN deployment_type text NOT NULL DEFAULT 'vercel';
-- 'vercel' | 'vps'
ALTER TABLE products ADD COLUMN vps_api_url text;
-- np. https://runner.riskydev.com/api/admin
ALTER TABLE products ADD COLUMN vps_api_token text;
-- shared secret do autoryzacji requestów
```

Kolumny `vercel_project_id` i `vercel_token` pozostają — są nullable dla produktów VPS.

### 1.2 Server action tworzenia tenanta — rozgałęzienie

Aktualny flow w kroku 6 wizarda woła Vercel API. Dodać rozgałęzienie:

```
IF product.deploymentType === 'vps'
  → POST {vpsApiUrl}/clients  (body: { slug, name, email })
ELSE
  → Vercel API (istniejący flow)
```

### 1.3 Rejestracja produktu mega-fun

Po zbudowaniu script runnera dodać rekord w `products`:
```
name: "Mega-fun Script Runner"
deploymentType: "vps"
vpsApiUrl: "https://runner.riskydev.com/api/admin"
vpsApiToken: <secret z env>
baseDomain: "runner.riskydev.com"
```

---

## 2. Co zbudować w Next.js script runner

### 2.1 Struktura folderów klientów na VPS

```
/home/deploy/clients/
  {client_slug}/
    input/        ← parametry przekazywane do skryptów
    output/       ← wyniki (CSV, JSON)
    logs/         ← stdout/stderr z każdego runa
    config.json   ← ustawienia klienta (API keys, limity)
```

### 2.2 Baza danych (SQLite lokalnie na VPS)

```sql
CREATE TABLE clients (
  id TEXT PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  email TEXT,
  config TEXT,   -- JSON
  status TEXT DEFAULT 'active',  -- active | suspended
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  client_slug TEXT NOT NULL,
  script TEXT NOT NULL,          -- np. 'scrape_google_maps'
  status TEXT DEFAULT 'pending', -- pending | running | done | error
  params TEXT,                   -- JSON z parametrami wywołania
  output_path TEXT,              -- ścieżka do pliku wynikowego
  started_at TEXT,
  finished_at TEXT
);
```

### 2.3 Admin API (wywoływane przez agency-platform)

Wszystkie endpointy wymagają nagłówka `Authorization: Bearer <VPS_API_TOKEN>`.

| Metoda | Endpoint | Działanie |
|--------|----------|-----------|
| POST | `/api/admin/clients` | Tworzy klienta + folder na VPS |
| GET | `/api/admin/clients/:slug` | Status klienta, ostatnie runy, rozmiar output/ |
| PATCH | `/api/admin/clients/:slug` | Zmiana statusu (active/suspended), update config |
| DELETE | `/api/admin/clients/:slug` | Usuwa klienta + folder (wymaga potwierdzenia) |
| POST | `/api/admin/clients/:slug/run` | Uruchamia skrypt dla klienta |
| GET | `/api/admin/health` | Stan VPS: dysk, cron, liczba aktywnych klientów |

### 2.4 Skrypty Python — jedyna wymagana zmiana

Każdy skrypt musi przyjmować `client_slug` jako pierwszy argument i budować ścieżki przez niego:

```python
# na początku każdego skryptu
import sys, os
CLIENT_SLUG = sys.argv[1]
BASE_DIR = f"/home/deploy/clients/{CLIENT_SLUG}"
OUTPUT_DIR = f"{BASE_DIR}/output"
LOG_DIR    = f"{BASE_DIR}/logs"
```

Żadnych innych zmian w logice skryptów.

### 2.5 Uruchamianie skryptów z Next.js

Next.js odpala skrypt przez `child_process.spawn`, zapisuje stdout do `logs/`, po zakończeniu aktualizuje rekord w `runs`:

```ts
// app/api/admin/clients/[slug]/run/route.ts
import { spawn } from "child_process";

const proc = spawn("python3", [
  `/home/deploy/scripts/${script}.py`,
  slug,
  ...params
]);
// stdout → append do logs/{runId}.log
// po exit → UPDATE runs SET status='done', finished_at=NOW()
```

### 2.6 UI script runnera (dla zalogowanych klientów)

Klient loguje się swoim hasłem (proste auth per-workspace, nie NextAuth):
- Dashboard: statystyki runów, ostatnie wyniki
- Przycisk uruchomienia skryptu z parametrami
- Lista plików w `output/` z możliwością pobrania
- Logi ostatnich runów

---

## 3. Kolejność realizacji

| # | Zadanie | Gdzie |
|---|---------|-------|
| 1 | Migracja schematu `products` (deployment_type, vps_api_url, vps_api_token) | agency-platform |
| 2 | Rozgałęzienie w server action tworzenia tenanta | agency-platform |
| 3 | Inicjalizacja Next.js + SQLite na VPS | mega-fun runner |
| 4 | Struktura folderów `/home/deploy/clients/` | VPS |
| 5 | Admin API (`/api/admin/*`) z auth tokenem | mega-fun runner |
| 6 | Aktualizacja skryptów Python — dodanie `client_slug` arg | VPS scripts |
| 7 | Logika `spawn` + zapis logów + update `runs` | mega-fun runner |
| 8 | Rejestracja produktu w agency-platform (INSERT do `products`) | agency-platform |
| 9 | Test end-to-end: stwórz klienta z agency-platform → folder pojawia się na VPS | — |
| 10 | UI dla klienta (dashboard, trigger runów, pobieranie wyników) | mega-fun runner |

---

## 4. Zmienne środowiskowe

### agency-platform `.env.local`
```env
# Dodać:
MEGA_FUN_API_URL=https://runner.riskydev.com/api/admin
MEGA_FUN_API_TOKEN=<secret>
```

### mega-fun runner `.env.local`
```env
ADMIN_API_TOKEN=<ten sam secret>
CLIENTS_BASE_DIR=/home/deploy/clients
SCRIPTS_DIR=/home/deploy/scripts
DATABASE_URL=file:./db.sqlite
```
