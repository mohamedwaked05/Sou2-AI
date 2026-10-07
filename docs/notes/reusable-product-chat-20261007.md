# Approved catalogue search in owner chat — 2026-10-07

Implemented on `feat/reusable-product-mappings` from `bdd5ae2`. This handoff
supersedes the earlier notes' pending implementation/startup status; it does not
alter their historical evidence or attempt ledger. No paid provider request,
merge, push or deployment occurred in this task.

## Implemented and reviewed

- `ProductMapping.capability` is required and remains the literal `products`.
  Gemini's instruction derives that value from the canonical contract. The
  transmitted schema requires the field and expresses literal constraints as
  singleton enums, matching Google's documented schema subset. Strict validation
  rejects `catalogue` and missing capability without rewriting or retrying.
  Serialized HTTP regressions cover the observed HTTP-200/STOP invalid response
  with reported usage 824/313/1,137, and a valid `products` response.
- The typed `product_search` registry operation declares `products` and dispatches
  through `MappedProductSource` and `execute_product_search`. Availability and
  execution check approved mappings, tenant binding, restricted permissions,
  current schema/source fingerprints and cancellable deadlines. The legacy
  PostgreSQL demo still exposes its original four inventory/sales operations.
  No source-specific SQL, table names or intent keyword router was added to chat.
- The model proposes only a bounded search phrase. Backend-held ambiguity records
  contain the actual offered string IDs and catalogue fields, business/user/
  conversation/source scope, source timestamp, approved version, schema/source
  fingerprints and expiry. Only displayed choices are offered. `eh` preserves
  choices and the original expiry. An explicit unique offered name, SKU or ID
  resolves to the backend-held identifier and re-reads the approved source.
  Unoffered, expired, stale and cross-scope choices cannot execute. Cancellation,
  unrelated requests, malformed-plan recovery and timeout recovery retain the
  established accounting and terminal replay behavior.
- Catalogue answers render validated names, identifier, optional SKU and category
  labels deterministically. Stock and prices are unknown; inventory, pricing,
  restocking and sales operations remain disabled. Catalogue reads require no
  resolver or synthesis generation.

Google documents string `enum` support in the
[GenerateContent JSON schema subset](https://ai.google.dev/api/generate-content).
This change does not claim that the historical failure's cause was proven, or
that a valid live Gemini mapping now exists.

## Actual offline verification

All generation used mocked transport; mapped-source queries were real.

| Check | Result |
| --- | --- |
| Combined backend regression selection below | **702 passed**, one upstream Starlette/httpx deprecation warning, 112.78 s |
| Focused mapping/provider/migration/historical tests | **49 passed**, one warning, 21.01 s |
| Frontend `npm test` | **43 passed** |
| Frontend `npm run typecheck`, `lint`, `build` | Passed |
| Backend Ruff check and format check | Passed |
| `git diff --check` | Passed |
| Frontend `npm run format:check` | Existing failures in unchanged `README.md` and `src/workspace.tsx`; preserved |
| Linux backend startup + host health + restricted SQL source | Passed; host HTTP 200, ODBC 18 encrypted connection, Hila READ_ONLY |

The combined command ran inside the Python 3.14/ODBC 18 test container, with
isolated PostgreSQL and Redis and stdin-only private source credentials:

```powershell
python -m pytest -q --tb=short tests/test_ai_usage.py tests/test_api_rate_limits.py tests/test_customer_channels.py tests/test_customer_channels_coverage.py tests/test_data_sources.py tests/test_health.py tests/test_mapping_migration.py tests/test_operational_contracts.py tests/test_operational_postgresql.py tests/test_operational_tools.py tests/test_owner_chat.py tests/test_owner_chat_provider.py tests/test_owner_chat_routing.py tests/test_owner_operational_reliability.py tests/test_source_mapping.py tests/test_historical_catalogue.py
python -m ruff check app tests alembic
python -m ruff format --check app tests alembic
```

The authenticated `POST /owner-chat/messages` flow tested real PostgreSQL tables
with opaque names, a differently named SQL Server schema, and Hila's restricted
historical catalogue. It verified all **13 source-backed Pepsi variants**,
`eh`, unique selection and unknown stock. A fictional product changed between
offer and selection, proving selection re-reads rather than echoing stale fields.
Replaying search, ambiguity and selection left full message/tool/reservation/
usage/mapping/rate-limit/knowledge snapshots unchanged, with zero provider calls
or source queries. Additional tests rejected nine invalid scope/provenance/expiry
variants, a genuinely reapproved mapping, guessed IDs and unavailable source
permissions/schema/timeouts. Failure/cancellation and unsupported stock tests
passed. An initial combined run found a stale test stub and missing isolated
Redis; both were corrected before the passing run. No shared queue was used.

The existing React submission shape already calls this endpoint and was inspected;
frontend tests/static/build checks passed. A browser UI walkthrough was not run.
Mocked generation plus real SQL proves backend safeguards and integration, not
live semantic classification or end-to-end live onboarding.

## Migration and resource compatibility

No migration or dependency/version change was added. The existing
`20261007_16` migration upgraded a fresh disposable database successfully. Its
ownership, restricted ACLs, admission function and active-source uniqueness
passed regression checks. Existing clarification JSON storage accommodates the
new typed state. Existing application-written approved mappings already serialize
`capability: products`; manually authored mappings missing the field now fail
strict validation. Revision 16's existing downgrade guard refuses rollback while
mapped configurations/revisions exist; do not delete customer configuration to
bypass it.

Shared development remains `20261006_15`, shared test `20261007_16`, and main
remains unchanged. Running this branch against development requires the already
existing revision 16 through the normal migration workflow; this task did not
migrate shared development. Source READ_ONLY state, reader grants, existing SQL
databases and shared queues were preserved. Only manifest-owned disposable
application PostgreSQL/Redis containers and the fictional SQL Server fixture
database/login were cleaned up after evidence capture. No forced session cleanup,
restore or imported jobs/procedures occurred. The before/after source-state and
grant snapshots matched.

Private credentials, raw catalogue records, provider payloads, XML/log evidence,
DPAPI files and helpers remain ignored locally. Only redacted outcomes are
committed. Unrelated working-tree notes were left unstaged.

## Verified local startup path

Native Windows remains blocked: `pyodbc.drivers()` lacks Driver 18 and the
OneDrive-backed SQLAlchemy SQL Server dialect import raises `OSError [Errno 22]`.
No Windows privileges, Python version or installation restrictions changed.

The working path uses the existing backend Dockerfile/image and a read-only
mount of the current application, Python 3.14 and Driver
`libmsodbcsql-18.7.so.1.1`. A Uvicorn backend with restricted PostgreSQL runtime
credentials started successfully. Its container runtime connected to SQL Server
through the restricted reader, checked four approved metadata objects and schema
provenance, and confirmed Hila READ_ONLY. A separate credential-free loopback
proxy made `/api/v1/health` reachable from the Windows host with HTTP 200 at
`127.0.0.1:18789`. Both providers were mock and no Gemini key was injected.

The backend shared the local SQL container network namespace and used
`127.0.0.1:1433`, ODBC Driver 18 and `Encrypt=yes`. The existing
`TrustServerCertificate=yes` exception applies only to this loopback development
source. Remote source certificate verification and global TLS policy were
unchanged. Existing DPAPI credentials were decrypted only in memory, sent on
stdin and installed in the application process environment, absent from Docker
environment metadata, command arguments and images. Migrator credentials were
excluded from the startup backend. See the
[backend launch recipe](../../backend/README.md#local-sql-server-backend-with-odbc-18).

## Concrete live plan requiring separate authorization

The historical record remains **23/25 cumulative attempts**, byte-for-byte
unchanged. Its SHA-256 is
`7A6092085F6A4B36DDA83CC96B286F7D8B6CD468B17C1FDB477CBF2CA30F28B0`.
No new live attempt was dispatched or counted as success here.

The minimum proposed flow needs **five new attempts**, starting from 23 and ending
at no more than **28**. The existing two slots cover only a valid proposal and
initial chat search. Completing this plan requires an additional **three slots
above the prior ceiling of 25**, with explicit authorization for **at most five
new paid attempts and cumulative ceiling 28**.

| Stage | Paid attempts | Required evidence |
| --- | ---: | --- |
| Mapping proposal, existing approved metadata only | 1 | Required `products`, strict application validation, independently reviewed semantics |
| Approve/activate/discover/source reads | 0 | Normal authenticated lifecycle, existing restricted reader, source provenance |
| Owner chat: `Find Pepsi` | 1 planner | Exact verified variants and scoped persisted ambiguity |
| Owner chat: `eh` | 1 planner | Same candidates and original expiry; no product execution |
| Owner chat: one explicit unique offered variant | 1 planner | Correct catalogue fields from a new restricted source read |
| Owner chat: unsupported stock request | 1 planner | Unknown stock, no fabricated quantity or inventory operation |
| Resolver / synthesis | **0 / 0** | Deterministic catalogue response path; block unexpected generation before dispatch |
| Terminal proposal/chat replays and final accounting | 0 | No calls, charges, source queries or mutations; zero final hold |
| **Total** | **5** | **23 + 5 = 28 maximum** |

Use a new isolated fictional application database/business with complete confirmed
onboarding, the unchanged source reader, normal 20,000-token daily allowance and
isolated Redis/queues with no generation workers. Do not preseed approvals or
clarification state, reset allowances, modify the historical source or run
background evaluations. Bind private credentials through the verified container
path. Recheck frozen code, ledger, source READ_ONLY/grants and committed budget
before dispatch. Recompute the current payload estimate and allowance for each
stage; commit admission/any resize before its external request. Capture reported
usage or unknown status, conservative reconciliation and final hold.

Proposal exposure is bounded operator-approved metadata only: reverify four
objects, twelve columns and zero row/credential exposure (previous canonical
metadata was 1,850 UTF-8 bytes). The changed instruction/schema means the previous
token estimate is not a fresh estimate. Chat planners necessarily receive bounded
owner text/history and offered catalogue labels/identifiers; exclude contacts,
private columns and credentials. Independently review the live proposal before
approval/activation. If evidence cannot support its semantics, stop for review.
Keep clarification follow-ups inside the original 15-minute expiry.

Before every external dispatch, append its stage and committed admission to the
existing ledger; never reset or overwrite its historical entries. Every proposal,
planner, resolver, synthesis or unexpected background generation counts, as does
every failed HTTP/timeout/truncation/invalid-response attempt. Enforce both a
five-new-attempt guard and cumulative 28 guard with **no retries** and stop on the
**first failure**. An admission rejection with zero HTTP dispatch is recorded
separately, never presented as a successful attempt. If an unexpected stage is
needed, stop before dispatch and seek a revised bounded authorization. No hidden
calls can consume uncounted capacity.

The plan does not spend calls on further adversarial stale/cross-scope variants;
those backend checks are already tested with real source reads and mocked
generation. Live success requires the five planned stages and independent mapping
review to pass, zero-cost replays/accounting evidence to match, and source/grants
to remain unchanged. Mock results do not satisfy those conditions.

## Remaining boundaries and completion workflow

Live proposal and planner behavior remain unverified. Literal Arabic search in
the historical catalogue has no verified match; translations, Arabizi and inferred
aliases remain unsupported. Deterministic catalogue replies currently use English;
multilingual catalogue reply quality was not established. Blank/generic labels
remain possible. Catalogue-only mapping supports the existing bounded base-table
keys/joins, not arbitrary schemas, views, transformations or stock/pricing/sales.
No whole-workflow latency SLA is claimed.

Scoped tested code is committed on the feature branch. Your explicit no-paid-call
instruction requires separate approval of the plan above. After that validation
is authorized and passes: review the final frozen diff, merge into local main and
push main, preserving unrelated work and comparing remote history first. Until
then, main stays unchanged. No deployment or draft PR is part of this task.
