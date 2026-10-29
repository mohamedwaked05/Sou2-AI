# Hilaleyih local test mode

`HILALEYIH_TEST_MODE=true` enables the isolated Owner Chat test path only when
`ENVIRONMENT=development`, for the configured Hilaleyih business and source IDs.
The historical reader additionally requires the `sou2ai_hila23_test` database
and the existing tenant-restricted `hila_catalogue_dev` connection. The switch
defaults to false. Production and other businesses retain their existing paths.

Catalogue searches, identities, exclusions and counts still use the approved
catalogue tool. Sales counts still use the approved historical Sales tool.
Expired approvals must be renewed through fresh validation and the normal
authenticated owner confirmation/approval endpoints. Test mode does not create
approval events or grant permissions.

Pricing returns the product's historical `Selling1` source amount, unit and
currency code, with explicit unverified semantics. Missing or unusable amounts
receive a deterministic DEMO estimate in synthetic currency units. These are
never represented as current checkout prices. Product resolution works through
the valid catalogue, requires clarification for ambiguous variants and refuses
unknown products.

Hilaleyih Market is one store, with no branches. Requests for Jbeil or another
branch receive that explanation without any branch-specific quantity. There
are no default/Jbeil location namespaces or generated stock quantities.

Inventory first prefers a readable StockInventory balance with exact product,
company `hila23`, matching source unit and unique internal warehouse keys. It is
reported as an undated historical source balance, never verified current stock.
ItemsQty lacks company/unit/date evidence; ItemsInventory's unexplained buckets
cannot establish on-hand stock. In the inspected source, StockInventory belongs
to company `DB`, not the approved Hilaleyih company; Pepsi 1.25 L has no rows in
any of those three balance tables. Runtime balance-table access is also denied.

When no reliable matched balance is available, the DEMO proxy uses positive
product-specific Paid SI SalesFooter quantities, including free quantities only
when their units match. It joins the complete SalesHeader document identity,
requires an explicit product base unit and pack factor 1, and never guesses pack
conversions. Negative documents/lines are counted and excluded without claiming
they are verified refunds. Incomplete/unknown-unit lines and orphan references
are disclosed. No usable product-specific demand means no numerical estimate;
missing evidence is never zero stock.

The explicit replenishment assumption is one restock to seven days of average
observed demand, with no safety stock, rounded up to a whole source unit:
`ceil(positive base-unit demand / source calendar days * 7)`. Calendar days span
the entire source's first through last Paid SI dates, inclusive, rather than
only the days that a particular product sold. This is a low-confidence modeled
replenishment target, not actual current stock. The answer gives the period,
record counts, source record-key samples, exclusions, method and confidence.
Nothing random, hashed or product-hardcoded determines inventory quantities.

Internal `IDBranch` remains part of the source's composite document identity for
correct joins; it is never interpreted as a physical branch, used as a stock
filter or grouped into separate store balances. No source records are modified.

Revenue uses a fixed read-only Paid SI SalesHeader projection scoped to company
`hila23`, an explicit historical month, and source currency code. It reports
gross minus stored discount amount plus stored tax amount, preserving signed
negative documents and incomplete-record counts. It does not classify negative
documents as verified refunds or combine currencies. Settlement discrepancies
are calculated only when the source reader can access payment and remaining
amounts; denied access is disclosed as unassessed reconciliation, never agreement.
The output is a provisional historical calculation, not approved revenue.

The financial projection is a separate fixed policy under the opt-in test path;
it does not change the catalogue connection fingerprint, approved mappings or
global quality rules. SQL Server permissions remain authoritative. No source
write, permission grant, migration, authentication bypass or approval mutation
exists in this path. Reads have source timeouts and privacy-minimal tool audits.

The deterministic path executes within the ordinary authenticated Owner Chat
turn, retaining authorization, idempotency, persisted conversation context and
assistant messages. It does not call Gemini or consume provider tokens. Product
context expires after 30 minutes, remains scoped to the owner and conversation,
and is checked against the current catalogue mapping and source fingerprints.

Product selection prefers a unique exact source name or identifier, including
normalized whitespace/size spelling and the optional word "Bottle". It never
invents catalogue entries. Offered names, numbered choices and valid identifiers
are revalidated through the authorized catalogue search. Narrowed choices and
the selected product remain in scoped conversation context across follow-ups.
Barcode-only and "product barcode ..." requests use the catalogue identifier
search, including its approved alternate-barcode mappings.

Validation includes the seven example questions through the HTTP Owner Chat
route with synthetic source fixtures and an unavailable provider, plus exact
non-Diet selection, repeated names, narrowed variants, barcodes, missing products,
nonexistent branches, missing quantity evidence, authentication and replay.
These automated checks do not substitute for authenticated live-store
acceptance or owner approval renewal.
