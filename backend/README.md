# Sou2AI backend

Sou2AI is a local AI assistant planned for small businesses, with future support for English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language conversations.

## Current milestone

Milestone 4 adds authenticated multi-business creation, full-access creator
memberships, tenant-scoped list/detail/update operations, resumable onboarding,
controlled Lebanese locations and categories, seven-day working hours, and final
profile confirmation. New businesses remain pending and inactive.

It does **not** include activation/admin APIs, invitations or extra roles, pgvector,
Ollama connections, RAG, tool execution/adapters, operational business data,
inventory, billing, memory, uploads, or a React frontend.

## Requirements

Use Python 3.14. The selected package versions declare Python 3.14 support.

## Setup on Windows PowerShell

From the `backend` directory:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Edit `.env` only for local configuration. Do not commit it. The default CORS origins target a future local React development server. In production, set `ALLOWED_CORS_ORIGINS` to explicit trusted origins; wildcard origins are rejected.

Authentication also requires a strong `ACCESS_TOKEN_SECRET` and Resend settings.
For initial Resend testing, `onboarding@resend.dev` can send only to the email
address associated with the Resend account. To send to other recipients, verify a
domain in Resend and configure `RESEND_SENDER_EMAIL` with that domain. Put the API
key only in `.env`; never commit it. Local links default to:

- `http://localhost:5173/verify-email?token=...`
- `http://localhost:5173/reset-password?token=...`

Local HTTP uses `REFRESH_COOKIE_SECURE=false`. Production refuses to start with
that setting or the development signing secret. Set `REFRESH_COOKIE_SECURE=true`
in production and choose `REFRESH_COOKIE_SAMESITE` for the deployed frontend/API
topology. Enable `TRUST_PROXY_HEADERS` only when a trusted reverse proxy replaces
client-supplied forwarding headers.

## Start PostgreSQL

From the repository root:

```powershell
docker compose up -d postgres
docker compose ps
```

The initialization script creates `sou2ai_dev` and `sou2ai_test`. Development
data persists in the `sou2ai_postgres_data` volume. Local credentials in
`.env.example` are development defaults only. Tests refuse to run destructive
setup against any database not named `sou2ai_test`.

From `backend`, apply or roll back the schema:

```powershell
python -m alembic upgrade head
python -m alembic downgrade base
python -m alembic upgrade head
```

## Run the API

```powershell
uvicorn app.main:app --reload
```

Open Swagger UI at <http://127.0.0.1:8000/docs>.

Endpoints:

- Service metadata: <http://127.0.0.1:8000/>
- Health status: <http://127.0.0.1:8000/api/v1/health>
- Database health: <http://127.0.0.1:8000/api/v1/health/database>
- Authentication: `/api/v1/auth/register`, `/verify-email`,
  `/resend-verification`, `/login`, `/refresh`, `/logout`, `/logout-all`, `/me`,
  `/forgot-password`, `/reset-password`, and `/change-password`
- Businesses: `POST/GET /api/v1/businesses`,
  `GET/PATCH /api/v1/businesses/{business_id}`, and
  `POST /api/v1/businesses/{business_id}/onboarding/confirm`

```json
{
  "status": "healthy",
  "service": "Sou2AI API",
  "version": "0.1.0",
  "environment": "development"
}
```

## Run tests

```powershell
$env:TEST_POSTGRESQL_DATABASE_URL = "postgresql+psycopg://sou2ai:sou2ai_local@127.0.0.1:5433/sou2ai_test"
python -m pytest
```

The integration suite also mocks the email-service boundary and exercises the
complete authentication and session lifecycle without contacting Resend.

Authentication abuse controls store temporary event rows containing the event
type, normalized email address, trusted client IP address, and timestamp. The
longest current rate-limit window is one hour. `AUTH_EVENT_RETENTION_HOURS`
therefore defaults to 24 and rejects values below 2 hours, keeping cleanup safely
beyond every active decision window.

Login, verification-resend, and forgot-password requests opportunistically trigger
cleanup. `AUTH_EVENT_CLEANUP_INTERVAL_MINUTES` is a positive integer that defaults
to 60. PostgreSQL stores the next eligible attempt, so requests inside that shared
interval return before querying expired authentication events, including across
processes, instances, and restarts. A nonblocking transaction advisory lock and an
atomic due-time claim prevent duplicate work. The claim advances before deletion,
which bounds retries after a failure. Cleanup deletes at most 1,000 rows per
invocation and is best-effort, so maintenance cannot change a valid authentication
response or bypass its rate-limit transaction. An external database maintenance
job may replace or supplement this mechanism later if event volume requires it.

## Formatting and linting

```powershell
python -m ruff check app tests alembic
python -m ruff format --check app tests alembic
python -m ruff format app tests alembic
```

## Activation and audit operations

Business completion is derived from the current stored profile and cannot be set
by a client. Required trimmed lengths are name 2-120, description 20-2,000, custom
category 2-100 when category is `OTHER`, and address 5-255. Locations must match
the approved governorate/district/city hierarchy. All seven weekdays are required;
closed days have no shifts and open days have one to three chronological,
non-overlapping, non-overnight shifts. Adjacent shifts are allowed.

Drafts are saveable and resumable. Final confirmation validates the whole profile
and records only the first successful timestamp; neither completion nor
confirmation activates a business. Owner-scoped duplicate names are normalized by
trimming, collapsing whitespace, and case-insensitive comparison while preserving
punctuation. PostgreSQL enforces uniqueness per immutable creator owner.

Every business query joins through current-user membership, and unauthorized or
unknown business IDs return the same not-found response. Creation commits the
business and `FULL_ACCESS` creator membership atomically. Schedule replacement is
transactional, and simultaneous profile changes use row-level serialization with
intentional last-write-wins behavior. These database guarantees are safe beyond
the current one-replica MVP and use no process-local locks or onboarding state.

Approved categories are `GROCERY_SUPERMARKET`, `BAKERY`, `RESTAURANT`, `CAFE`,
`CLOTHING`, `ELECTRONICS`, `PHARMACY`, `BEAUTY_COSMETICS`, `HOME_FURNITURE`,
`SERVICES`, and `OTHER`. `OTHER` requires custom text; predefined categories reject
custom text.

The focused location hierarchy is: Beirut/Beirut; Mount Lebanon with Baabda, Aley,
Metn, Keserwan, and Chouf; North with Tripoli, Zgharta, and Koura; Akkar/Akkar;
Bekaa with Zahle and West Bekaa; Baalbek-Hermel with Baalbek and Hermel; South with
Saida and Jezzine; and Nabatieh with Nabatieh, Bint Jbeil, and Marjayoun. Cities and
areas are accepted only under their configured district and governorate; arbitrary
location text is rejected.

Future tool arguments must be canonicalized and HMAC-SHA-256 signed with the
server-only `TOOL_CALL_AUDIT_HMAC_SECRET`. Only the digest is stored. Audit rows
retain no raw arguments, results, prompts, conversations, customer PII, or raw
errors. `TOOL_CALL_AUDIT_RETENTION_DAYS` defaults to 90; a future external
scheduler will call the existing retention operation.

## Next milestone

Future tenant authorization work extends isolation to later business resources;
authentication alone still never grants access to a business without membership.
