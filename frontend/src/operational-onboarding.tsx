import { useId, useState } from "react";
import { api, ApiError, DataSource, OperationalWorkspace } from "./api";
import { Alert } from "./ui";

export function OperationalOnboarding({
  businessId,
  source,
  onRefresh,
}: {
  businessId: string;
  source: DataSource;
  onRefresh: () => Promise<void>;
}) {
  const [workspace, setWorkspace] = useState<OperationalWorkspace | null>(null);
  const [mapping, setMapping] = useState("");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [validated, setValidated] = useState(false);
  const [semantics, setSemantics] = useState(false);
  const [isolation, setIsolation] = useState(false);
  const [approvalNeedsReview, setApprovalNeedsReview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mappingId = useId();
  const queryId = useId();

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "Check the mapping and request fields.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function load() {
    const value = await api.operationalWorkspace(businessId, source.id);
    setWorkspace(value);
    setMapping(JSON.stringify(value.history[0]?.mapping ?? value.context, null, 2));
    setValidated(false);
    setSemantics(false);
    setIsolation(false);
    setResult(null);
    setApprovalNeedsReview(false);
  }

  async function submit(action: "validate" | "preview" | "approve") {
    if (!workspace) return;
    if (action === "approve" && approvalNeedsReview) return;
    const request = {
      mapping: JSON.parse(mapping),
      proposals: workspace.proposals,
      discovery: workspace.discovery,
      ...(action === "preview" ? { request: JSON.parse(query) } : {}),
      ...(action === "approve"
        ? { confirm_semantics: semantics, confirm_row_isolation: isolation }
        : {}),
    };
    // An approval can commit even if its response is lost. Require fresh history
    // before another attempt; drafts and previews do not have this side effect.
    if (action === "approve") setApprovalNeedsReview(true);
    const value = await api.reviewOperations(businessId, source.id, action, request);
    setResult(value);
    if (action === "validate") setValidated(true);
    if (action === "approve") {
      setValidated(false);
      setSemantics(false);
      setIsolation(false);
      try {
        await onRefresh();
        await load();
      } catch {
        setError(
          "Approval was recorded, but refreshed history is unavailable. Prepare the proposal again to review the saved version.",
        );
      }
      setResult(value);
    }
  }

  return (
    <section className="mapping-preview" aria-label="Operational mapping review">
      <h3>Operational mappings</h3>
      <p>
        Review stock, availability, prices, sales, revenue, refunds, best sellers and
        restocking independently. Only capabilities included in the approved version
        become available.
      </p>
      {error && <Alert>{error}</Alert>}
      {approvalNeedsReview && (
        <p role="status">
          An approval request was sent. Prepare the proposal again and review its
          history before approving another version.
        </p>
      )}
      <button className="btn-secondary" disabled={busy} onClick={() => void run(load)}>
        Prepare operational proposal
      </button>
      {workspace && (
        <>
          <ul>
            {workspace.limitations.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
          <p>
            Proposal uses permitted metadata and owner-supplied semantics. No AI call is
            made.
          </p>
          <details>
            <summary>Discovered evidence and mapping field guide</summary>
            <pre>
              {JSON.stringify(
                {
                  discovery: workspace.discovery,
                  products: workspace.product_mapping,
                  contract: workspace.mapping_schema,
                },
                null,
                2,
              )}
            </pre>
          </details>
          <p>
            Approved versions:{" "}
            {workspace.history.map((item) => item.version).join(", ") || "None"}.
            Approval retains immutable history and replaces the complete capability set.
          </p>
          {source.status === "ACTIVE" ? (
            <p>
              Disable this source before validating, previewing or approving a
              replacement mapping.
            </p>
          ) : (
            <>
              <label htmlFor={mappingId}>Proposed operational mapping</label>
              <textarea
                id={mappingId}
                rows={16}
                value={mapping}
                disabled={busy}
                onChange={(event) => {
                  setMapping(event.target.value);
                  setValidated(false);
                  setSemantics(false);
                  setIsolation(false);
                  setResult(null);
                }}
              />
              <button
                className="btn-secondary"
                disabled={busy || !mapping.trim()}
                onClick={() => void run(() => submit("validate"))}
              >
                Validate operational structure
              </button>
              <label htmlFor={queryId}>Bounded preview request</label>
              <textarea
                id={queryId}
                rows={5}
                value={query}
                disabled={busy}
                placeholder={
                  '{"capability":"stock","catalogue_revision":1,"mapping_revision":1,"limit":10}'
                }
                onChange={(event) => {
                  setQuery(event.target.value);
                  setResult(null);
                }}
              />
              <button
                className="btn-secondary"
                disabled={busy || !validated || !query.trim()}
                onClick={() => void run(() => submit("preview"))}
              >
                Preview source results
              </button>
              <p>
                Preview is an unapproved draft. Compare quantities, units, currency,
                periods and amounts with authoritative source records.
              </p>
              <label>
                <input
                  type="checkbox"
                  checked={semantics}
                  disabled={busy || !validated}
                  onChange={(event) => setSemantics(event.target.checked)}
                />{" "}
                I reviewed source semantics, evidence and limitations for every included
                capability.
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={isolation}
                  disabled={busy || !validated}
                  onChange={(event) => setIsolation(event.target.checked)}
                />{" "}
                I confirm business row isolation, including joined dimensions.
              </label>
              <button
                className="btn"
                disabled={
                  busy || approvalNeedsReview || !validated || !semantics || !isolation
                }
                onClick={() => void run(() => submit("approve"))}
              >
                Approve operational version
              </button>
            </>
          )}
          {result && <pre aria-live="polite">{JSON.stringify(result, null, 2)}</pre>}
        </>
      )}
    </section>
  );
}
