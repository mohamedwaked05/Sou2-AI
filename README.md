# Sou2AI

Sou2AI is planned as a local AI assistant for small businesses, supporting English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed-language messages.

## Status

Milestone 7 is implemented. The backend includes authentication, isolated
multi-business onboarding, database-controlled lifecycle management, and one
persistent private owner conversation per business. Complete, confirmed active
businesses use local Ollama with `qwen2.5:7b` in development, retain full
chat history, and learn owner-reviewable permanent or expiring business facts.
Idempotent submissions, PostgreSQL-backed
turn ordering, cursor pagination, and tenant-scoped knowledge management are
covered by the integration suite.

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

Customer chat, cloud providers, RAG, embeddings, pgvector,
documents, live operational integrations and analytics, frontend functionality,
payments, and activation/admin HTTP endpoints remain future work. Manual lifecycle
changes use a controlled PostgreSQL function with permanent append-only history.
The local database uses separate bootstrap, restricted FastAPI runtime, and
restricted lifecycle-operator roles; the backend never connects as the database
owner or Docker bootstrap superuser.

The restricted operator also changes AI allowances through one controlled
PostgreSQL function with permanent append-only audit. Owners cannot change their
allowance through the API.

The planned architecture is a FastAPI backend, PostgreSQL with pgvector, local Ollama models, and a React frontend. The backend lives in [`backend/`](backend/README.md).

Local development uses deployment-wide Ollama for every business; startup remains
independent of Ollama. The mock provider is explicit and offline-only for tests or
offline development—there is no automatic fallback. Milestone 9 found critical
multilingual failures, so Qwen2.5 7B is not production-approved. Production will
later use an approved OpenAI model through this boundary; OpenAI is not implemented.

- [Project specification](PROJECT.md)
- [System architecture](ARCHITECTURE.md)
- [Agent development rules](AGENTS.md)
