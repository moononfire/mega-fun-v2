# Next.js Script Runner — Plan aplikacji Vercel

## Wizja systemu

Aplikacja Next.js działająca na Vercelu jest **centrum dowodzenia** całego systemu.
Pełni trzy role jednocześnie:

1. **Admin API** — punkt kontaktu dla agency-platform (tworzenie klientów, status, zawieszanie)
2. **Silnik orkiestracji** — zleca uruchamianie skryptów na VPS, śledzi wyniki w bazie
3. **UI dla klientów** — każdy klient agencji loguje się i zarządza swoimi kampaniami, widzi wyniki, pobiera pliki

VPS nie jest widoczny dla klientów ani dla agency-platform — jest wewnętrznym workerem wywoływanym wyłącznie przez Next.js.

---

## Stack technologiczny

| Warstwa | Technologia | Powód wyboru |
|---------|-------------|--------------|
| Framework | Next.js 15 (App Router) | Server Actions, Route Handlers, streaming |
| Język | TypeScript | type safety, szczególnie ważne przy API contracts |
| Baza danych | Neon (Postgres serverless) | działa na Vercelu, darmowy tier, SQL |
| ORM | Drizzle ORM | lekki, type-safe, dobry z Neon |
| Auth klientów | własny JWT (jose) | prosta auth per-workspace, bez OAuth |
| UI | Tailwind CSS + shadcn/ui | szybki development, spójny design |
| Fetch do VPS | natywny fetch z Next.js | proste wywołania REST do VPS Worker API |
| Env vars | Vercel Environment Variables | bezpieczne przechowywanie tokenów |
| Deploy | Vercel (auto z gałęzi main) | CD z pudełka |

---

## Schemat bazy danych (Neon Postgres)

### Tabela `clients`
```sql
CREATE TABLE clients (
  id          TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        TEXT UNIQUE NOT NULL,         -- identyfikator klienta, używany wszędzie
  name        TEXT NOT NULL,
  email       TEXT,
  status      TEXT DEFAULT 'active',        -- active | suspended
  config      JSONB DEFAULT '{}',           -- API keys, limity, ustawienia per-klient
  created_at  TIMESTAMPTZ DEFAULT now()
);
```

### Tabela `client_auth`
```sql
CREATE TABLE client_auth (
  id            TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
  client_slug   TEXT NOT NULL REFERENCES clients(slug),
  password_hash TEXT NOT NULL,              -- bcrypt hash hasła do UI
  created_at    TIMESTAMPTZ DEFAULT now()
);
```

### Tabela `runs`
```sql
CREATE TABLE runs (
  id            TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
  client_slug   TEXT NOT NULL REFERENCES clients(slug),
  script        TEXT NOT NULL,              -- np. 'scrape_google_maps', 'send_campaign'
  status        TEXT DEFAULT 'pending',     -- pending | running | done | error
  params        JSONB DEFAULT '{}',         -- parametry przekazane do skryptu
  vps_run_id    TEXT,                       -- ID zwrócone przez VPS Worker po uruchomieniu
  output_files  JSONB DEFAULT '[]',         -- lista plików wynikowych (ścieżki na VPS)
  error_message TEXT,                       -- jeśli status=error
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now()
);
```

### Tabela `campaigns`
```sql
CREATE TABLE campaigns (
  id            TEXT PRIMARY KEY DEFAULT gen_random_uuid(),
  client_slug   TEXT NOT NULL REFERENCES clients(slug),
  name          TEXT NOT NULL,
  status        TEXT DEFAULT 'draft',       -- draft | scheduled | running | done | paused
  script        TEXT DEFAULT 'send_campaign',
  config        JSONB DEFAULT '{}',         -- treść maila, lista odbiorców, skrzynki SMTP
  last_run_id   TEXT REFERENCES runs(id),
  scheduled_at  TIMESTAMPTZ,               -- null = ręczne uruchomienie
  created_at    TIMESTAMPTZ DEFAULT now()
);
```

---

## Admin API — kontrakt z agency-platform

Wszystkie endpointy pod `/api/admin/*`.
Wymagają nagłówka: `Authorization: Bearer <ADMIN_API_TOKEN>` (zmienna środowiskowa na Vercelu).

### POST `/api/admin/clients`
Tworzy klienta w Postgres + wywołuje VPS Worker żeby założył folder klienta.

Request body:
```json
{
  "slug": "firma-xyz",
  "name": "Firma XYZ Sp. z o.o.",
  "email": "kontakt@firmaxyz.pl",
  "initialPassword": "haslo123"
}
```

Flow wewnętrzny:
1. Walidacja slug (unikalność, format)
2. Zapis do `clients` + `client_auth` (bcrypt hasła)
3. `POST {VPS_WORKER_URL}/clients` — VPS tworzy folder struktury
4. Jeśli VPS zwróci błąd → rollback w Postgres
5. Zwraca `201` z danymi klienta

Response `201`:
```json
{
  "id": "uuid",
  "slug": "firma-xyz",
  "name": "Firma XYZ Sp. z o.o.",
  "status": "active",
  "createdAt": "2026-06-11T12:00:00Z"
}
```

---

### GET `/api/admin/clients/:slug`
Status klienta + ostatnie 10 runów + rozmiar plików na VPS.

Response `200`:
```json
{
  "client": { "slug": "...", "name": "...", "status": "active" },
  "recentRuns": [ { "id": "...", "script": "...", "status": "done", "finishedAt": "..." } ],
  "storage": { "outputSizeBytes": 1048576 }
}
```

---

### PATCH `/api/admin/clients/:slug`
Zmiana statusu lub aktualizacja config.

Request body (wszystkie pola opcjonalne):
```json
{
  "status": "suspended",
  "config": { "smtpLimit": 50 }
}
```

Jeśli `status: "suspended"` → Next.js informuje VPS żeby zablokował nowe runy dla tego klienta.

---

### DELETE `/api/admin/clients/:slug`
Usuwa klienta z Postgres + wywołuje VPS żeby usunął folder (nieodwracalne).
Wymaga dodatkowego nagłówka: `X-Confirm-Delete: yes`.

---

### POST `/api/admin/clients/:slug/run`
Uruchamia skrypt dla klienta (może wywołać agency-platform lub przyszłe cron-y).

Request body:
```json
{
  "script": "scrape_google_maps",
  "params": { "query": "restauracje Kraków", "limit": 100 }
}
```

---

### GET `/api/admin/health`
Sprawdza stan systemu. Odpytuje VPS o `/health` i agreguje z danymi z Postgres.

Response:
```json
{
  "postgres": "ok",
  "vpsWorker": "ok",
  "activeClients": 12,
  "runningScripts": 2,
  "vpsStorage": { "usedGB": 4.2, "freeGB": 20.1 }
}
```

---

## UI dla klientów

### Autentykacja
- Prosta strona logowania: `/login` — `slug` + hasło
- Po zalogowaniu: JWT cookie (httpOnly, 7 dni)
- Middleware Next.js sprawdza cookie na trasach `/dashboard/*`
- Klient widzi **tylko swoje dane** — slug z JWT jest używany do wszystkich zapytań

### Dashboard `/dashboard`
- Kafelki statystyk: liczba runów, ostatni run, rozmiar plików
- Tabela ostatnich 10 runów ze statusem i linkami do pobrania
- Przycisk "Uruchom skrypt"

### Kampanie `/dashboard/campaigns`
- Lista kampanii klienta (tabela z statusem, datą, ostatnim runem)
- Formularz tworzenia kampanii:
  - Nazwa kampanii
  - Skrypt (select: `send_campaign`, `scrape_google_maps`, etc.)
  - Parametry (dynamiczny formularz zależny od wybranego skryptu)
  - Opcjonalnie: harmonogram (data/czas)
- Przycisk "Uruchom teraz" → POST `/api/runs`
- Historia runów kampanii

### Runs `/dashboard/runs`
- Pełna historia runów
- Filtrowanie po statusie, skrypcie, dacie
- Kliknięcie w run → szczegóły + logi (streaming z VPS przez Next.js)

### Pliki `/dashboard/files`
- Lista plików output z VPS (nazwa, rozmiar, data)
- Przycisk pobierania — Next.js proxy do VPS, klient nigdy nie zna adresu VPS
- Możliwość usunięcia pliku

### Ustawienia `/dashboard/settings`
- Zmiana hasła
- Dane klienta (tylko do odczytu — zarządza agency-platform)
- Config klienta (edytowalne pola zależne od produktu)

---

## Wewnętrzne API (dla UI klientów)

Endpointy pod `/api/*`, wymagają ważnego JWT cookie.

| Metoda | Endpoint | Działanie |
|--------|----------|-----------|
| GET | `/api/runs` | Lista runów zalogowanego klienta |
| POST | `/api/runs` | Uruchomienie skryptu |
| GET | `/api/runs/:id` | Status runa + parametry |
| GET | `/api/runs/:id/logs` | Streaming logów z VPS (SSE lub polling) |
| GET | `/api/files` | Lista plików output klienta |
| GET | `/api/files/:filename` | Proxy pobierania pliku z VPS |
| DELETE | `/api/files/:filename` | Usunięcie pliku (Next.js woła VPS) |
| GET | `/api/campaigns` | Lista kampanii klienta |
| POST | `/api/campaigns` | Tworzenie kampanii |
| PATCH | `/api/campaigns/:id` | Edycja kampanii |
| POST | `/api/campaigns/:id/run` | Ręczne uruchomienie kampanii |

---

## Komunikacja Next.js → VPS Worker

Next.js komunikuje się z VPS przez HTTPS. Każde wywołanie zawiera:
```
Authorization: Bearer <VPS_WORKER_TOKEN>
Content-Type: application/json
```

### Flow uruchamiania skryptu (szczegółowy)

```
1. Klient klika "Uruchom" w UI
2. POST /api/runs (Next.js Route Handler)
3. Next.js: sprawdza JWT, weryfikuje slug
4. Next.js: INSERT do runs (status='pending')
5. Next.js: POST {VPS_WORKER_URL}/run
   body: { runId, clientSlug, script, params }
6. VPS: odpowiada natychmiast { vpsRunId: "abc123" } — async!
7. Next.js: UPDATE runs SET vps_run_id='abc123', status='running'
8. Next.js: odpowiada klientowi { runId, status: 'running' }
9. UI: polling GET /api/runs/:id co 3 sekundy
10. VPS: po zakończeniu skryptu woła webhook:
    POST {NEXT_PUBLIC_URL}/api/webhooks/run-complete
    body: { vpsRunId, status, outputFiles, errorMessage }
11. Next.js webhook: UPDATE runs SET status, finished_at, output_files
12. UI: kolejny polling zwraca status='done' → pokazuje wyniki
```

### Obsługa błędów połączenia z VPS
- Jeśli VPS nie odpowiada w 10s → run.status = 'error', error_message = 'VPS unavailable'
- Webhook ma retry z exponential backoff (3 próby)
- Next.js ma endpoint `/api/admin/runs/:id/sync` do ręcznej synchronizacji statusu z VPS

---

## Zmienne środowiskowe (Vercel)

```env
# Baza danych
DATABASE_URL=postgresql://...neon.tech/...

# Autoryzacja Admin API (agency-platform → Next.js)
ADMIN_API_TOKEN=<długi random secret>

# Komunikacja z VPS
VPS_WORKER_URL=https://runner-worker.riskydev.com
VPS_WORKER_TOKEN=<długi random secret — ten sam co na VPS>

# JWT dla klientów
JWT_SECRET=<długi random secret>

# Webhook (Next.js musi znać własny URL)
NEXTAUTH_URL=https://runner.riskydev.com
```

---

## Fazy realizacji

### Faza 1 — Fundament (3-4h)
1. `npx create-next-app@latest` — TypeScript, App Router, Tailwind
2. Połączenie z Neon Postgres + konfiguracja Drizzle ORM
3. Definicja schematu (clients, client_auth, runs, campaigns)
4. Migracja bazy (`drizzle-kit push`)
5. Middleware auth — ochrona tras `/dashboard/*` i `/api/admin/*`
6. Strona logowania `/login` — formularz + JWT cookie

### Faza 2 — Admin API (2-3h)
1. `POST /api/admin/clients` — tworzenie klienta w DB (bez VPS na razie)
2. `GET /api/admin/clients/:slug` — odczyt klienta + runy
3. `PATCH /api/admin/clients/:slug` — update statusu/config
4. `DELETE /api/admin/clients/:slug` — usunięcie
5. `GET /api/admin/health` — basic health check
6. Testy endpointów przez curl/Postman

### Faza 3 — Integracja z VPS Worker (2-3h)
*Wymaga gotowego VPS Worker — patrz vps-plan.md Faza 1+2*

1. Serwis `vpsClient.ts` — wrapper na fetch z auth tokenem i timeoutami
2. Rozszerzenie `POST /api/admin/clients` o wywołanie VPS `/clients`
3. `POST /api/runs` — zapis do DB + wywołanie VPS `/run`
4. Endpoint webhook `POST /api/webhooks/run-complete` — aktualizacja statusu runa
5. `GET /api/runs/:id` — odczyt statusu (polling przez UI)
6. Test end-to-end: stwórz klienta → odpal skrypt → sprawdź status

### Faza 4 — UI klientów (4-5h)
1. Layout dashboard z nawigacją (sidebar)
2. Strona `/dashboard` — kafelki + tabela ostatnich runów
3. Strona `/dashboard/runs` — lista, filtrowanie, szczegóły runa
4. Strona `/dashboard/runs/:id` — logi (polling `/api/runs/:id/logs`)
5. Strona `/dashboard/files` — lista + pobieranie przez proxy
6. Formularz uruchamiania skryptu

### Faza 5 — Kampanie (3-4h)
1. Strona `/dashboard/campaigns` — lista kampanii
2. Formularz tworzenia kampanii z dynamicznymi parametrami per skrypt
3. `POST /api/campaigns/:id/run` — uruchomienie kampanii
4. Historia runów kampanii
5. Strona `/dashboard/settings` — zmiana hasła, config

### Faza 6 — Stabilizacja (2h)
1. Error boundaries i obsługa błędów w UI
2. Loading states i optymistyczne UI
3. Rate limiting na Admin API
4. Logi requestów (Vercel Analytics lub własne)
5. Test pełnego flow z agency-platform
