# Customer Arabic repair and remaining integration blocker

The predecessor `d41ad62` contained two mojibake literals in
`backend/app/worker/customer_messages.py`: the Arabic alternatives of the business
question regex and the Arabic missing-information reply. Reversing the existing
UTF-8/Windows-1252 misinterpretation recovered the exact original wording:
`توصيل|السعر|قديش|بكرا|العنوان` and
`عذراً، هيدا المعلومة مش متوفرة حالياً.` No keywords or routing behavior were
added. The source and regression file are UTF-8. Adjacent handoff, private-data,
injection guards and static replies were inspected and are intact.

Ten added regressions cover all five restored alternatives, the four Arabic
static reply variants, and an authenticated customer delivery request with no
delivery evidence. That request persists the intended fallback, completes the
inbound message, and creates neither a generation call nor a reservation.
Seven new cases failed before the repair; all ten pass after it.

Fresh combined checks on the repair:

- The backend selection recorded in `main-integration-20261005.md`: **648 passed**
  in 131.36 seconds; one upstream Starlette/httpx deprecation warning. Local JUnit:
  `backend/.tmp/main-integration-repair-20261005.xml` (not tracked).
- Ruff lint passed; format check passed for all 151 files in app/tests/Alembic.
- Frontend `npm run build`: TypeScript and Vite passed. `npm test -- --run`:
  **40 passed**. Initial sandbox process-launch failures were resolved by rerunning
  with approved execution permissions; no application repair was needed.
- Alembic has one head, `20261005_14`. Actual read-only development SQL confirms
  that revision, migrator ownership, SECURITY DEFINER, `search_path=pg_catalog`,
  and execution ACLs only for the migrator and runtime roles. No production
  migrations or data changes were performed. The deferred migration/startup
  procedure in the previous report remains applicable.

## Additional material blocker: lost customer usage after output rejection

The remaining predecessor review found a separate accounting defect. After
`resolved_provider.generate(request)` returns, `_validate_customer_result` may
raise `ValueError` for invalid citations, proposed knowledge, or an empty reply.
The worker's `except ApplicationError, ValueError` branch calls
`reconcile_ai_usage(..., usage=None, outcome="release")`, discarding already
reported consumption. This is a post-generation failure, not a pre-use rejection.

A task-owned offline probe used the authenticated webhook, active fictional
customer channel, test database, mocked generation and disabled queue dispatch.
The provider returned an empty reply with authoritative input/output usage
100/10. Database inspection through the existing test migrator fixture showed:

- Provider calls: **1**; reported consumption: **110**.
- Inbound message: `FAILED`, `customer.generation_failed`.
- Reservation: `released`; daily tokens used: **0**; outstanding hold: **0**.

The diagnostic reproduction passed its assertions of this defective behavior;
it is not a correctness pass. Probe source is retained only in
`backend/.tmp/customer-accounting-probe-20261005.py`, excluded from the commit.
No paid calls or shared worker jobs were created. The smallest proposed next fix
is to preserve returned usage on output-validation failure and reconcile it once
as consumed, retaining a distinct pre-dispatch release path and conservative
unknown-usage handling. Add a regression for rejected output after authoritative
generation. Lease-expiry/unknown consumption should also be reviewed before
asserting complete customer accounting correctness. No accounting repair cycle
was started under this task.

The Arabic repair can be committed independently, but main integration remains
blocked. No merge commit, main push, post-merge main smoke run, deployment, or
supermarket setup occurred. PR #1 remains draft. Existing owner-chat translation/
alias limitations, historical unknown usage, probabilistic classification, and
unverified live multi-call resizing remain explicit. The full release review is
not complete while this blocker remains.
