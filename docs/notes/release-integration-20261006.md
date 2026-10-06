# Combined release integration

This release preserves the predecessor customer-channel, operational integration,
owner-chat, and frontend changes from the 20 commits above old main
`39619a07d56e281a9da5abb9b9fd14666e6a42c9`, plus the Arabic repair `def42a9`
and the customer-accounting/recovery repair described here. No history is squashed
or rewritten. Raw evidence, helpers, source data, and database backups stay local.

## Repairs and review

Arabic regex alternatives and the missing-information reply were recovered from
the corrupted UTF-8 literals without adding keyword rules. Customer output
rejection had discarded authoritative usage: the offline probe reported 110 tokens
but charged zero. Returned usage and typed provider failure usage now reconcile
as consumed. Post-dispatch unknown usage conservatively charges the held estimate;
explicit pre-use rejection retains the release path. Terminal replay is unchanged.

Expired customer recovery previously queried a protected accounting table and
failed under restricted runtime credentials. New revision `20261006_15` replaces
that lookup with a controlled SECURITY DEFINER function. It validates an expired
inbound PROCESSING claim, matches its business/customer reservation, and delegates
idempotent unknown-usage reconciliation to the existing budget function. The
runtime still cannot select accounting tables; no migration privileges are granted.
Message recovery and reconciliation commit together. Sending claims retain the
delivery-uncertain behavior and are not blindly resent.

Review covered planner/dispatch contracts, source-backed pending transitions and
expiry, user/business/source validation, budget admission and resize, usage/failure
reconciliation, idempotency, customer identity envelopes/profiles/webhook gates,
operational adapter filters/resolution, frontend pagination/polling, and the ordered
migration chain. The two documented customer integration defects are resolved.
This is a scoped code review and offline validation, not universal correctness.

## Validation

The final combined backend selection is the 13 modules recorded in the previous
integration report: customer channels/coverage, operational contracts/PostgreSQL/
tools, owner provider/reliability/chat/routing, AI usage, API rate limits, data
sources, and health. Final local JUnit is
`backend/.tmp/release-final-20261006.xml`; retain it locally, not in Git.
The final combined run passed **653 tests** in 139.10 seconds, with one upstream
Starlette/httpx deprecation warning. Earlier intermediate runs are not used as
validation of changed application bytes. Test-file formatting normalized line
endings only; the final post-merge smoke checks also execute committed test bytes.

Focused cases prove readable Arabic fallback without generation/reservation,
known/unknown consumption on rejected output and provider failures, replay without
duplicate charges, interruption recovery under restricted credentials, rejection
of unexpired claims, and repeat recovery with zero outstanding hold.
Ruff lint and format checks cover app/tests/Alembic. Frontend TypeScript/Vite build
and 40 frontend tests passed. No paid calls were made; the historical Gemini count
remains 22/25. The eleven frozen owner-chat file hashes still match the saved
manifest. Customer repair bytes have their own fresh tests.

GitHub read-only checks found no active repository hooks, Actions workflows,
main branch protections/applicable rules, or deployment records. No tracked
deployment configuration was found. These findings support a normal main push
without a repository-managed automatic deployment; independently configured
external services are outside the available evidence. No deployment was requested.

## Migration and later production startup

Development advanced from verified revision 14 to `20261006_15` through the existing
migrator process. Revision 15 creates only a function; it contains no table/data
changes. SQL confirms migrator ownership, SECURITY DEFINER, fixed
`search_path=pg_catalog`, and execution ACLs only for migrator/runtime on revisions
14 and 15. Runtime direct accounting-table SELECT remains denied.

Development backup retained locally at
`data/generated/release-integration-20261006/sou2ai_dev_before_20261006_15.dump`;
SHA-256 `5ffa8b0cae11d54b034733e97ef8d5d3c6fbcd9946a9b6ecb2088c5211300518`.
It is a custom-format archive with a readable 408-entry table of contents.
This task did not perform a full restore test of that new archive.

Production is untouched. For a separately authorized release:

1. Confirm the target server/database and current revision through SQL. Back up
   and restore-test existing data. Drain API generation and workers while
   preserving queued jobs, leases, usage, and allowances. Validate existing
   customer relationships before revision 09 adds scope constraints; stop on
   invalid records rather than deleting them.
2. Privately inject existing migrator credentials outside runtime environments.
   From `backend/`, run `alembic current`, then `alembic upgrade 20261006_15`, then
   `alembic current` using `.\.venv\Scripts\alembic.exe`. Ordered upgrades from
   revision 08 are 09 → 10 → 11 → 12 → 13 → 14 → 15. Inspect unexpected revisions.
   Allow time for validating constraints and locks. Do not use a blanket downgrade
   as rollback: earlier downgrades drop preference data.
3. Verify revision, existing data, function owners/search paths/ACLs and denied
   runtime accounting-table access. Keep development allowance overrides unset.
   Start the updated backend and workers only after migration, with restricted
   runtime credentials. Preserve customer identity encryption/HMAC keys. Keep
   migrator/operator credentials out of application and worker environments.
4. Review production HTTPS, trusted hosts/CORS, secure cookies, private storage,
   provider approval, channel profile/remote validation configuration, and queues.
   Startup does not apply migrations or probe providers. Validate health,
   authorization, queues and controlled smoke scenarios before reopening traffic.

Translation/paraphrase/ordinal/alias limitations, probabilistic Gemini
classification, historical unknown usage, and unverified live multi-call resizing
remain explicit. Smaller reservations do not prove lower actual model consumption.
Production provider/channel approval, load testing and deployment remain separate.
The supermarket setup is deferred.
