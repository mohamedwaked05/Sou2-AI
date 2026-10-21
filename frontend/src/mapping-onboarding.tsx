import { FormEvent, useEffect, useId, useRef, useState } from "react";
import {
  api,
  ApiError,
  CatalogueResult,
  DataSource,
  MappingReview,
  ProductMapping,
  SchemaDiscovery,
} from "./api";
import { Alert } from "./ui";
import { CapabilityDashboard } from "./capability-dashboard";
import { CatalogueQuality } from "./catalogue-quality";

export function MappingOnboarding({
  businessId,
  source,
  onRefresh,
}: {
  businessId: string;
  source: DataSource;
  onRefresh: () => Promise<void>;
}) {
  const [discovery, setDiscovery] = useState<SchemaDiscovery | null>(null);
  const [review, setReview] = useState<MappingReview | null>(null);
  const [history, setHistory] = useState<MappingReview[]>([]);
  const [mappingText, setMappingText] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CatalogueResult | null>(null);
  const editorId = useId();
  const searchId = useId();
  const replayKey = useRef<string | null>(null);
  let draft: ProductMapping | null = null;
  try {
    const value = JSON.parse(mappingText) as ProductMapping;
    if (
      typeof value.object_id === "string" &&
      Array.isArray(value.name_columns) &&
      value.name_columns.every((field) => typeof field === "string") &&
      Array.isArray(value.key_columns) &&
      value.key_columns.every((field) => typeof field === "string")
    )
      draft = value;
  } catch {
    /* Partial JSON stays editable in the advanced section. */
  }
  const productObject = discovery?.objects.find(
    (item) => item.object_id === draft?.object_id,
  );
  function editDisplayFields(fields: Partial<ProductMapping>) {
    if (!draft) return;
    setMappingText(JSON.stringify({ ...draft, ...fields }, null, 2));
    setConfirmed(false);
  }

  function showReview(value: MappingReview) {
    setReview(value);
    setDiscovery(value.discovery);
    setMappingText(
      value.approved_mapping || value.proposal?.mapping
        ? JSON.stringify(
            value.approved_mapping ?? value.proposal?.mapping,
            null,
            2,
          )
        : "",
    );
    setConfirmed(false);
  }

  useEffect(() => {
    let cancelled = false;
    void api
      .mappingReviews(businessId, source.id)
      .then((items) => {
        if (!cancelled) {
          setHistory(items);
          if (items[0]) showReview(items[0]);
        }
      })
      .catch(() => {
        if (!cancelled) setError("Mapping reviews could not be loaded.");
      });
    return () => {
      cancelled = true;
    };
  }, [businessId, source.id]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "The request failed. Check the mapping and try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function propose() {
    const storageKey = `sou2ai-mapping:${businessId}:${source.id}`;
    if (review && review.status !== "proposing") {
      replayKey.current = crypto.randomUUID();
    }
    replayKey.current ??=
      sessionStorage.getItem(storageKey) ?? crypto.randomUUID();
    sessionStorage.setItem(storageKey, replayKey.current);
    const proposed = await api.proposeMapping(
      businessId,
      source.id,
      replayKey.current,
    );
    setHistory((items) => [
      proposed,
      ...items.filter((item) => item.id !== proposed.id),
    ]);
    showReview(proposed);
  }

  async function approve(event: FormEvent) {
    event.preventDefault();
    if (!review || !confirmed) return;
    await run(async () => {
      const mapping = JSON.parse(mappingText) as ProductMapping;
      const approved = await api.approveMapping(
        businessId,
        source.id,
        review.id,
        mapping,
      );
      setHistory((items) =>
        items.map((item) => (item.id === approved.id ? approved : item)),
      );
      showReview(approved);
      await onRefresh();
    });
  }

  async function search(selector: {
    query?: string;
    external_product_id?: string;
    mapping_version?: number;
  }) {
    await run(async () =>
      setResults(
        await api.searchCatalogue(businessId, source.id, {
          ...selector,
          limit: 50,
        }),
      ),
    );
  }

  return (
    <section
      className="mapping-preview mapping-workflow"
      aria-label="Catalogue mapping review"
    >
      <h3>Catalogue mapping</h3>
      <p>
        Catalogue approval covers products only. Stock is unknown until a
        separate operational mapping supports it.
      </p>
      {error && <Alert>{error}</Alert>}
      <div className="source-actions">
        <button
          className="btn-secondary"
          disabled={busy}
          onClick={() =>
            void run(async () =>
              setDiscovery(await api.discoverSource(businessId, source.id)),
            )
          }
        >
          Discover approved metadata
        </button>
        {source.status !== "ACTIVE" && (
          <button
            className="btn-secondary"
            disabled={busy}
            onClick={() => void run(propose)}
          >
            {review ? "New mapping proposal" : "Request mapping proposal"}
          </button>
        )}
      </div>
      <p>
        Proposal requests use the business AI allowance when Gemini is
        configured. Metadata only; no product records are sent.
      </p>
      {discovery && (
        <details>
          <summary>Advanced: allowed tables, columns and unique keys</summary>
          <ul>
            {discovery.objects.map((object) => (
              <li key={object.object_id}>
                <strong>
                  {object.object_id}: {object.schema_name}.{object.name}
                </strong>
                <p>
                  Columns:{" "}
                  {object.columns.map((column) => column.name).join(", ")}
                </p>
                <p>
                  Unique keys:{" "}
                  {object.unique_keys
                    .map((key) => key.join(" + "))
                    .join("; ") || "None"}
                </p>
              </li>
            ))}
          </ul>
        </details>
      )}
      {history.length > 0 && (
        <label className="field-label">
          Mapping history
          <select
            className="input"
            value={review?.id ?? ""}
            disabled={busy}
            onChange={(event) => {
              const item = history.find((r) => r.id === event.target.value);
              if (item) showReview(item);
            }}
          >
            {history.map((item) => (
              <option key={item.id} value={item.id}>
                Revision {item.version}: {item.status}
                {item.status === "approved" &&
                item.version === source.mapping_version
                  ? " (current approved mapping)"
                  : item.status === "approved"
                    ? " (historical approval)"
                    : " (proposal history)"}
              </option>
            ))}
          </select>
        </label>
      )}
      {review && (
        <div>
          <p>
            Revision {review.version}: {review.status}
          </p>
          {(review.approved_mapping ?? review.proposal?.mapping) &&
            (() => {
              const mapping =
                review.approved_mapping ?? review.proposal!.mapping!;
              const objectName = (id: string) => {
                const object = review.discovery.objects.find(
                  (item) => item.object_id === id,
                );
                return object ? `${object.schema_name}.${object.name}` : id;
              };
              return (
                <div aria-label="Mapping summary">
                  {review.status === "review" && (
                    <p>
                      Saved proposal summary. Edits below remain unapproved
                      until validation and approval.
                    </p>
                  )}
                  <p>Products: {objectName(mapping.object_id)}</p>
                  <p>
                    Identity: {mapping.key_columns.join(" + ")} · Names:{" "}
                    {mapping.name_columns.join(", ")}
                  </p>
                  <ul>
                    {mapping.categories.map((category, index) => (
                      <li key={index}>
                        Category: {objectName(category.object_id)} · Label:{" "}
                        {category.label_column}
                        <div>
                          Join:{" "}
                          {category.joins
                            .map(
                              (pair) =>
                                `${pair.product_column} → ${pair.related_column}`,
                            )
                            .join(" AND ")}
                        </div>
                      </li>
                    ))}
                  </ul>
                  <p>
                    {review.status === "approved"
                      ? "Owner-approved version; source health is checked separately."
                      : "Unapproved proposal; review semantics before approval."}
                  </p>
                </div>
              );
            })()}
          {review.proposal && (
            <>
              <p>{review.proposal.rationale}</p>
              <ul>
                {review.proposal.uncertainties.map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
              </ul>
            </>
          )}
          {review.failure_code && (
            <Alert>
              {review.failure_code === "mapping_category_join_not_unique"
                ? "The proposed category join does not cover a complete unique key. A composite key requires every join component; using one column could duplicate or miscategorize products. The existing approved mapping is unchanged."
                : `Proposal failed: ${review.failure_code}.`}{" "}
              This request will not make another call when replayed.
            </Alert>
          )}
          {review.status === "review" && (
            <form onSubmit={(event) => void approve(event)}>
              <button
                type="button"
                className="btn-secondary"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const rejected = await api.rejectMapping(
                      businessId,
                      source.id,
                      review.id,
                    );
                    setHistory((items) =>
                      items.map((item) =>
                        item.id === rejected.id ? rejected : item,
                      ),
                    );
                    showReview(rejected);
                  })
                }
              >
                Reject proposal
              </button>
              {draft && productObject && (
                <fieldset disabled={busy}>
                  <legend>Product display fields</legend>
                  <p>
                    Table or view: {productObject.schema_name}.
                    {productObject.name}. Identity:{" "}
                    {draft.key_columns.join(" + ")}.
                  </p>
                  <label className="field-label">
                    Product name columns
                    <select
                      className="input"
                      multiple
                      value={draft.name_columns}
                      onChange={(event) =>
                        editDisplayFields({
                          name_columns: Array.from(
                            event.target.selectedOptions,
                            (option) => option.value,
                          ),
                        })
                      }
                    >
                      {productObject.columns
                        .filter((column) => column.kind === "text")
                        .map((column) => (
                          <option key={column.name} value={column.name}>
                            {column.name}
                          </option>
                        ))}
                    </select>
                  </label>
                  <p>
                    Choose one to three columns containing product names. Column
                    names alone do not prove their meaning.
                  </p>
                  <label className="field-label">
                    SKU column (optional)
                    <select
                      className="input"
                      value={draft.sku_column ?? ""}
                      onChange={(event) =>
                        editDisplayFields({
                          sku_column: event.target.value || null,
                        })
                      }
                    >
                      <option value="">No SKU assigned</option>
                      {productObject.columns
                        .filter((column) => column.kind === "text")
                        .map((column) => (
                          <option key={column.name} value={column.name}>
                            {column.name}
                          </option>
                        ))}
                    </select>
                  </label>
                  <p>
                    Keys, relationships and row isolation remain available in
                    the advanced configuration.
                  </p>
                </fieldset>
              )}
              <details>
                <summary>Advanced: edit mapping configuration</summary>
                <label htmlFor={editorId}>
                  Review the structured product mapping
                </label>
                <textarea
                  id={editorId}
                  rows={12}
                  value={mappingText}
                  onChange={(event) => {
                    setMappingText(event.target.value);
                    setConfirmed(false);
                  }}
                  disabled={busy}
                />
                <p>
                  Use only discovered objects, columns, keys and joins. SQL and
                  expressions are rejected.
                </p>
              </details>
              <label>
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(event) => setConfirmed(event.target.checked)}
                  disabled={busy}
                />{" "}
                I confirm these fields describe products and acknowledge the
                uncertainties and missing capabilities.
              </label>
              <button
                className="btn"
                disabled={
                  busy ||
                  !confirmed ||
                  !draft ||
                  draft.name_columns.length < 1 ||
                  draft.name_columns.length > 3
                }
              >
                Validate and approve mapping
              </button>
            </form>
          )}
          {review.validation_notes.length > 0 && (
            <ul>
              {review.validation_notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {review && draft && ["review", "approved"].includes(review.status) && (
        <CatalogueQuality
          key={`${businessId}:${source.id}:${review.id}`}
          businessId={businessId}
          source={source}
          revisionId={review.id}
          mapping={draft}
          onRefresh={onRefresh}
        />
      )}
      {source.status === "ACTIVE" && (
        <div>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void search({ query });
            }}
          >
            <label htmlFor={searchId}>Search source catalogue</label>
            <input
              id={searchId}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              maxLength={128}
              disabled={busy}
            />
            <button className="btn-secondary" disabled={busy || !query.trim()}>
              Search products
            </button>
          </form>
          {results && (
            <div aria-live="polite">
              {results.coverage?.disclosure && (
                <p className="quality-warning">{results.coverage.disclosure}</p>
              )}
              <p>
                {results.status === "not_found"
                  ? "No source-backed matches."
                  : results.status === "ambiguous"
                    ? "Choose the intended variant."
                    : "Source-backed catalogue details."}
              </p>
              {results.truncated && (
                <p>More matches exist. Narrow your search.</p>
              )}
              <ul>
                {results.items.map((item) => (
                  <li key={item.external_product_id}>
                    <button
                      disabled={busy}
                      className="btn-secondary"
                      onClick={() =>
                        void search({
                          external_product_id: item.external_product_id,
                          mapping_version: results.mapping_version,
                        })
                      }
                    >
                      {item.names.join(" / ") || item.external_product_id}
                    </button>
                    <p>
                      ID: {item.external_product_id}; categories:{" "}
                      {item.categories.join(" / ") || "Unknown"}; stock:
                      unknown.
                    </p>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
      <CapabilityDashboard
        key={`${businessId}:${source.id}`}
        businessId={businessId}
        source={source}
        onRefresh={onRefresh}
        onCatalogueReview={() => {
          const approved = history.find(
            (item) =>
              item.status === "approved" &&
              item.version === source.mapping_version,
          );
          if (approved) showReview(approved);
        }}
      />
    </section>
  );
}
