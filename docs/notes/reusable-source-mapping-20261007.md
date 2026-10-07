# Reusable catalogue mapping: offline handoff

Offline implementation is validated. Real Gemini proposal quality and full live
onboarding remain pending approval. No paid generation, embeddings, production
change, source activation in development, or source-database mutation occurred.
The code is saved on `feat/reusable-product-mappings`; main integration/push is
held until the required live proposal check is resolved.

## Application behavior

Authenticated Data Sources routes and UI support approved metadata discovery,
idempotent proposal requests, structured review, explicit semantic confirmation,
validation, activation and catalogue search. Engine connectors support PostgreSQL
and Microsoft SQL Server. The reusable compiler accepts validated structures,
quotes identifiers and binds values; neither the model nor onboarding accepts
executable SQL. Product keys remain strings, including composite keys and leading
zeros. Both mapped name columns, SKU and related alternate identifiers can be
searched. Category joins cover full unique keys. Ambiguity offers choices; explicit
selection includes the offered mapping version and rejects stale selections.

Approved immutable revisions retain source/schema provenance and approval identity.
Compact metadata/permission checks gate reads on incompatible drift; new mappings
are not discovered or generated per message. Catalogue-only sources expose
`products` and return stock as unknown. Inventory, prices, sales, revenue, refunds
and restocking are disabled. Existing PostgreSQL demo capabilities remain supported.
Only one source can be active per business across engines.

To onboard another supported supermarket without application code: an operator
configures a restricted read-only TLS connection, explicit business binding and
approved catalogue object/column exposure policy in private
`SOURCE_CONNECTIONS_JSON`. The owner chooses its safe profile key, discovers
metadata, requests a proposal, reviews uncertainties/structured fields, confirms
semantics, validates and approves, then activates. Unsupported semantics remain
disabled. Connections are deployment-managed; this phase adds no credential-entry
UI. Default `SOURCE_MAPPING_PROVIDER=mock` explains uncertainty without a paid call.
Gemini is the sole optional proposal provider; the contract/compiler do not depend
on it and no OpenAI production switch is implemented.

## Fresh validation

- Combined backend selection from the release report plus mapping/ACL tests:
  **672 passed, 1 SQL Server opt-in skipped**, 138.34 seconds. JUnit retained at
  `backend/.tmp/mapping-system-20261006/combined.xml`.
- Separate sequential Linux/ODBC run: **21 passed**, 11.81 seconds, against actual
  PostgreSQL, a fictional SQL Server schema and the existing restricted Hila reader.
  Private log: `data/generated/mapping-system-20261007/offline-validation.log`.
- Frontend: **43 passed**; TypeScript, ESLint and Vite build pass. Repository-wide
  Prettier still reports unchanged `frontend/README.md` and `frontend/src/workspace.tsx`.
  Changed frontend files are formatted. Backend Ruff lint and format pass across
  app/tests/Alembic. An upstream Starlette/httpx deprecation warning remains.
- Tests cover opaque differently named schemas on both engines, composite keys and
  relationships, malformed mappings/SQL rejection, permission rejection, business
  isolation, missing capabilities, schema drift, ambiguity, bounds and stale
  selection. Concurrent replay proves one dispatch/hold; terminal replay adds no
  call or charge. A committed 2,348-token fixture hold reconciles to reported mock
  usage 200 with zero outstanding hold. Unknown usage charges the hold once;
  admission rejects an over-budget request before dispatch.
- The restored READ_ONLY `sou2ai_hila23_test` flow uses an explicitly mocked Gemini
  transport/proposal fixture, not a custom Hila adapter or live classification.
  Application validation accepts source-backed identifiers, both name columns,
  categories including the composite relationship and barcode search structure.
  English Pepsi yields 13 variants; explicit selection returns the offered source
  details. An actual secondary-column value is searchable. Stock stays null.
  Synthetic reported usage 280 reconciles with zero hold; proposal replay makes
  no second call. No records were sent to any external provider.

An overlapping database test run was discarded because both runners shared test
cleanup. Only the subsequent sequential results above count. The exact tested
file hashes and evidence hashes are in
`reusable-source-mapping-20261007-validation.json`; no earlier 417/653 result is
attributed to changed bytes.

## Migration and startup

Alembic has one head, `20261007_16`, following `20261006_15`. Actual SQL confirms
`sou2ai_test` is at 16 and `sou2ai_dev` remains at 15. Revision 16 was applied and
guardedly revalidated only on the test database; no development/production data,
usage or allowances were reset. It adds mapping revisions, immutable-state guards,
an atomic admission wrapper and broader source constraints. The active-source
index becomes unique per business, preserving existing demo data. The admission
function is owned by `sou2ai_migrator`, SECURITY DEFINER, fixed
`search_path=pg_catalog`, and executable by runtime only through controlled ACLs.
Runtime accounting-table SELECT, revision DELETE and provenance UPDATE remain
denied. Downgrade refuses while mapped configuration/revisions exist.

For a separately authorized target rollout, confirm target/revision, back up and
restore-test data, drain API/workers and privately inject existing migrator
credentials. Inspect any intervening revisions or invalid records rather than
deleting them. From `backend/` in PowerShell:

```powershell
.\.venv\Scripts\alembic.exe current
.\.venv\Scripts\alembic.exe upgrade 20261007_16
.\.venv\Scripts\alembic.exe current
```

Upgrade through the existing ordered migrations (including 14 and 15); verify
data, owners/search paths/ACLs before starting updated services with restricted
runtime credentials. Never place migrator credentials in application environments.
Install the declared dependencies with `python -m pip install -e '.[dev]'` in the
existing environment. SQL Server additionally requires Microsoft's ODBC Driver 18.
The backend image builds with that driver and Python 3.14 unchanged. Native
Windows driver installation is blocked by administrator privileges on this host;
Linux container validation passed. Do not change Windows privileges to bypass it.
The Windows SQLAlchemy copy had unavailable OneDrive files; validation used a
private intact copy of the same installed version, 2.0.51, without dependency
version changes. See [Microsoft ODBC installation](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server).

The Hila instance remains available at `127.0.0.1:14333`, database
`sou2ai_hila23_test`, with existing restricted reader and grants unchanged.
Credentials remain DPAPI-protected under the established private mechanism.
Do not restore again or execute imported procedures/jobs. Start/stop instructions
remain in the local restoration handoff. Raw records, credentials, backups,
fixtures and traces are ignored locally and excluded from the commit.
The manifest-owned fictional SQL fixture database/login were removed after the
final run; private cleanup evidence is retained. Hila and all Docker volumes remain.

## Exact live Gemini plan for approval

Authorize **at most one additional generation attempt**: a single authenticated
mapping-proposal request for the existing Hila source in a new isolated fictional
application business/database, using the restricted reader and normal 20,000-token
allowance. No pending/mapping state is preseeded. Use the production proposal
implementation/provider infrastructure and append the attempt to the historical
record before dispatch. No planner, resolver, synthesis, background or embedding
call is required; no evaluation jobs enter shared worker queues. Failed calls also
count. No automatic retry or increase of the cap is allowed.

Frozen preflight: four approved catalogue objects, twelve columns, names/types,
nullability, unique keys and available relationships. **1,850 UTF-8 metadata bytes;
zero product/customer/employee/patient/account/contact records, zero credentials.**
The request also includes the fixed system instruction and response JSON schema.
Model: existing `gemini-3-flash-preview`; output ceiling 2,048. Current serialized
input estimate 1,893 gives a committed initial reservation of **3,941**. Recompute
the estimate and remaining allowance before dispatch; stop if admission fails or
the exposure differs. Never reset allowances or bypass admission.

Validate the returned structure through the application and independently review
it against the inspection evidence. Require explicit semantic confirmation before
activation; stop for review if the proposal is incomplete or uncertain beyond that
evidence. Do not substitute the mock fixture as a live success. After approval,
perform English Pepsi/secondary-name search, explicit variant selection and
proposal/approval replay. Those steps need **zero** additional model calls.
Capture the committed reservation, provider usage (or unknown status), conservative
reconciliation, remaining allowance, persisted approval/version and zero final hold.
Retain redacted evidence and clean up only the new task-owned application resources.

Saved records remain **22/25 cumulative attempts** (historical 19 preserved plus
attempts 20–22). This plan reaches at most **23/25**, subject to verifying no new
external attempts before starting. The two-consecutive-provider-failure stop rule
remains; this one-attempt plan stops on its first failure. No paid call has occurred
in this implementation task.

## Remaining limits and integration status

Backend validation and safe execution do not prove Gemini semantic classification.
Literal Arabic `بيبسي` has no match in this historical source; English lookup and
the secondary-name-column check do not establish translation/Arabic/Arabizi aliases.
No aliases are synthesized. Empty/generic category labels remain possible. Only
base tables, enforced nonnullable text/integer product keys and the specified
bounded joins are supported; views, custom transformations and other semantic
capabilities require a separate phase. Statement deadlines do not establish a
whole-workflow latency SLA. Historical data is permanent and is not refreshed.

Catalogue routes/UI and the controlled catalogue tool work offline; owner-chat
planner dispatch for this new operation is not added. Existing continuity/replay
safeguards are covered by the combined regressions; live multi-call reservation
resizing remains unverified. Smaller holds do not prove reduced actual consumption.
This is ready for review of the offline scope, not full live onboarding or production
readiness. No deployment or production modification occurred. Remote main was
fetched and remains the implementation base. GitHub checks found no Actions,
active hooks, main protections/rules or deployment records; independently configured
external automation is outside this evidence. Merge/push waits for the remaining
live validation and final review.
