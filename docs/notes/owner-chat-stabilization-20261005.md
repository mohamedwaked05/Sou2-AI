# Owner-chat stabilization handoff

Reviewed against `d8dd8b921bb24f248d43112b4203844d79876797`. No material defect was
identified in the complete working patch. Existing predecessor commits are outside
this focused commit; the draft PR targets `fix/operational-query-correctness`.

## Resulting behavior

- Planner wire contracts require relevant tool arguments, bounded sales periods,
  and explicit pending-reply classifications. Delegated conversation/knowledge
  decisions do not require an unused reply. Best-seller intent dispatches through
  the registered tool. Compact schemas retain validation constraints; the supported
  Flash Lite planner/category modes use their reviewed limited-thinking policy.
- Product, category and location choices retain source-backed filters. Ambiguous
  inventory locations cannot be narrowed by an invented model-expanded label.
  Inventory presentation uses source quantities and discloses partial row coverage
  and missing pack/case units.
- Pending preferences survive unresolved acknowledgements and recoverable failed
  turns without renewing expiry. Explicit unique source-backed selection completes
  them. Cancellation/replacement/unrelated transitions require current-message
  provenance and consistent action fields. Expired, changed-source and missing
  candidate state is invalidated. Cancellation does not clear an existing default.
- Operational turns reserve the complete imminent planner payload. Each subsequent
  call resizes the same hold to prior consumption plus the next serialized estimate
  and output cap. Admission failures prevent dispatch; earlier usage still charges.
  Known usage, unknown estimates and pre-use rejection remain distinguishable.
  Replays do not generate, charge or mutate again. Smaller holds are not evidence
  of reduced actual Gemini consumption.

## Review and migration

Reviewed the full application and test diff, including registry-derived dispatch,
provider validation, source/business/user/conversation scoping, pending recovery,
idempotency, generation claims, failure reconciliation and concurrency coverage.
Migration `20261005_14`, directly after `20260930_13`, uses the shared business
allowance lock followed by reservation/daily locks, includes competing holds,
checks the active attempt/token and unexpired claim/reservation/day, and does not
renew leases. The function is migrator-owned, `SECURITY DEFINER`, uses qualified
tables and `search_path=pg_catalog`, revokes PUBLIC execution and grants runtime
execution. Runtime receives no migration privileges. Upgrade adds the function/ACLs
without resetting data or allowances; downgrade drops only that function.

Apply through **20261005_14 before starting this backend version** using the existing
Alembic migrator process. It is verified on development and the isolated evaluation
database; production was not changed. The development backup passed a full restore
and integrity check and remains local. No backup or credentials are in this commit.

## Validation provenance

All eleven frozen paths match the final validation manifest. The curated companion
[validation manifest](owner-chat-stabilization-20261005-validation.json) records exact
working-file hashes, evidence digests, regression counts and safe live summaries.
Normal Git newline conversion is separately verified against staged content; no
application or test text was changed during review.

- **417 regressions passed**, zero failures/errors/skips: provider 156, operational
  reliability 129, tools 82, accounting 50. The frozen run took 92.998 seconds and
  covers transitions/recovery, isolation, known/unknown usage, replay, admission
  rejection at each stage, actual serialized estimates, concurrent owner/customer
  holds, resize idempotency, expiry and ACLs. These are saved results for matched
  bytes; the full suite was not rerun merely to commit them.
- Fresh review checks: Ruff lint and format on all ten Python paths pass;
  `git diff --check` passes. Existing warning: Starlette/httpx deprecation.
- Final real-Gemini sequence: fresh fictional business, normal 20,000 allowance,
  authenticated routes, restricted roles, two source locations, no preseeded pending
  state, isolated worker-free queue. Ambiguity offered both choices; `eh` preserved
  choices/expiry with no saved preference; explicit warehouse selection saved the
  correct source-backed warehouse and completed the pending row. Selection replay
  added **zero calls, charges or mutations**.
- Three new planner attempts, cumulative **22/25**, including all historical
  failed/resolver/synthesis/delegation attempts. Historical 19 records were preserved.
  No new provider failures or unknown usage. New authoritative usage/charge:
  **4,286 input + 128 output = 4,414**; remaining 15,586; outstanding holds **0**.
  Every dispatch observed committed admission and each reservation reconciled once.
  No background generation, local model or embedding calls occurred.

| Final live stage | Reservation | Actual consumption |
|---|---:|---:|
| Ambiguous preference |5,089|1,323|
| `eh` |6,311|1,534|
| Explicit warehouse |6,302|1,557|
| Same-key successful replay |No new hold|0 new|

## Remaining limitations

No second call occurred in the final live sequence, so **live multi-call resizing
remains unverified**; its atomicity/failure/ACL coverage is offline. Translation,
paraphrase, ordinal and approved-alias selections can still fail exact selection
guards. Gemini classified `eh` correctly in the last run, but a historical run
misclassified it as a branch selection; the backend refused that unsupported
choice. This establishes safeguards and the stated sequence, not universal model
classification, multilingual correctness, complete owner-chat validation or general
token optimization. Earlier synthesis truncation remains a provider-quality concern
with source-grounded fallback. Unexpected internal errors retain conservative
uncertain-hold accounting, not an assertion of actual provider consumption.

The scoped patches are ready for review/merge with these limits. No merge, deployment,
production modification or supermarket setup was performed. Raw traces, local
database/queue evidence, historical reports, helpers and development backup are
retained locally; only the curated non-sensitive manifest is intended for Git.
