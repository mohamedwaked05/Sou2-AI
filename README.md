# Sou2AI

Sou2AI is planned as a local AI assistant for small businesses, supporting English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language messages.

## Status

Milestones 15–18 are implemented. The backend includes authentication, isolated
multi-business onboarding, database-controlled lifecycle management, and multiple
retained private owner conversations per business. Complete, confirmed active
businesses use the selected provider, retain full chat history, and learn
owner-reviewable permanent or expiring business facts.
Idempotent submissions, PostgreSQL-backed
turn ordering, cursor pagination, and tenant-scoped knowledge management are
covered by the integration suite.

Milestone 18 adds deterministic conversation titles, archive-without-delete,
selected-conversation APIs and UI, and an asynchronous tenant-scoped rolling
summary. The latest 12 eligible messages remain verbatim; older complete turns are
summarized through Redis/RQ without modifying originals. Summary usage has a
separate shared-allowance reservation, and current tools/profile/RAG/current input
always outrank untrusted conversation memory.

Registration and owner-generation abuse controls, per-business local-day AI
token allowances, leased usage reconciliation, and the owner usage-summary API
are PostgreSQL-backed and safe across API replicas. Narrow database functions own
rate admission and database-clock retention; the runtime cannot directly mutate
rate or usage records. Provider reservations use the complete canonical serialized
model input, ambiguous post-dispatch failures are conservatively charged, and the
usage percentage includes both completed and reserved tokens. The HTTP boundary
now uses server request IDs, explicit trusted hosts/CORS, CIDR-based proxy trust,
streamed body limits, safe errors, security headers, environment-controlled
documentation, and redacted environment-aware logging. Production host and CORS
checks normalize case, DNS trailing dots, and IPv4/IPv6 loopback representations
before rejecting local or malformed values.

Milestone 17 adds four controlled read-only owner-chat tools on top of Milestone
16's provider-neutral operational contracts and read-only adapter
for a separate deterministic fake-store PostgreSQL source, and tenant-scoped Data
Sources management. Deployment-managed allowlisted profile keys keep connection
credentials out of the public API and browser; a versioned semantic mapping must
validate before activation. The responsive React interface can configure,
validate, activate, health-check, and disable the demonstration source. Live
operational records remain in that external database and are never copied into
Sou2AI. The centralized executor strictly validates tool requests, requires an
active healthy tenant source, bounds results and time, and writes only HMAC-hashed
privacy-minimal audits. Customer chat, additional cloud providers, payments, and
activation/admin HTTP endpoints remain future work. Manual lifecycle
changes use a controlled PostgreSQL function with permanent append-only history.
The local database uses separate bootstrap, restricted FastAPI runtime, and
restricted lifecycle-operator roles; the backend never connects as the database
owner or Docker bootstrap superuser.

The restricted operator also changes AI allowances through one controlled
PostgreSQL function with permanent append-only audit. Owners cannot change their
allowance through the API.

The architecture includes a FastAPI backend, PostgreSQL with pgvector, local Ollama models, and a React frontend. The backend lives in [`backend/`](backend/README.md), and the frontend lives in [`frontend/`](frontend/README.md).

PostgreSQL development uses pgvector. Tenant-owned document metadata and normalized
chunks are separate from owner-chat `business_knowledge`; PostgreSQL stores only
provider-neutral storage keys, never file bytes or public URLs. Upload, parsing,
embeddings, retrieval, and RAG remain future work.

Local development uses deployment-wide Ollama for every business; startup remains
independent of Ollama. The mock provider is explicit and offline-only for tests or
offline development—there is no automatic fallback. Milestone 9 found critical
multilingual failures, so Qwen2.5 7B is not production-approved. Production will
later use an approved OpenAI model through this boundary; OpenAI is not implemented.

- [Project specification](PROJECT.md)
- [System architecture](ARCHITECTURE.md)
- [Agent development rules](AGENTS.md)

## Milestone 12: knowledge documents

Authenticated full-access members of active businesses can upload private PDF,
DOCX, and UTF-8 TXT knowledge sources. Redis/RQ runs a separate processing worker;
the API returns `202` after durable storage and metadata creation. Files are kept
outside public directories under provider-neutral keys, validated before queuing,
and normalized into internal chunks only—there are no downloads, embeddings, or
RAG endpoints. Limits are 5 MiB, 100 PDF pages, 500,000 extracted characters, and
500 chunks. Failed documents have safe codes and may be retried; a replacement
keeps the old READY version until its successor is READY. Deletion is permanent.
