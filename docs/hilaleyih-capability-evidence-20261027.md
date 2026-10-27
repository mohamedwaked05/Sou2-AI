# Hilaleyih capability evidence and verification

Inspection date: 2026-10-27. The restored SQL Server database remains READ_ONLY.
Existing source permissions, deployment allowlists and stored mappings were not
expanded. No source procedures were executed. Private records, credentials,
exclusion identifiers and provider responses remain outside source control.

## Catalogue renewal

An ACTIVE connection can renew Catalogue approval only for its current,
previously approved mapping with a prior Catalogue approval. A review revision,
changed mapping or superseded revision cannot use this path. New or changed
mappings retain the inactive-source restriction.

Renewal requires a fresh full-scope report, explicit owner confirmation and
matching counts, mapping, source/schema fingerprints and policy digest. Approval
rechecks the data and relationship digests inside a consistent source transaction
held through the platform commit. It appends an approval event, preserves ACTIVE
and leaves independent Sales approvals unchanged. Replaying an expired report
returns a controlled revalidation error. The UI requires validation after the
previous approval and clears its confirmation when the report changes.

Actual mapping v5 validation returned 9,353 total records, 9,343 valid identities
and names, and exactly 10 proven-safe exclusions: approximately 0.106918% invalid.
These are source product identities; this does not establish current sellability,
prices, stock or a new product-category meaning. Approval is an owner action;
validation alone never renews it.

## Historical document counts

Fresh scoped SQL confirms 17,312 SalesHeader documents with CompCode `hila23`,
Type `SI` and Status `Paid`. The complete enforced identity is
Type/InvNumber/IDBranch/CompCode. The February 1 inclusive through February 17
exclusive, 2023 source-local interval contains 5,815 documents. The approved
interpretation of SalesHeader.Date is Asia/Beirut, as previously confirmed by
the owner; the backup timestamp alone does not establish that timezone.

Counts include negative documents and one header without lines. They are not
ordinary retail-sale counts, product quantities, rankings, revenue, refunds or
current-day sales. The source covers January 2 through February 16, 2023; an
empty later interval describes the historical backup, not current operations.

## Pricing: evidence does not support activation

Fresh diagnostics reproduce 218 invalid records: one missing identity and 217
missing units. No Selling1 amount is NULL or negative; that does not repair the
identity or unit defects. The alternative ItemMultiPrice A/B/C lists contain
almost entirely zero amounts and do not establish checkout precedence.

Required evidence remains:

- The authoritative selling tier/list and precedence between Selling1 and
  ItemMultiPrice, including the meaning of zero amounts.
- The amount/currency pairing, per-unit or per-pack basis, branch/customer rules,
  tax/discount treatment and effective date.
- Authoritative source corrections or mappings for the missing identity and 217
  units. Alternate box barcodes with different pack quantities and prices are not
  proven replacements for missing base units.

Currency and tax dictionaries provide useful labels, but do not by themselves
establish these price rules. Historical backup data must not be presented as a
current checkout price. Invalid price records were not silently excluded.

## Inventory: alternative sources remain unfit

The refreshed metadata survey covers all 548 tables, 13 modules and four declared
relationships. Relevant alternatives and earlier full-scope integrity evidence
were inspected, including ItemsQty, StockInventory, ItemsInventory, Table_1,
batch records and the movement ledger.

- ItemsQty has 63 rows, including 54 unmatched product references and 52 unmatched
  warehouse references, with overlap. Its columns provide no balance timestamp,
  unit or reservation definition.
- StockInventory has 1,755 rows, with 1,375 unmatched products, 1,755 unmatched
  warehouses and 205 missing units. Its company/location grain and duplicate
  groups do not establish authoritative Hilaleyih balances.
- ItemsInventory has unexplained quantity buckets; import and batch alternatives
  do not provide a complete, dated, correctly joined balance.
- The 55,587 movement records are SI movements with no inbound events in the
  inspected ledger. There is no verified opening-plus-complete-movements basis.

Missing evidence is an authoritative balance/report, complete product and location
identity, quantity/pack unit, company/warehouse scope, actual balance timestamp
and reservation/available-quantity rules. A backup date does not establish when a
stored balance was calculated. Missing rows are unknown stock, never zero stock.

## Revenue: arithmetic is insufficient for a financial approval

All 17,312 headers have stored gross, discount, tax and payment fields. Headers
with lines reconcile to quantity times price, but one header has no lines.
There are 190 negative-gross Paid SI documents; all lack an original-document
reference. There are also 81 documents whose paid-plus-rest amount differs from
gross minus discount plus tax. Seven differences exceed one source currency unit.

No verified evidence classifies all negative documents as refunds, credits or
adjustments, or establishes complete refund linkage and recognition rules.
The settlement/rounding differences also require explanation. Required business
definitions include gross versus net, completed/excluded states, refund timing
and linkage, currency and tax/discount treatment. Raw signed invoice arithmetic
is not an approved net-revenue definition. No invalid financial events were
discarded to obtain an approval.

## Verification and remaining limitations

- Corrected two additional runtime regressions found by the complete suite:
  explicit location-preference commands bypass the business-address knowledge
  fast path, and a validated Catalogue cancellation takes precedence over
  changed-product wording. Invalidated saved locations now receive an accurate
  message. Ordinary location, delivery, returns and Friday-hours policy routing
  retains regression coverage.
- 99 frontend tests, TypeScript, lint, production build and isolated Chrome
  navigation checks at three viewport widths passed.
- 246 core backend checks and 18 updated operational-contract checks passed.
- 88 native Catalogue/capability checks passed, including ACTIVE renewal,
  fresh-confirmation enforcement and preservation of an independent Sales event.
  The expired-report replay check also passed after the final error normalization.
- Disposable worker/Redis validation passed 24 runtime checks, 52 source checks
  and 3,095 offline checks. An additional offline run included the corrected
  legacy mapping-profile tests: 3,100 passed.
- Revision-18 clone/forward-upgrade verification and the existing operational
  approval regressions passed on disposable resources. Live migrations did not run.
- Three normal live Owner Chat turns in English, Arabic and Arabizi returned
  matching Friday time values and resolvable citations with filenames/passages.
  Idempotent replay returned the same assistant message. The two existing
  knowledge documents remain READY with 50 Gemini embedding chunks.
- Pricing, Inventory and Revenue refusal checks used zero charged tokens. No
  live 500-chunk capacity attempt was repeated; bounded processing and retry
  behavior were verified with isolated providers.
- The final complete legacy backend run passed: **1,199 passed, two skipped,
  zero failures**. The two opt-in tests require private historical-source and
  isolated SQL Server fixture credentials; actual Hilaleyih validation was
  performed separately through its restricted source configuration.
- Legacy tests now exercise the current deterministic rendering and exact token
  accounting, bounded completed-owner history, evidenced pending transitions,
  real intervening exchanges and independent reporting/deadline clocks. Invalid
  or foreign selections must fail closed without source reads. Cancellation
  tests first establish a real active question through the normal conversation
  API. No blanket exclusions or weaker production guards were introduced.

Catalogue and Sales approval renewal still requires the exact owner confirmation
requested during this run. Fresh validation is not approval, and expired reports
must be replaced and their evidence compared before any normal approval request.
Pricing, Inventory and Revenue remain unavailable for the evidence reasons above.
These outstanding approvals and source definitions prevent certifying the entire
product as complete.

The connected authenticated Chrome origin was previously denied by saved browser
permissions. API and isolated-browser evidence do not replace an authenticated
UI acceptance run. Earlier browser acceptance results were left intact.

To reproduce the isolated legacy suite from the repository root:

```powershell
.\backend\.venv\Scripts\python.exe backend/tests/run_legacy_validation.py
```

This runner pins existing images, creates private PostgreSQL/Redis test services
without published ports or an external network, seeds synthetic data, writes
ignored local evidence and removes only its identity-verified disposable
containers. Historical reversible migrations use fresh owned test databases;
revision 19's permanent-evidence downgrade prohibition remains unchanged.
