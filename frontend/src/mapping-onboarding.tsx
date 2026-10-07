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
  const [mappingText, setMappingText] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CatalogueResult | null>(null);
  const editorId = useId();
  const searchId = useId();
  const replayKey = useRef<string | null>(null);

  function showReview(value: MappingReview) {
    setReview(value);
    setDiscovery(value.discovery);
    setMappingText(
      value.proposal?.mapping ? JSON.stringify(value.proposal.mapping, null, 2) : "",
    );
    setConfirmed(false);
  }

  useEffect(() => {
    let cancelled = false;
    void api
      .mappingReviews(businessId, source.id)
      .then((items) => {
        if (!cancelled && items[0]) showReview(items[0]);
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
    replayKey.current ??= sessionStorage.getItem(storageKey) ?? crypto.randomUUID();
    sessionStorage.setItem(storageKey, replayKey.current);
    showReview(await api.proposeMapping(businessId, source.id, replayKey.current));
  }

  async function approve(event: FormEvent) {
    event.preventDefault();
    if (!review || !confirmed) return;
    await run(async () => {
      const mapping = JSON.parse(mappingText) as ProductMapping;
      showReview(await api.approveMapping(businessId, source.id, review.id, mapping));
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
        await api.searchCatalogue(businessId, source.id, { ...selector, limit: 50 }),
      ),
    );
  }

  return (
    <section className="mapping-preview" aria-label="Catalogue mapping review">
      <h3>Catalogue mapping</h3>
      <p>
        Products only. Stock is unknown; inventory, prices, sales and refunds are
        disabled.
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
        Proposal requests use the business AI allowance when Gemini is configured.
        Metadata only; no product records are sent.
      </p>
      {discovery && (
        <ul>
          {discovery.objects.map((object) => (
            <li key={object.object_id}>
              <strong>
                {object.object_id}: {object.schema_name}.{object.name}
              </strong>
              <p>Columns: {object.columns.map((column) => column.name).join(", ")}</p>
              <p>
                Unique keys:{" "}
                {object.unique_keys.map((key) => key.join(" + ")).join("; ") || "None"}
              </p>
            </li>
          ))}
        </ul>
      )}
      {review && (
        <div>
          <p>
            Revision {review.version}: {review.status}
          </p>
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
              Proposal failed: {review.failure_code}. This request will not make another
              call when replayed.
            </Alert>
          )}
          {review.status === "review" && (
            <form onSubmit={(event) => void approve(event)}>
              <label htmlFor={editorId}>Review the structured product mapping</label>
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
                disabled={busy || !confirmed || !mappingText.trim()}
              >
                Validate and approve mapping
              </button>
            </form>
          )}
          {review.status === "approved" && (
            <ul>
              {review.validation_notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
        </div>
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
              <p>
                {results.status === "not_found"
                  ? "No source-backed matches."
                  : results.status === "ambiguous"
                    ? "Choose the intended variant."
                    : "Source-backed catalogue details."}
              </p>
              {results.truncated && <p>More matches exist. Narrow your search.</p>}
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
                      {item.categories.join(" / ") || "Unknown"}; stock: unknown.
                    </p>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
