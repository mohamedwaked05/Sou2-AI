# Project name

Sou2AI

## Vision

Sou2AI is a local-first AI operations assistant for small businesses.

It begins as a RAG-powered business knowledge assistant and will later grow into an agent capable of interacting with structured business data and safely performing business operations.

## Problem

Small businesses often manage product details, prices, policies, orders, customer information, and communications across disconnected documents and messaging applications.

Sou2AI aims to provide one natural-language interface for finding information and, in later phases, taking controlled actions.

## Initial target users

* Small shop owners
* Instagram and WhatsApp sellers
* Home businesses
* Retail stores
* Small service businesses
* Lebanese and Arabic-speaking business owners

## Supported languages

* English
* Arabic
* Lebanese Arabic
* Franco-Arabic or Arabizi
* Mixed Arabic and English
* Mixed Franco-Arabic and English

## Local-first principle

During development, the application runs locally without paid AI APIs.

Planned local AI stack:

* Ollama
* Qwen2.5 7B
* BGE-M3

## Technical stack

Backend:

* Python
* FastAPI
* Pydantic
* PostgreSQL
* pgvector

AI:

* Ollama
* Qwen2.5 7B
* BGE-M3
* Custom RAG pipeline
* Agent tools in later milestones

Frontend:

* React
* Vite

Testing and quality:

* pytest
* Ruff

## Current status

Milestone 7 is implemented on the authenticated PostgreSQL platform. Each
business has one authoritative `PENDING`, `ACTIVE`, or `DISABLED` status;
`is_active` is derived only for response compatibility. Controlled direct
PostgreSQL administration performs manual activation, disabling, and re-enabling
with permanent append-only internal history. Owner chat requires a complete,
confirmed `ACTIVE` business and `FULL_ACCESS` membership. PostgreSQL now enforces
registration and owner-generation limits plus per-business local-day AI token
allowances with leased reservations, a protected 25% owner reserve, permanent
allowance-change audit, and privacy-minimal retained accounting. The API also has
server request IDs, CIDR-aware proxy trust, trusted hosts, streamed body limits,
restricted CORS/docs, safe errors, security headers, and environment-aware
redacted logging. Rate admission and retention are exposed to the restricted
runtime only through narrow PostgreSQL functions; cleanup uses the database clock
and cannot be redirected at current records by caller-supplied time.

Milestone 8 Ollama owner-chat connectivity was completed early. Much of the
Milestone 10 provider abstraction, and the usage/cost-control portion of
Milestone 20, is also present early. Unrelated later roadmap work remains planned
rather than complete.

Milestone 9 is complete. Its fixed fictional-business baseline recorded critical
multilingual failures for Qwen2.5 7B. The completed scoring is accurately
disclosed as AI-assisted (with human acceptance required), and Qwen2.5 7B is not
production-approved.

The current repository does not yet contain:

* RAG answer generation
* Agent tools
* React functionality
* WhatsApp integration

Later roadmap milestones remain planned, not implemented.

## Development roadmap

### Milestone 1: Backend foundation — Complete

* FastAPI application structure
* Versioned API routing
* Configuration foundation
* Health endpoint
* Testing and formatting foundation

### Milestone 2: PostgreSQL platform foundation — Complete

* Docker Compose creates separate `sou2ai_dev` and `sou2ai_test` databases.
* SQLAlchemy and Psycopg provide scoped synchronous database sessions.
* Alembic owns native enums, foundational platform tables, constraints, indexes, and database triggers.
* Users, business profiles, single-owner MVP memberships, weekly opening hours, and privacy-minimal tool-call audit metadata are represented.
* Profile completion is derived from current data; activation is guarded in PostgreSQL so direct SQL cannot activate an incomplete business.
* Database health, schedule normalization, retention, migration, model, and API behavior are covered by automated tests using `sou2ai_test`.

This milestone does not add authentication or business-creation endpoints, pgvector, RAG, AI tools/adapters, operational business data, inventory, billing, or a platform-admin dashboard.

### Milestone 3: User authentication — Complete

* Add email-and-password registration with normalized, unique email addresses.
* Hash passwords securely and never store or log plain-text credentials.
* Add login, short-lived access tokens, refresh-token rotation or revocation, logout, and a current-user endpoint.
* Protect authenticated endpoints and return consistent authentication errors.
* Test duplicate registration, invalid credentials, malformed or expired tokens, refresh behavior, logout, and protected routes.

Authentication identifies the user; it does not by itself grant access to a business.

### Milestone 4: Business management and onboarding — Complete

* Allow an authenticated user to create and own multiple businesses.
* Create the business and its full-access owner membership in one transaction.
* Keep new businesses pending and inactive.
* Add endpoints to create, list, view, and update businesses.
* Collect the required profile, controlled Lebanese location, category, and all seven working days.
* Support closed days and up to three valid, non-overlapping shifts per open day.
* Return derived profile-completion status and enforce the agreed exact-duplicate business-name rule.
* Test the complete onboarding flow.

### Milestone 5: Owner chat and learned business knowledge — Complete

* Provide exactly one private main owner conversation for every business.
* Persist owner and assistant messages and return stable 50-message cursor pages.
* Send only the newest 12 messages through a replaceable provider contract, with
  the deterministic offline mock as default and local Ollama as an opt-in provider.
* Require authentication, `FULL_ACCESS`, profile completion, and existing manual
  activation before chat.
* Enforce database idempotency and PostgreSQL-backed ordered generation across API
  replicas without holding a transaction during provider work.
* Learn only allowed stable permanent or clearly expiring temporary facts, update
  duplicate subjects, exclude expired facts from context, and provide owner review,
  edit, and deletion APIs.
* Reject live operational values such as stock, revenue, orders, sales, restocking,
  and appointment availability; future controlled integrations remain their source.
* Prevent active businesses from becoming profile-incomplete through profile or
  schedule edits.
* Support opt-in local `qwen2.5:7b` owner-chat generation through Ollama while
  retaining the deterministic mock as the default provider.

### Milestone 6: Business lifecycle and manual activation — Complete

* Use `PENDING`, `ACTIVE`, and `DISABLED` as the authoritative stored states;
  derive `is_active` only in API responses.
* Preserve PostgreSQL profile-completion protection and require confirmed
  onboarding for activation and re-enabling.
* Perform manual lifecycle changes only through the controlled PostgreSQL
  function; PostgreSQL ownership and grants separate migration, FastAPI runtime,
  and lifecycle-operator privileges, and the MVP has no platform-admin HTTP API
  or dashboard.
* Block pending and disabled businesses from paid AI functionality independently
  for every tenant business.
* Record every successful change with operator, reason, transition, and timestamp
  in permanent append-only internal lifecycle history.

Online payments are not implemented for the MVP. Lifecycle reasons are internal
and are not exposed through owner-facing APIs.

### Milestone 7: API security foundation - Complete

* Standardize validation and safe error envelopes with a server-generated request
  UUID on every response.
* Enforce explicit CORS, trusted hosts, CIDR-based proxy trust, streamed 65,536-byte
  request bodies, security headers, and environment-controlled API documentation.
* Apply PostgreSQL-backed concurrent registration and owner-generation limits with
  stable `429` errors, reset metadata, and `Retry-After`; runtime access is limited
  to controlled admission and exact owner-admission undo functions rather than
  direct rate-event mutation.
* Give every business a protected 20,000-token local-day allowance, 25% owner
  reserve, leased pre-generation reservations, atomic reconciliation, and an
  authenticated tenant-scoped current-usage endpoint. Reservation input is
  estimated from each provider's complete canonical serialized input, and usage
  percentage/status include both completed and currently reserved tokens.
* Change allowances only through the restricted database operator function and
  retain permanent append-only audit history.
* Retain owner burst events for 24 hours, registration events for 48 hours,
  detailed AI usage for 90 days, and daily summaries for 12 months through
  PostgreSQL-clock, capped-batch coordinated maintenance without an internal
  scheduler or caller-controlled cutoff.
* Emit readable safe development logs, quiet test logs, and one JSON object per
  production event with central defense-in-depth secret redaction.

Upload-specific limits remain deferred to Milestone 12 because no upload endpoint
exists. Customer and WhatsApp limits remain deferred to Milestone 19.

### Milestone 8: Local Ollama connectivity

The owner-chat path now connects to the configurable local Ollama HTTP API through
the provider boundary, uses `qwen2.5:7b`, handles provider failures, and uses mocked
HTTP transports in automated tests. It does not add startup probes, a model-status
endpoint, or a separate generation-demo endpoint.

This milestone does not add RAG, memory, agent tools, or business-answering logic.

### Milestone 9: Arabic and Franco-Arabic model evaluation — Complete

* A version-controlled 50-scenario dataset uses the same 10 semantic cases in
  English, Arabic, Lebanese Arabic, Franco-Arabic, and mixed language.
* The runner exercises the existing production Ollama provider and prompt
  directly against one deterministic fictional business fixture.
* Deterministic warnings support, but do not replace, human scoring of intent,
  relevance, hallucination, clarification, tone, and instruction following.
* The completed baseline found critical failures. Its scoring artifact is
  AI-assisted and requires human acceptance; Qwen2.5 7B is not approved for
  production use.

### Milestone 10: Model-provider abstraction — Complete

* Owner chat uses a provider-neutral Protocol for requests, results, usage, and
  normalized failures; provider-specific formats remain inside adapters.
* Local development selects deployment-wide Ollama for every business, using
  `qwen2.5:7b`; mock is explicit and development/testing-only.
* Provider and model changes are loaded at startup and require a backend restart.
* Shared contract tests cover both mock and Ollama with mocked HTTP transport.
* Owner-facing provider failures are generic. There are no automatic retries or
  mock fallbacks. Usage prefers valid authoritative counts and otherwise records
  a labelled estimate.

Qwen2.5 7B is not production-approved after Milestone 9's critical failures.
Production will later use an approved OpenAI model through this boundary; OpenAI
integration is not implemented in this milestone.

### Milestone 11: pgvector and knowledge storage — Complete

* Enable pgvector and add tenant-scoped document and chunk models with nullable `vector(1024)` embeddings.
* Store safe file references, processing state, traceable source metadata, chunk order, and embedding metadata.
* Add migrations, constraints, and indexes for tenant-safe retrieval.
* Keep document metadata and chunks separate from `business_knowledge`, profiles, chat history, and live operational data.

### Milestone 12: Secure document ingestion and chunking — Complete

* Private tenant-scoped PDF, DOCX, and UTF-8 TXT uploads use local storage keys, Redis/RQ processing, safe extraction, and deterministic chunks; S3 remains a future provider.
* Source document APIs require authenticated full access to an ACTIVE business, preserve safe errors, support retry/replacement/deletion, and never expose source files or chunks.

### Milestone 13: Embeddings and vector retrieval  Complete

* Connect BGE-M3 locally behind an embedding-provider interface.
* Generate and store chunk embeddings in pgvector.
* Implement similarity search that always filters by authorized `business_id`.
* Configure result count, similarity threshold, and embedding model.
* Support re-embedding and evaluate retrieval independently from generation.
* BGE-M3 evaluation recorded Recall@10 of 100% in every language group, Recall@5
  of 100% in every language group, MRR of 91.7% English and 100% for Arabic,
  Lebanese Arabic, Franco-Arabic, and mixed-language questions (98.3% overall),
  zero execution failures, and zero PostgreSQL-backed cross-tenant leakage.

### Milestone 14: In Progress — RAG question-answering flow

* Authenticate the user and authorize the selected business.
* Retrieve relevant chunks and combine them with trusted business-profile facts.
* Return grounded answers with traceable source references.
* Clarify ambiguous questions and refuse to invent unsupported answers.
* Defend against document prompt injection and cross-tenant retrieval.
* Evaluate correct, missing, and conflicting knowledge in all supported languages.

RAG covers relatively stable knowledge such as policies, delivery information, FAQs, warranties, and documents. It is not the source of truth for current stock, today's sales, orders, or other changing facts.

### Milestone 15: React business interface — Complete

* Add registration, login, session handling, and multi-business selection.
* Add business creation, profile editing, onboarding, and working-hours setup.
* Display profile completion and business activation state.
* Add document upload, processing status, chat, and answer sources.
* Show accessible loading, empty, success, and error states.
* Prevent inactive businesses from using paid AI features.

The `frontend/` Vite application provides the authenticated business interface,
uses memory-only access tokens with refresh-cookie restoration, and includes the
responsive light/dark UI, onboarding, owner-chat, knowledge-document, account,
and truthful future-feature states. The business profile persists the selected
default language; Alembic revision `20260822_05` restores the database and API
completion invariant.

### Milestone 16: Controlled operational integrations

* Define stable Sou2AI operational contracts for products, inventory, sales, best-seller rankings, and restocking recommendations.
* Build a separate fake PostgreSQL store database with realistic Lebanese minimarket products, stock, sales, and restocking rules.
* Treat the fake database as an external source system, not part of `sou2ai_dev`.
* Connect with a dedicated read-only database user through a PostgreSQL adapter.
* Implement predefined, parameterized read operations; never let the model inspect schemas, generate arbitrary SQL, or receive database credentials.
* Normalize the fake store's schema into the standard Sou2AI operational contracts so agent tools do not depend on its table or column names.
* Support future adapters for prebuilt POS/ERP connectors, business APIs, custom database mappings, and controlled CSV or Excel imports.
* Include mapping semantics such as completed-sale status, returns, reservations, branches, warehouses, currencies, and timezones—not only column names.
* Validate connections and mappings before activation and expose integration health.
* Apply tenant authorization, query timeouts, row limits, and safe error handling.
* Avoid copying live operational records into the Sou2AI platform database; retain only required configuration and privacy-minimal audit metadata.
* Clearly report when a source lacks the data needed for an answer or recommendation.

The first integration proves the contract and adapter design. Sou2AI will add connectors based on real customer demand rather than promise compatibility with every store system in the initial MVP.

### Milestone 17: Agent tool calling

* Add a controlled agent loop with an explicit registry of approved tools.
* Start with read-only tools for current inventory, sales summaries, best sellers, and restocking recommendations.
* Validate strict input and output schemas outside the model.
* Authenticate the user, authorize the business and operation, and require an active business before execution.
* Apply timeouts and result-size limits; forbid arbitrary SQL, URLs, code, and tool names.
* Write exactly one privacy-minimal audit record per success, error, denial, or timeout through the centralized executor.
* Require explicit user confirmation before any future sensitive or destructive write action.

### Milestone 18: Conversation history and memory

* Store tenant-scoped conversations and messages with user or channel identity.
* Define retention, short-term context, and limited approved long-term memory.
* Prevent cross-business conversation access.
* Never allow memory or a previous AI answer to override current structured data, retrieved sources, or live tool results.
* Keep the latest 12 messages verbatim and compress older messages into a short
  rolling summary with a checkpoint of the latest summarized sequence.
* Never delete original messages because of summarization; update summaries in
  controlled batches and preserve the last valid summary on failure.
* Charge summary-generation tokens to the business allowance and scope summaries
  by tenant, channel, and conversation.
* Never leak customer conversations into owner or other customer context. Treat
  summaries as untrusted conversational context: current profile data, approved
  knowledge, RAG sources, and live tool results override them.
* Test summary ordering, concurrency, retry, isolation, and hallucination
  boundaries.

### Milestone 19: External customer messaging integrations

* Add channels such as WhatsApp only after the core assistant is reliable.
* Verify webhooks and map each channel connection and conversation to one business.
* Separate external-customer permissions from owner-only tools and information.
* Add rate limits, deduplication, retry handling, human handoff, and privacy rules.
* Preserve message source, answer sources, and safe tool traceability.
* Keep the owner-generation limit separate from customer limits; scope
  customer/WhatsApp limits per customer conversation and add an aggregate
  business customer-channel burst limit.
* Deduplicate webhook deliveries before counting them. Rejected pre-generation
  messages consume no AI tokens.
* Share the daily business token allowance across every channel while preventing
  customers from consuming the owner reserve.
* Decide exact customer and WhatsApp ceilings when their webhook and concurrency
  architecture is implemented.

### Milestone 20: Production readiness and cloud deployment

* Define reproducible development, test, staging, and production environments.
* Use managed PostgreSQL and an approved cloud model behind provider abstractions.
* Protect secrets, HTTPS, domains, proxies, CORS, and controlled migrations.
* Add safe centralized logging, monitoring, alerts, backups, restore tests, disaster recovery, rollback procedures, and security scanning.
* Add per-business usage limits and cloud-model cost controls.
* Run tenant-isolation, security, load, and reliability reviews before release.
* Preserve manual offline payment for the MVP and local Ollama development.

Milestone 7 implements the provider-neutral usage metadata and per-business
allowance foundation early. It does not complete the unrelated deployment,
monitoring, backup, cloud-provider, or operational-readiness work in this
milestone.

## Main user use case

A business owner creates an account, creates and completes one or more business profiles, and waits for manual activation after offline payment. For an active business, the owner can upload stable knowledge such as policies and FAQs, ask questions in supported languages, receive source-grounded answers, and query live operational facts through approved read-only tools.

Sou2AI authenticates the user, isolates the selected business, chooses RAG or a controlled operational adapter, and never guesses when trusted data is unavailable.

## UML-style use-case view

```mermaid
flowchart LR
    OWNER[Business owner]
    ADMIN[Platform administrator]
    CUSTOMER[External customer]
    SYSTEM((Sou2AI))

    OWNER -->|Register and manage businesses| SYSTEM
    OWNER -->|Upload knowledge and ask questions| SYSTEM
    OWNER -->|Query live operations| SYSTEM
    ADMIN -->|Activate or disable businesses| SYSTEM
    CUSTOMER -.->|Use an approved future channel| SYSTEM

## Milestone rules

* Complete and verify one milestone before starting the next.
* Keep each milestone limited to its stated scope.
* Update this file when milestone order or scope changes.
* Do not treat planned functionality as implemented.
* New ideas are allowed, but they must be discussed and placed in the appropriate milestone before implementation.

## Core reliability principles

* Never fabricate business information.
* Retrieve business knowledge before answering.
* Use structured database tools for changing business facts.
* Provide sources for RAG answers.
* Ask for missing information.
* Confirm destructive actions.
* Keep the LLM replaceable.
* Develop one milestone at a time.
