# Sou2AI backend

Sou2AI is a local AI assistant planned for small businesses, with future support for English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language conversations.

## Current milestone

Milestone 6 adds authoritative `PENDING`, `ACTIVE`, and `DISABLED` business
lifecycle states, controlled manual PostgreSQL transitions, and permanent
append-only internal history. API `is_active` remains compatible but is derived
only from `status == ACTIVE`. Owner chat retains its deterministic offline mock and
opt-in local Ollama provider and requires an authenticated `FULL_ACCESS`
membership plus a complete, confirmed active business.

It does **not** include customer chat, activation/admin APIs, cloud or paid model
providers, RAG, embeddings, pgvector, documents, operational
integrations or analytics, tool execution, payments, frontend code, invitations,
or additional roles.

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

### Optional local Ollama owner chat

Owner chat defaults to `OWNER_CHAT_PROVIDER=mock`, which is deterministic and
offline. To use the local complete-response provider, install/start Ollama and run:

```powershell
ollama list
ollama pull qwen2.5:7b
ollama run qwen2.5:7b
```

`ollama list` verifies service availability and installed models; `ollama run`
provides a quick direct model check. Do not substitute `qwen2.5-coder:7b` for owner
chat. Configure the backend before starting it:

```powershell
$env:OWNER_CHAT_PROVIDER = "ollama"
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"
$env:OLLAMA_CHAT_MODEL = "qwen2.5:7b"
$env:OLLAMA_REQUEST_TIMEOUT_SECONDS = "120"
$env:OWNER_CHAT_GENERATION_LEASE_SECONDS = "150"
uvicorn app.main:app --reload
```

Startup does not contact Ollama. The service is called only for eligible owner-chat
generation, with `stream: false`; the API waits for one complete response. A
missing model or unavailable service returns the same retryable safe `503` as other
provider failures. Models are never pulled automatically.

For an opt-in end-to-end check, first authenticate, complete and manually activate
a test business through the existing workflow. Put only temporary values in the
current PowerShell process, then submit and read the persisted conversation:

```powershell
$env:SOU2AI_ACCESS_TOKEN = "replace-with-temporary-access-token"
$env:SOU2AI_BUSINESS_ID = "replace-with-active-business-uuid"
$headers = @{ Authorization = "Bearer $env:SOU2AI_ACCESS_TOKEN" }
$body = @{
    idempotency_key = [guid]::NewGuid().ToString()
    content = "What are my business opening hours on Saturday?"
} | ConvertTo-Json
$base = "http://127.0.0.1:8000/api/v1/businesses/$env:SOU2AI_BUSINESS_ID"
Invoke-RestMethod -Method Post -Uri "$base/owner-chat/messages" `
    -Headers $headers -ContentType "application/json" -Body $body
Invoke-RestMethod -Method Get -Uri "$base/owner-chat/messages" -Headers $headers
```

Use a new idempotency key for each new owner message; reusing the same key and
content intentionally returns the already stored turn with `replayed: true`. Do
not save or commit access tokens. Normal automated tests use mocked HTTP transports
and never contact `127.0.0.1:11434`.

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
- Owner chat: `POST/GET
  /api/v1/businesses/{business_id}/owner-chat/messages`
- Learned knowledge: `GET /api/v1/businesses/{business_id}/knowledge` and
  `PATCH/DELETE /api/v1/businesses/{business_id}/knowledge/{knowledge_id}`

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

Owner-chat tests use the provider contract without network access and exercise
PostgreSQL constraints, idempotency races, independent-session concurrency,
tenant isolation, cursor history, provider/persistence failures, and learned-fact
lifecycle behavior.

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
business, `FULL_ACCESS` creator membership, and its single owner conversation
atomically. Schedule replacement is transactional, and simultaneous profile
changes use row-level serialization with intentional last-write-wins behavior.
PostgreSQL also prevents an active business from ending a transaction with an
incomplete profile after either profile-field or schedule edits.

Lifecycle status is the only stored activation source of truth. Operators must not
run direct `UPDATE businesses SET status = ...` statements; a database trigger
rejects that bypass. Connect with the authorized PostgreSQL operator role and use
the schema-qualified function with `psql` variables so values remain parameters:

```powershell
docker compose exec postgres psql -U sou2ai -d sou2ai_dev `
  -v business_id="00000000-0000-0000-0000-000000000000" `
  -v admin_identifier="operator@example.com" `
  -v reason="Offline payment received" `
  -c "SELECT * FROM public.sou2ai_change_business_status(:'business_id'::uuid, 'ACTIVE'::business_status, :'admin_identifier', :'reason');"
```

Use `ACTIVE` for initial activation after complete confirmed onboarding. Use the
same command with `DISABLED` and an appropriate reason to disable an active
business, or with `ACTIVE` to re-enable an eligible disabled business. Allowed
transitions are only `PENDING -> ACTIVE`, `ACTIVE -> DISABLED`, and `DISABLED ->
ACTIVE`. Every successful call atomically writes one internal history record;
rejected calls write none. History cannot be updated or deleted and reasons are
never returned by owner APIs. There is no admin HTTP endpoint or dashboard.

## Owner chat and learned knowledge

`POST .../owner-chat/messages` requires a 1-200 character client idempotency key
and a nonblank owner message of at most 4,000 characters after trimming. Original
message text is preserved. Assistant messages retain an internal 14,000-character
database allowance. The key is database-unique within the business's one
conversation. Replays with identical content reuse the stored result; different
content returns a safe conflict. Provider failure retains the owner message and
returns a retryable `503` without inventing an assistant response.

History stores every message and returns the newest 50 per page through an opaque
stable cursor. Pages are deterministic by logical sequence and UUID even when
timestamps match. Only the newest 12 messages enter provider context. Active,
non-expired knowledge candidates are PostgreSQL-filtered, bounded by
`OWNER_CHAT_KNOWLEDGE_CONTEXT_LIMIT` (100 by default), and deterministically ranked
for the current message.

Generation uses short database transactions and persisted per-turn claims. A
conversation row lock selects only the earliest unfinished turn, then commits
before the provider runs. `OWNER_CHAT_GENERATION_LEASE_SECONDS` allows recovery
after a crashed claimant, while `OWNER_CHAT_GENERATION_WAIT_SECONDS` bounds inline
waiting. This coordinates independent replicas without Redis, workers,
process-local locks, or holding a pooled connection during provider work.

Provider selection is `OWNER_CHAT_PROVIDER=mock|ollama`. Ollama uses the configured
base URL and model, a 120-second default HTTP timeout, JSON-schema structured
output, and complete non-streaming responses. Its generation lease defaults to 150
seconds and must exceed the HTTP timeout. The application still validates every
proposed fact and remains authoritative for allowed knowledge categories. The
provider-neutral business profile includes the authoritative seven-day stored
schedule, including closed days and chronologically ordered local-time shifts.

Learned facts have a normalized tenant-unique subject, allowed stable category,
owner-chat provenance, and permanent or temporary lifecycle. Temporary facts need
a future timezone-aware expiry; explicit “today” uses the business timezone.
Expired facts stay visible for owner management but never enter model context.
Repeated subjects update in place and preserve `created_at`. Live stock, revenue,
orders, sales, best sellers, restocking, appointment availability, and similar
changing values are rejected by application allowlists and remain the concern of
future controlled tenant-scoped tools.

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

## Implementation boundary

Milestone 6 and the early optional local Ollama provider are complete. Much of the
later provider abstraction also arrived early, without completing unrelated
roadmap work. Later RAG,
documents, cloud model connectivity, controlled live tools and analytics, customer
channels, and frontend work remain planned only. Ollama is a local-development
provider, not the production deployment decision.
Authentication alone never grants business access without membership.
