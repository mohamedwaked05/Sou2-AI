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

Inventory uses stable DEMO quantities because the inspected source balances do
not establish reliable current inventory. SHA-256 over the product identity and
canonical test location produces the quantity. Values survive application
restarts without writing source balances. Default and Jbeil are explicitly
simulated test-location labels, not verified branch mappings. Unknown locations
require clarification.

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

Validation includes the seven example questions through the HTTP Owner Chat
route with synthetic source fixtures and an unavailable provider, plus ambiguous
variants, missing products, branch clarification, authentication and replay.
These automated checks do not substitute for authenticated live-store
acceptance or owner approval renewal.
