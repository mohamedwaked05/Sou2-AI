# Sou2AI backend

Sou2AI is a local AI assistant planned for small businesses, with future support for English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language conversations.

## Current milestone

Milestone 7 adds PostgreSQL-backed registration and owner-generation limits,
per-business local-day AI token allowances, leased usage reconciliation, a
tenant-scoped current-usage endpoint, and controlled allowance administration. It
also establishes trusted host/proxy/CORS handling, a streamed body limit, server
request IDs, safe errors, security headers, environment-aware documentation, and
redacted console/JSON logging. Milestone 6 lifecycle controls and the verified
mock/local-Ollama owner chat remain intact.

Milestone 13 adds local BGE-M3 embeddings, atomic document embedding, internal
tenant-scoped exact pgvector retrieval, and an internal re-embedding queue/CLI.
It does **not** include customer chat, activation/admin APIs, cloud or paid model
providers, RAG answer generation, public retrieval endpoints, operational
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

The API's `POSTGRESQL_DATABASE_URL` must use the restricted runtime login. Keep
`MIGRATION_POSTGRESQL_DATABASE_URL` and lifecycle-operator credentials out of the
FastAPI process environment in deployed environments; they are separate
administrative credentials, not application configuration. Runtime connections
fail closed when configured as a PostgreSQL superuser, migrator, or lifecycle
operator.

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
topology. `TRUSTED_PROXY_CIDRS` is empty by default. Add only actual reverse-proxy
network ranges; forwarding headers from every other direct peer are ignored. Set
`TRUSTED_HOSTS` to the real API domain in production. Production also requires
explicit non-local CORS origins, disables `/docs`, `/redoc`, and `/openapi.json`
unless `API_DOCS_ENABLED=true`, and rejects wildcard hosts/origins. Enable HSTS
only with both `HSTS_ENABLED=true` and `TRUSTED_HTTPS_TERMINATION=true` after
confirming production HTTPS termination. Local HTTP must leave HSTS disabled.
Host validation trims and case-normalizes names, removes a final DNS dot, and
recognizes IPv4/IPv6 loopback forms consistently. CORS origins are parsed as
explicit HTTP(S) origins; user information, non-root paths, queries, fragments,
wildcards, and malformed origins are rejected.

The global current-endpoint request-body limit is 65,536 streamed bytes. CORS
allows only `GET`, `POST`, `PATCH`, `DELETE`, and `OPTIONS`, the
`Authorization`, `Content-Type`, and `Accept` request headers, and exposes
`X-Request-ID` plus `Retry-After`. Every response receives a new server-controlled
request UUID; client request IDs are ignored.

## Start PostgreSQL

The compose service uses `pgvector/pgvector:0.8.0-pg17`. The Milestone 11
migration enables `vector` in development and test databases. Documents store
tenant-scoped metadata and provider-neutral storage keys only; file bytes, upload,
parsing, embeddings, similarity indexes, retrieval, and RAG are not implemented.
Migration downgrade intentionally retains the extension because future objects may
depend on it.

From the repository root:

```powershell
docker compose up -d postgres
docker compose ps
docker compose exec postgres pg_isready
```

The initialization script creates `sou2ai_dev` and `sou2ai_test`. Development
data persists in the `sou2ai_postgres_data` volume. Local credentials in
`.env.example` are development defaults only. Tests refuse to run destructive
setup against any database not named `sou2ai_test`.

For an existing named volume created before the role-separated setup, provision
the roles and local logins idempotently once (this does not reset either database):

```powershell
docker compose exec postgres sh /docker-entrypoint-initdb.d/10-init-test-db.sh
```

The local role model is:

- Docker `sou2ai`: trusted bootstrap superuser used only to initialize roles and
  run migrations.
- `sou2ai_migrator`: `NOLOGIN` owner of protected lifecycle objects.
- `sou2ai_runtime`: `NOLOGIN` privileges inherited by
  `sou2ai_runtime_login`, which FastAPI uses.
- `sou2ai_lifecycle_operator`: `NOLOGIN` execute-only lifecycle privilege inherited
  by `sou2ai_lifecycle_operator_login`.

PostgreSQL superusers remain trusted bootstrap administrators and can bypass
ordinary grants. Neither FastAPI nor normal lifecycle operators may use one.

From `backend`, apply or roll back the schema:

```powershell
$env:MIGRATION_POSTGRESQL_DATABASE_URL = "postgresql+psycopg://sou2ai:local-bootstrap-password@127.0.0.1:5433/sou2ai_dev"
python -m alembic upgrade head
python -m alembic downgrade base
python -m alembic upgrade head
Remove-Item Env:MIGRATION_POSTGRESQL_DATABASE_URL
```

Use a secret-injection mechanism rather than command history for real deployment
credentials. Alembic loads this bootstrap-only variable separately; FastAPI's
settings object does not contain it.

## Run the API

```powershell
uvicorn app.main:app --reload
```

Open Swagger UI at <http://127.0.0.1:8000/docs>.

### Local Ollama owner chat

Local development uses deployment-wide `OWNER_CHAT_PROVIDER=ollama` for every
business. The deterministic mock is explicit and offline-only for tests or offline
development; Ollama failure never falls back to it. To use the local
complete-response provider, install/start Ollama and run:

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
- Current AI usage: `GET
  /api/v1/businesses/{business_id}/ai-usage/current`
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
$env:TEST_POSTGRESQL_DATABASE_URL = "postgresql+psycopg://sou2ai_runtime_login:sou2ai_runtime_local@127.0.0.1:5433/sou2ai_test"
$env:TEST_MIGRATION_POSTGRESQL_DATABASE_URL = "postgresql+psycopg://sou2ai:sou2ai_local@127.0.0.1:5433/sou2ai_test"
$env:TEST_LIFECYCLE_OPERATOR_POSTGRESQL_DATABASE_URL = "postgresql+psycopg://sou2ai_lifecycle_operator_login:sou2ai_lifecycle_operator_local@127.0.0.1:5433/sou2ai_test"
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

Registration adds independent PostgreSQL counters: five attempts per normalized
email/hour, 30 per client IP/15 minutes, and 100 per client IP/24 hours. Successful
and failed admissions count before password hashing and email delivery. Owner chat
permits three generation attempts per business/minute and 20/hour. A blocked
request returns `429`, a stable code, reset metadata, `Retry-After`, and the request
ID without calling the provider or creating an assistant/token charge.

Every business defaults to 20,000 input-plus-output tokens per stored-timezone
local day. Twenty-five percent is reserved for owner traffic; current owner chat
can use the full allowance, while future customer channels can use only the shared
portion. Before generation the database reserves conservative estimated input plus
`OWNER_CHAT_MAX_OUTPUT_TOKENS` (512 by default) for the 150-second generation
lease. Each provider estimates its complete canonical serialized input before
admission. For Ollama this includes system instructions, JSON/schema structure,
the full profile and schedule, knowledge categories/expiries, message roles,
request time, and JSON escaping. Ollama maps the output cap to
`options.num_predict`. Its non-authoritative fallback uses the same canonical
serialization and estimates about one token per three UTF-8 bytes. This
intentionally errs conservatively and is only approximate for Arabic,
Franco-Arabic, and mixed-language text. The serialized input is never persisted or
logged. A missing model or connection refusal known to occur before dispatch can
release a reservation. Timeouts, read/write/protocol/reset failures, generic HTTP
5xx responses, invalid responses, and other ambiguous failures charge the full
reservation unless authoritative reported usage is available, in which case that
usage is charged exactly once.

Usage records contain identifiers, channel/capability, counters, safe
provider/model identifiers, statuses, windows, and timestamps only. They never
store messages, prompts, answers, bodies, raw payloads, reasoning, authorization
data, or costs. Owner burst events retain for 24 hours, registration events for 48
hours, detailed usage for 90 days, and daily summaries for 12 months. The existing
PostgreSQL-coordinated best-effort maintenance function uses the database clock
and caps batches at 1,000; callers cannot provide a cutoff timestamp. The runtime
cannot directly mutate rate-event or usage tables, and invokes only controlled
admission, exact current-attempt undo, reservation/reconciliation, summary, and
cleanup functions. There is no internal scheduler.

The current usage endpoint keeps completed input/output/total counters separate,
but calculates availability percentage and status from completed plus currently
reserved tokens. Remaining tokens never go below zero; authoritative usage can
legitimately make the percentage exceed 100%.

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
run direct `UPDATE businesses SET status = ...` statements; column privileges deny
that bypass even if a client forges the former `sou2ai.lifecycle_*` custom settings.
Those settings are no longer used as security controls. Connect with the restricted
operator login and use only the schema-qualified function with `psql` variables so
values remain parameters. `-W` prompts without placing the password in command
history:

```powershell
docker compose exec postgres psql -U sou2ai_lifecycle_operator_login -W -d sou2ai_dev `
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
rejected calls write none. The operator has function execution only: it cannot
directly update `businesses.status` or insert, update, delete, or truncate history.
FastAPI cannot execute the function. History cannot be updated or deleted and
reasons are never returned by owner APIs. There is no admin HTTP endpoint or
dashboard.

### AI allowance administration

Owners may view but cannot change the current usage summary. FastAPI and the
operator cannot directly update `business_ai_allowance_configs` or mutate
`business_ai_allowance_audit`. Connect with the restricted operator login and call
only the controlled function; never run direct configuration updates:

```powershell
docker compose exec postgres psql -U sou2ai_lifecycle_operator_login -W -d sou2ai_dev `
  -v business_id="00000000-0000-0000-0000-000000000000" `
  -v daily_allowance="20000" `
  -v owner_reserve_percent="25" `
  -v admin_identifier="operator@example.com" `
  -v reason="Approved local allowance adjustment" `
  -c "SELECT * FROM public.sou2ai_change_business_ai_allowance(:'business_id'::uuid, :'daily_allowance'::integer, :'owner_reserve_percent'::integer, :'admin_identifier', :'reason');"
```

The function locks the business/configuration, validates bounded nonblank operator
and reason fields, changes both allowance values, and inserts exactly one permanent
append-only audit record atomically. Lowering below current usage never erases
usage; it blocks new reservations immediately while existing reservations finish.
The next local day uses the new value. `PUBLIC` and FastAPI cannot execute the
function. PostgreSQL superusers remain trusted bootstrap administrators.

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

Provider selection is deployment-wide `OWNER_CHAT_PROVIDER=mock|ollama`; changing
the provider or model requires a backend restart. Ollama uses the configured base
URL and model, a 120-second default HTTP timeout, JSON-schema structured output,
and complete non-streaming responses. Its generation lease defaults to 150 seconds
and must exceed the HTTP timeout. The application still validates every proposed
fact and remains authoritative for allowed knowledge categories. The provider-neutral
business profile includes the authoritative seven-day stored schedule, including
closed days and chronologically ordered local-time shifts. Provider-specific
request/response formats remain inside the adapter, failures remain generic to
owners, and no generation or provider failure is retried automatically.

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

Milestone 10 completes the owner-chat-specific provider boundary. Ollama remains
the local-development provider using `qwen2.5:7b`, but Milestone 9 found critical
multilingual failures, so Qwen2.5 7B is not production-approved. Production will
later use an approved OpenAI model through this boundary; no OpenAI integration is
implemented here. Authoritative provider token counts are preferred; otherwise the
existing conservative estimate is recorded as non-authoritative. Later RAG,
documents, controlled live tools and analytics, customer channels, and frontend
work remain planned only.
Authentication alone never grants business access without membership.

## Milestone 12 knowledge documents

Supported private sources are PDF, DOCX, and UTF-8 TXT. Uploads require a full-
access member of an `ACTIVE` business and return `202`; document metadata can be
listed, read, replaced, retried after failure, or permanently deleted at
`/api/v1/businesses/{business_id}/knowledge/documents`. There are no public files
or chunk endpoints. Validation checks filename, declared MIME type, signature,
structure, encryption, scanned PDFs, and limits (5 MiB, 100 pages, 500,000 text
characters, 500 chunks). Errors use safe stable codes.

Start infrastructure from the repository root with `docker compose up -d redis
postgres worker`. For Windows local development, run the API normally and, from
`backend`, run `.\.venv\Scripts\rq.exe worker --url redis://127.0.0.1:6379/0
knowledge`; one normal RQ worker processes one job at a time.
`KNOWLEDGE_STORAGE_ROOT` is private local development storage. Future S3 will use
the same storage interface but is not implemented. Run focused checks with
`python -m pytest tests/test_knowledge_documents.py` and the full suite with
`python -m pytest`.

## Milestone 13 embeddings and retrieval

Ollama embeddings use `POST /api/embed`, `EMBEDDING_PROVIDER=ollama`, and the
configured `EMBEDDING_MODEL=bge-m3`. Chunk/query content and vectors are never
logged. Documents become ready only after every deterministic chunk has a valid
1024-dimension embedding. To queue replacement embeddings without re-reading source
files, run `python -m app.rag.reembed --business-id <uuid>` or `--all` from
`backend`; this uses the existing `knowledge` RQ queue.

The local BGE-M3 evaluation uses a 16-document tenant corpus plus separate-tenant
leakage distractors and 30 multilingual questions. The repaired recorded result is
100% Recall@5/Recall@10 for every language group, 98.3% overall MRR, zero execution
failures, and zero leakage. Run it with `python -m app.rag.evaluate_retrieval`; it
exits unsuccessfully when a completion gate fails.
