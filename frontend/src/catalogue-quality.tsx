import { useEffect, useId, useRef, useState } from "react";
import { api, ApiError, DataSource, ProductMapping, QualityReport } from "./api";
import { Alert, BusyLabel } from "./ui";

const impactLabels = {
  SAFE_FOR_CAPABILITY: "Safe for this capability",
  SAFE_WITH_EXCLUSIONS: "Safe with reviewed catalogue exclusions",
  BLOCKED_BY_MAPPING: "Mapping must be corrected",
  BLOCKED_BY_DATA_QUALITY: "Required data is invalid",
  INSUFFICIENT_CAPABILITY_EVIDENCE: "Required capability evidence is missing",
  SAFE_FOR_PARTIAL_CATALOGUE: "Safe for catalogue exclusions",
  REQUIRES_OWNER_REVIEW: "Separate owner review required",
  UNSAFE: "Unsafe to exclude",
  UNKNOWN_IMPACT: "Relationship impact is unknown",
};

const errorLabels: Record<string, string> = {
  catalogue_quality_report_stale:
    "The source or mapping changed, or this report expired. Revalidate before approving.",
  catalogue_validation_in_progress:
    "Another validation or approval is running for this source. Wait for it to finish, then retry.",
  catalogue_quality_confirmation_invalid:
    "The confirmation does not match this report. Review fresh validation details.",
  catalogue_operational_impact_unverified:
    "Operational approvals need their own verified exclusion contract. Catalogue exclusions are blocked.",
  mapping_disable_before_approval:
    "Disable this active source before changing its approval.",
};

export function CatalogueQuality({
  businessId,
  source,
  revisionId,
  mapping,
  onRefresh,
}: {
  businessId: string;
  source: DataSource;
  revisionId: string;
  mapping: ProductMapping;
  onRefresh: () => Promise<void>;
}) {
  const [report, setReport] = useState<QualityReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [notice, setNotice] = useState("");
  const id = useId();
  const epoch = useRef(0);
  const mappingJson = JSON.stringify(mapping);

  useEffect(() => {
    let cancelled = false;
    const generation = ++epoch.current;
    void api
      .catalogueQuality(businessId, source.id)
      .then((value) => {
        if (!cancelled && generation === epoch.current) setReport(value);
      })
      .catch(() => {
        if (!cancelled && generation === epoch.current)
          setError(
            "Quality evidence could not be loaded. Revalidate to obtain a report.",
          );
      });
    return () => {
      cancelled = true;
    };
  }, [businessId, source.id]);

  useEffect(() => {
    setConfirmed(false);
    setReviewing(false);
    setNotice("");
  }, [revisionId, mappingJson, report?.id]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (caught) {
      setConfirmed(false);
      setError(
        caught instanceof ApiError
          ? (errorLabels[caught.code] ?? caught.message)
          : "Quality validation could not finish. No approval was saved.",
      );
    } finally {
      setBusy(false);
    }
  }

  const current =
    report?.revision_id === revisionId &&
    JSON.stringify(report.mapping) === mappingJson;
  const fresh = !!report && new Date(report.expires_at).getTime() > Date.now();
  const full = report?.invalid_records === 0 && report.structural_status === "PASS";
  const activeRenewal =
    source.status === "ACTIVE" &&
    source.mapping_version === report?.mapping_version &&
    !!source.catalogue_approval_expires_at &&
    ["full", "partial", "catalogue_partial_approval_expired"].includes(
      source.quality_approval_status ?? "",
    );
  const eligible =
    current &&
    fresh &&
    (report?.eligible || full) &&
    (source.status !== "ACTIVE" ||
      (activeRenewal &&
        !!source.last_validated_at &&
        !!report &&
        new Date(report.validated_at).getTime() >
          new Date(source.last_validated_at).getTime()));
  const coverage = source.catalogue_coverage;

  return (
    <section className="catalogue-quality" aria-labelledby={id}>
      <header className="quality-heading">
        <div>
          <h3 id={id}>Data quality</h3>
          <p>Full-scope checks, reviewed coverage and source limitations.</p>
        </div>
        <button
          className="btn-secondary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              const generation = ++epoch.current;
              const value = await api.validateCatalogueQuality(
                businessId,
                source.id,
                revisionId,
                mapping,
              );
              if (generation !== epoch.current) return;
              setReport(value);
              setConfirmed(false);
              await onRefresh();
            })
          }
        >
          <BusyLabel
            busy={busy}
            idle={report ? "Revalidate catalogue" : "Validate catalogue quality"}
          />
        </button>
      </header>
      {error && <Alert>{error}</Alert>}
      {notice && <p role="status">{notice}</p>}
      {coverage?.approval_type === "partial" && (
        <div className="quality-warning" role="status">
          <strong>
            {source.status === "ACTIVE"
              ? "Active — partial catalogue"
              : "Partial coverage approved"}
          </strong>
          <p>{coverage.disclosure}</p>
          <p>
            Approval expires:{" "}
            {coverage.expires_at
              ? new Date(coverage.expires_at).toLocaleString()
              : "Unknown"}
            . Changed source data requires new validation.
          </p>
          <button
            className="btn-secondary"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await api.revokeCatalogueQuality(businessId, source.id);
                setConfirmed(false);
                await onRefresh();
              })
            }
          >
            Revoke partial approval
          </button>
        </div>
      )}
      {!report ? (
        <p>
          No quality report yet. Validation reads the approved scope without changing
          source records.
        </p>
      ) : (
        <>
          {!current && (
            <Alert>
              This report belongs to a different mapping or draft. Revalidate the
              mapping displayed here.
            </Alert>
          )}
          {!fresh && (
            <Alert>
              This report has expired. Revalidate before requesting approval.
            </Alert>
          )}
          <dl className="quality-metrics">
            {[
              ["Total records", report.total_records],
              ["Valid records", report.valid_records],
              ["Invalid records", report.invalid_records],
              [
                "Invalid percentage",
                report.invalid_percent === null
                  ? null
                  : `${Number(report.invalid_percent).toFixed(6)}%`,
              ],
              ["Tolerance (inclusive)", `${report.max_invalid_percent}%`],
            ].map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value ?? "Not verified"}</dd>
              </div>
            ))}
          </dl>
          <dl className="quality-health">
            <div>
              <dt>Connection</dt>
              <dd>
                {report.total_records !== null
                  ? "Read succeeded at validation"
                  : "Not verified"}
              </dd>
            </div>
            <div>
              <dt>Structural mapping</dt>
              <dd>{report.structural_status}</dd>
            </div>
            <div>
              <dt>Data quality</dt>
              <dd>
                {report.invalid_records === null
                  ? "Incomplete"
                  : report.invalid_records === 0
                    ? "Full catalogue validated"
                    : `${report.invalid_records} records need review`}
              </dd>
            </div>
            <div>
              <dt>Source activation</dt>
              <dd>
                {source.status}
                {coverage?.approval_type === "partial" ? " — partial coverage" : ""}
              </dd>
            </div>
          </dl>
          <div className={full ? "quality-neutral" : "quality-warning"} role="status">
            <strong>
              {full
                ? "Validated — full catalogue"
                : report.eligible
                  ? "Review required — partial approval eligible"
                  : "Approval blocked"}
            </strong>
            <p>{impactLabels[report.impact]}</p>
            <p>{report.recommended_action}</p>
          </div>
          <p>
            Being below the tolerance never grants approval. Stock, pricing, sales and
            financial capabilities require separate verified mappings.
          </p>
          <details>
            <summary>View validation details</summary>
            <p>
              Mapping revision {report.mapping_version} ·{" "}
              {new Date(report.validated_at).toLocaleString()}
            </p>
            <dl>
              {Object.entries(report.reason_counts).map(([rule, count]) => (
                <div key={rule}>
                  <dt>{rule.replaceAll("_", " ")}</dt>
                  <dd>{count}</dd>
                </div>
              ))}
            </dl>
            <dl>
              {Object.entries(report.relationship_counts ?? {}).map(([rule, count]) => (
                <div key={rule}>
                  <dt>{rule.replaceAll("_", " ")}</dt>
                  <dd>{count} optional relationships without a label</dd>
                </div>
              ))}
            </dl>
            <ul>
              {report.findings.map((finding) => (
                <li key={finding}>{finding.replaceAll("_", " ")}</li>
              ))}
            </ul>
            <p>
              Exact rate: {report.invalid_records ?? "?"} /{" "}
              {report.total_records ?? "?"} × 100 ≈{" "}
              {report.invalid_percent ?? "unknown"}%.
            </p>
            <p className="quality-fingerprint">
              Schema fingerprint: {report.schema_fingerprint}
            </p>
          </details>
          <details
            open={reviewing}
            onToggle={(event) => {
              setReviewing(event.currentTarget.open);
              if (!event.currentTarget.open) setConfirmed(false);
            }}
          >
            <summary>
              Review exclusions ({report.invalid_records ?? "unverified"})
            </summary>
            <p>
              Only approved identifiers and names are shown. An unavailable identifier
              has no invented replacement key. Diagnostics are bounded to 50 records.
            </p>
            <div className="quality-table-scroll">
              <table className="quality-table">
                <caption>Invalid catalogue records</caption>
                <thead>
                  <tr>
                    <th scope="col">Identifier / name</th>
                    <th scope="col">Failure and owner action</th>
                    <th scope="col">Exclusion</th>
                  </tr>
                </thead>
                <tbody>
                  {report.diagnostics.map((row, index) => (
                    <tr key={index}>
                      <td>
                        <strong>{row.identifier ?? "Identifier unavailable"}</strong>
                        <div>{row.name ?? "Name unavailable"}</div>
                      </td>
                      <td>
                        {row.explanation}
                        <details>
                          <summary>Relationship concerns and suggested action</summary>
                          <p>{row.relationship_concerns.join(" ")}</p>
                          <p>{row.suggested_action}</p>
                          <p>{row.locator_note}</p>
                        </details>
                      </td>
                      <td>
                        {row.severity} ·{" "}
                        {row.safe_exclusion ? "Proven safe" : "Blocked"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {report.diagnostics_truncated && (
              <p>
                Additional invalid records exist; exact aggregate counts above cover the
                full scope.
              </p>
            )}
            <label className="quality-confirm">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={busy || !eligible}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              I confirm the mapping semantics and limitations and approve{" "}
              {full
                ? "full catalogue coverage with zero exclusions"
                : `excluding exactly ${report.invalid_records} of ${report.total_records} records for catalogue use only`}
              . This approval does not authorize stock or financial calculations.
            </label>
            <button
              className="btn"
              disabled={busy || !eligible || !confirmed || !reviewing}
              onClick={() =>
                void run(async () => {
                  await api.approveCatalogueQuality(businessId, source.id, report);
                  setConfirmed(false);
                  setNotice(
                    activeRenewal
                      ? "Catalogue approval renewed. The source remains active; other capability approvals are unchanged."
                      : "Coverage approval saved. Activate the source separately after its health check.",
                  );
                  await onRefresh();
                })
              }
            >
              {activeRenewal
                ? "Renew catalogue approval"
                : full
                  ? "Approve full catalogue"
                  : "Approve partial catalogue"}
            </button>
            {!eligible && (
              <p>
                Approval requires a current report for this exact mapping, safe impact
                and permitted tolerance. Active-source renewal also requires its
                unchanged, previously approved mapping; new or modified mappings require
                an inactive source. Renewal requires a new validation after the previous
                approval.
              </p>
            )}
          </details>
        </>
      )}
    </section>
  );
}
