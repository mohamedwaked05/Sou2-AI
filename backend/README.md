# Sou2AI backend

Sou2AI is a local AI assistant planned for small businesses, with future support for English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language conversations.

## Current milestone

Milestone 3 adds user registration, required email verification, password login,
rotating refresh sessions, logout, account recovery, and authenticated password
changes to the existing PostgreSQL platform foundation.

It does **not** include business onboarding or authorization, pgvector, Ollama
connections, RAG, tool execution/adapters, operational business data, inventory,
billing, memory, or a React frontend.

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

## Formatting and linting

```powershell
python -m ruff check app tests alembic
python -m ruff format --check app tests alembic
python -m ruff format app tests alembic
```

## Activation and audit operations

Business completion is calculated from required profile fields and a complete,
valid seven-day schedule. PostgreSQL rejects activation while incomplete. Direct
activation is a manual platform-owner operation and must happen only after
offline payment confirmation; no billing record exists in this milestone.

Future tool arguments must be canonicalized and HMAC-SHA-256 signed with the
server-only `TOOL_CALL_AUDIT_HMAC_SECRET`. Only the digest is stored. Audit rows
retain no raw arguments, results, prompts, conversations, customer PII, or raw
errors. `TOOL_CALL_AUDIT_RETENTION_DAYS` defaults to 90; a future external
scheduler will call the existing retention operation.

## Next milestone

Business management and onboarding remain a separate milestone. Authentication
identifies a user and does not grant access to or create a business.
