import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  api,
  ApiError,
  CapabilityFamily,
  CapabilityQualityReport,
  CapabilityState,
  CapabilityWorkspace,
  DataSource,
} from "./api";
import { Alert, BusyLabel } from "./ui";

const labels: Record<CapabilityFamily, string> = {
  inventory: "Inventory",
  pricing: "Pricing",
  sales: "Sales",
  financial: "Financial reporting",
};
const statusLabel = (value: string) => value.toLowerCase().replaceAll("_", " ");
const enables: Record<CapabilityFamily, string> = {
  inventory:
    "Answer stock and availability questions for verified units and locations.",
  pricing:
    "Look up the exact reviewed source price, currency, unit and pricing context. Retail meaning requires confirmation.",
  sales:
    "Answer sales questions for reviewed dates and transaction states. Product rankings also need verified product quantities.",
  financial:
    "Report revenue and refunds using the reviewed amount, tax and return definitions.",
};
function reviewStatus(state: CapabilityState) {
  if (state.status === "ACTIVE") return "Ready";
  if (state.status === "SOURCE_UNAVAILABLE")
    return "Connection needs attention";
  if (state.approval_status === "REVOKED") return "Approval revoked";
  if (state.approval_status === "EXPIRED") return "Revalidation required";
  if (!state.report && state.capability === "inventory")
    return "Current stock not proven";
  if (!state.report && state.capability === "financial")
    return "Revenue definition unverified";
  if (state.report?.eligible)
    return new Date(state.report.expires_at).getTime() > Date.now()
      ? "Ready for approval review"
      : "Revalidation required";
  if (state.status === "VALIDATION_FAILED") return "Validation needs attention";
  return "Mapping review required";
}
function nextAction(state: CapabilityState) {
  if (state.status === "SOURCE_UNAVAILABLE")
    return "Restore the source connection, then refresh capabilities before validation or approval.";
  if (state.status === "ACTIVE")
    return "This capability is enabled. Use Owner Chat for its approved operations.";
  if (
    state.approval_status === "EXPIRED" ||
    (state.report && new Date(state.report.expires_at).getTime() <= Date.now())
  )
    return "Revalidate this capability before reviewing a fresh approval.";
  if (state.report?.eligible)
    return "Review the business meaning and scope, then explicitly approve this capability.";
  if (state.report)
    return "Resolve the reported validation findings, then validate again.";
  return "Confirm the business meaning and source access, then review and validate the mapping.";
}
const errorLabels: Record<string, string> = {
  capability_report_stale:
    "The relevant source data or scope changed. Revalidate before approving.",
  capability_identity_mapping_required:
    "Review the product identity mapping first. Invalid display names do not block operational validation.",
  mapping_fact_key_not_unique:
    "Choose the full enforced primary or unique fact key.",
  mapping_join_not_unique:
    "A required relationship must cover the complete target key.",
  mapping_status_classification_overlap:
    "Completed and excluded statuses must be different.",
  mapping_price_timezone_unsupported:
    "This price date field does not prove an effective timezone. Review the date contract before approval.",
};

export function CapabilityDashboard({
  businessId,
  source,
  onRefresh,
  onCatalogueReview,
}: {
  businessId: string;
  source: DataSource;
  onRefresh: () => Promise<void>;
  onCatalogueReview?: () => void;
}) {
  const [workspace, setWorkspace] = useState<CapabilityWorkspace | null>(null);
  const [selected, setSelected] = useState<CapabilityFamily | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  const load = useCallback(async () => {
    const generation = ++epoch.current;
    setBusy(true);
    setError("");
    try {
      const value = await api.capabilityWorkspace(businessId, source.id);
      if (generation === epoch.current) setWorkspace(value);
    } catch (caught) {
      if (generation === epoch.current)
        setError(
          caught instanceof ApiError
            ? caught.message
            : "Capability details could not be loaded. Retry discovery.",
        );
    } finally {
      if (generation === epoch.current) setBusy(false);
    }
  }, [businessId, source.id]);
  const cancelLoad = useCallback(() => {
    epoch.current++;
  }, []);
  useEffect(() => {
    setWorkspace(null);
    setSelected(null);
    void load();
    return cancelLoad;
  }, [load, cancelLoad]);
  const active = workspace?.capabilities.find(
    (state) => state.capability === selected,
  );
  const detectedSources = active?.report
    ? active.report.discovery.objects
    : workspace?.discovery?.objects.filter((object) =>
        active?.candidates.some(
          (candidate) => candidate.object_id === object.object_id,
        ),
      );
  return (
    <section className="capability-dashboard" aria-label="Capability dashboard">
      <header className="quality-heading">
        <div>
          <h3>Business capabilities</h3>
          <p>
            Approve each operation independently. Catalogue exclusions do not
            approve stock, prices or financial reporting.
          </p>
        </div>
        <button
          className="btn-secondary"
          disabled={busy}
          onClick={() => void load()}
        >
          <BusyLabel busy={busy} idle="Refresh capabilities" />
        </button>
      </header>
      {error && <Alert>{error}</Alert>}
      {workspace && (
        <>
          <p className="capability-connection">
            Connection:{" "}
            <strong>{statusLabel(workspace.connection_status)}</strong>.
            Capability failures are shown separately.
          </p>
          <div className="capability-list">
            <article className="capability-summary">
              <div>
                <h4>Catalogue</h4>
                <span className="quality-state">
                  {source.status === "ACTIVE"
                    ? source.catalogue_coverage?.approval_type === "partial"
                      ? "active — partial catalogue"
                      : "active"
                    : "review required"}
                </span>
              </div>
              <p>
                Mapping:{" "}
                {source.mapping_version
                  ? `approved version ${source.mapping_version}`
                  : "approval required"}
                . Catalogue health and exclusions have their own review.
              </p>
              {source.catalogue_coverage && (
                <p>
                  {source.catalogue_coverage.valid_records?.toLocaleString()}{" "}
                  valid; {source.catalogue_coverage.excluded_records} excluded
                  of {source.catalogue_coverage.total_records?.toLocaleString()}{" "}
                  records.
                </p>
              )}
              <button
                className="btn-secondary"
                onClick={onCatalogueReview}
                disabled={!onCatalogueReview}
              >
                Review catalogue quality
              </button>
            </article>
            {workspace.capabilities.map((state) => (
              <article
                className={`capability-summary ${state.status === "ACTIVE" ? "capability-active" : ""}`}
                key={state.capability}
              >
                <div>
                  <h4>{labels[state.capability]}</h4>
                  <span className="quality-state">{reviewStatus(state)}</span>
                </div>
                <dl className="capability-health">
                  <div>
                    <dt>Mapping</dt>
                    <dd>{statusLabel(state.mapping_status)}</dd>
                  </div>
                  <div>
                    <dt>Quality</dt>
                    <dd>{statusLabel(state.quality_status)}</dd>
                  </div>
                  <div>
                    <dt>Approval</dt>
                    <dd>{statusLabel(state.approval_status)}</dd>
                  </div>
                  <div>
                    <dt>Runtime</dt>
                    <dd>{statusLabel(state.runtime_status)}</dd>
                  </div>
                </dl>
                {state.report && (
                  <p>
                    {state.report.valid_records?.toLocaleString() ?? "Unknown"}{" "}
                    valid /{" "}
                    {state.report.total_records?.toLocaleString() ?? "unknown"}{" "}
                    relevant records;{" "}
                    {state.report.invalid_records ?? "unknown"} invalid. No
                    operational records excluded.
                  </p>
                )}
                <p>
                  {!state.report && state.capability === "inventory"
                    ? "No authoritative current stock calculation has been validated from the available source evidence."
                    : !state.report && state.capability === "financial"
                      ? "Revenue definition has not been established from the available source evidence."
                      : state.prerequisites[0]}
                </p>
                <button
                  className="btn-secondary"
                  aria-expanded={selected === state.capability}
                  onClick={() =>
                    setSelected(
                      selected === state.capability ? null : state.capability,
                    )
                  }
                >
                  Review {labels[state.capability].toLowerCase()}
                </button>
              </article>
            ))}
          </div>
          {active && (
            <>
              <section
                className="capability-review-guide"
                aria-label={`${labels[active.capability]} next steps`}
              >
                <h4>Review {labels[active.capability].toLowerCase()}</h4>
                <dl>
                  <div>
                    <dt>What approval enables</dt>
                    <dd>{enables[active.capability]}</dd>
                  </div>
                  <div>
                    <dt>Discovered data</dt>
                    <dd>
                      {!!detectedSources?.length && (
                        <p>
                          {active.report
                            ? "Validated source objects"
                            : "Detected candidates in approved scope"}
                          :{" "}
                          {detectedSources
                            .map(
                              (object) =>
                                `${object.schema_name}.${object.name}`,
                            )
                            .join(", ")}
                          .
                        </p>
                      )}
                      {active.status === "SOURCE_UNAVAILABLE"
                        ? "The source connection is unavailable. Previous reports do not verify its current data."
                        : active.candidates.length
                          ? `${active.candidates.length} source candidates have readable fields and an enforced key in the approved query scope. Field names alone do not establish their business meaning.`
                          : "No operational source candidate with an enforced key is available in the approved query scope. Ask the operator to review the exact readable columns; this does not mean the database has no operational data."}
                    </dd>
                  </div>
                  <div>
                    <dt>Suggested business interpretation</dt>
                    <dd>
                      {active.report
                        ? active.report.meaning
                        : `${enables[active.capability]} The source meaning still needs confirmation.`}
                      {active.report && active.status !== "ACTIVE" && (
                        <p>
                          Submitted for review; this interpretation is not
                          active.
                        </p>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>What validation proved</dt>
                    <dd>
                      {active.report
                        ? `${active.report.structural_status === "PASS" ? "Mapping structure passed." : "Mapping structure is not verified."} ${active.report.eligible ? "The report is eligible for approval review; freshness and source data are checked again when approving." : "This report does not permit approval."}`
                        : "No capability quality report has been completed. No source values or calculations are approved."}
                    </dd>
                  </div>
                  <div>
                    <dt>What needs confirmation</dt>
                    <dd>
                      <ul>
                        {active.prerequisites.map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    </dd>
                  </div>
                  <div>
                    <dt>Next available action</dt>
                    <dd>{nextAction(active)}</dd>
                  </div>
                </dl>
                <p>
                  Review a validated mapping first. Approval enables only this
                  capability. Source access and business definitions require
                  separate confirmation.
                </p>
              </section>
              {workspace.discovery && (
                <CapabilityEditor
                  key={`${businessId}:${source.id}:${active.capability}`}
                  businessId={businessId}
                  source={source}
                  workspace={workspace}
                  state={active}
                  onChanged={async () => {
                    await load();
                    await onRefresh();
                  }}
                />
              )}
            </>
          )}
          <details className="capability-access">
            <summary>Reader access and missing prerequisites</summary>
            <p>{workspace.access_inventory.limitation}</p>
            {workspace.access_inventory.truncated && (
              <p>
                Metadata is truncated. Omitted objects have unknown
                availability.
              </p>
            )}
            {workspace.access_inventory.objects?.map((object) => (
              <details key={`${object.schema_name}.${object.object_name}`}>
                <summary>
                  {object.schema_name}.{object.object_name}
                </summary>
                <ul>
                  {object.columns.map((column) => (
                    <li key={column.name}>
                      {column.name} ({column.kind}):{" "}
                      {column.approved_scope
                        ? "approved read scope"
                        : column.readable
                          ? "readable; operator scope review required"
                          : "SELECT access not available"}
                    </li>
                  ))}
                </ul>
              </details>
            ))}
            <p>
              Missing fields require narrowly scoped, separately authorized
              SELECT access and an execution allowlist review. No write or
              administrator permissions are needed.
            </p>
          </details>
        </>
      )}
    </section>
  );
}

type Ref = { object_id: string; column: string };
type DraftFields = Record<string, string>;
function parseRef(value: string): Ref {
  const [object_id, ...column] = value.split("/");
  if (!object_id || !column.length)
    throw new Error("Choose all required source fields.");
  return { object_id, column: column.join("/") };
}
const split = (value: string) =>
  value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);

function buildMapping(
  workspace: CapabilityWorkspace,
  family: CapabilityFamily,
  fields: DraftFields,
): Record<string, unknown> {
  const discovery = workspace.discovery!;
  const products = workspace.identity_mapping;
  const object = discovery.objects.find((o) => o.object_id === fields.object);
  if (!object) throw new Error("Choose an authoritative source object.");
  const key = object.unique_keys[Number(fields.key ?? 0)];
  if (!key)
    throw new Error("The source needs an enforced primary or unique key.");
  const header =
    (family === "sales" || family === "financial") &&
    fields.grain === "transaction_events";
  const productColumns = header
    ? []
    : products
      ? products.key_columns.map((_, index) => fields[`product_${index}`])
      : split(fields.product_columns || "");
  if (productColumns.some((value) => !value))
    throw new Error("Map every product identity component.");
  const usedTargets = new Set(
    Object.values(fields)
      .filter((v) => v.includes("/"))
      .map((v) => v.split("/")[0]),
  );
  const joins = (discovery.relationships ?? [])
    .filter(
      (r) =>
        r.source_object_id === object.object_id &&
        r.target_object_id !== products?.object_id &&
        usedTargets.has(r.target_object_id),
    )
    .map((r) => ({
      target_object_id: r.target_object_id,
      fact_columns: r.source_columns,
      target_columns: r.target_columns,
    }));
  const fact: Record<string, unknown> = {
    object_id: object.object_id,
    key_columns: key,
    product_columns: productColumns,
    product_reference: header
      ? "none"
      : products
        ? fields.productReference || "catalogue_identity"
        : "fact_identity",
    joins,
    unit: header ? null : parseRef(fields.unit || ""),
    row_scopes: (products?.row_scopes ?? []).map((scope, scopeIndex) => {
      if (header)
        return {
          field: parseRef(fields[`scope_${scopeIndex}`] || ""),
          value: scope.value,
        };
      const index = products!.key_columns.indexOf(scope.column);
      if (index < 0) throw new Error("Advanced partition review is required.");
      return {
        field: { object_id: object.object_id, column: productColumns[index] },
        value: scope.value,
      };
    }),
  };
  for (const name of ["location", "branch", "warehouse"])
    if (fields[name]) fact[name] = parseRef(fields[name]);
  const mapping: Record<string, unknown> = {
    ...workspace.context,
    contract_version: 1,
    timezone: fields.timezone,
    scope_reviewed: true,
    external_scope: (products?.row_scopes ?? []).length
      ? "partitioned"
      : "dedicated",
  };
  if (family === "inventory") {
    mapping.stock = {
      ...fact,
      on_hand: parseRef(fields.on_hand || ""),
      availability_rule: fields.availability_rule || "unsupported",
    };
    const stock = mapping.stock as Record<string, unknown>;
    if (fields.available) stock.available = parseRef(fields.available);
    if (fields.reserved) {
      stock.reserved = parseRef(fields.reserved);
      stock.reservations_complete = fields.reservations_complete === "yes";
    }
  } else if (family === "pricing") {
    mapping.prices = {
      ...fact,
      amount: parseRef(fields.amount || ""),
      currency: parseRef(fields.currency || ""),
      tax_basis: fields.tax_basis || "unknown",
      precedence: fields.precedence || "require_unique",
    };
    for (const name of ["price_list", "valid_from", "valid_until"])
      if (fields[name])
        (mapping.prices as Record<string, unknown>)[name] = parseRef(
          fields[name],
        );
  } else {
    const transactions =
      header && !fields.transactions
        ? key.map((column) => ({ object_id: object.object_id, column }))
        : split(fields.transactions || "").map(parseRef);
    const sales = {
      ...fact,
      transaction_columns: transactions,
      transaction_identity:
        fields.transaction_identity || "enforced_header_key",
      grain: header ? "transaction_events" : "product_events",
      quantity: header ? null : parseRef(fields.quantity || ""),
      occurred_at: parseRef(fields.occurred_at || ""),
      timestamp_basis: fields.timestamp_basis,
      status: parseRef(fields.status || ""),
      completed_statuses: split(fields.completed_statuses || ""),
      excluded_statuses: split(fields.excluded_statuses || ""),
      returns_semantics: fields.returns_semantics || "unsupported",
    };
    mapping.sales = sales;
    if (family === "financial")
      mapping.revenue = {
        gross_amount: parseRef(fields.gross_amount || ""),
        net_rule: fields.net_rule || "source_net",
        net_amount:
          fields.net_rule === "gross_minus_discount"
            ? null
            : parseRef(fields.net_amount || ""),
        discount_amount:
          fields.net_rule === "gross_minus_discount"
            ? parseRef(fields.discount_amount || "")
            : null,
        currency: parseRef(fields.currency || ""),
        net_basis: fields.net_basis,
        tax_basis: fields.tax_basis,
      };
  }
  return mapping;
}

function CapabilityEditor({
  businessId,
  source,
  workspace,
  state,
  onChanged,
}: {
  businessId: string;
  source: DataSource;
  workspace: CapabilityWorkspace;
  state: CapabilityState;
  onChanged: () => Promise<void>;
}) {
  const initialReport = state.latest_validation ?? state.report;
  const [fields, setFields] = useState<DraftFields>({});
  const [meaning, setMeaning] = useState(initialReport?.meaning ?? "");
  const [advanced, setAdvanced] = useState(
    initialReport ? JSON.stringify(initialReport.mapping, null, 2) : "",
  );
  const [useAdvanced, setUseAdvanced] = useState(!!initialReport);
  const [report, setReport] = useState<CapabilityQualityReport | null>(
    initialReport,
  );
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [historical, setHistorical] = useState(false);
  const id = useId();
  const discovery = workspace.discovery!;
  const root = discovery.objects.find((o) => o.object_id === fields.object);
  const kind = state.capability;
  const change = (name: string, value: string) => {
    setFields((old) => ({ ...old, [name]: value }));
    setUseAdvanced(false);
    setConfirmed(false);
    setReport(null);
  };
  const field = (
    name: string,
    label: string,
    kinds?: string[],
    optional = false,
  ) => (
    <label key={name} htmlFor={`${id}-${name}`}>
      {label}
      {optional ? " (optional)" : ""}
      <select
        id={`${id}-${name}`}
        value={fields[name] ?? ""}
        onChange={(event) => change(name, event.target.value)}
      >
        <option value="">Choose source field</option>
        {discovery.objects.flatMap((o) =>
          o.columns
            .filter((c) => !kinds || kinds.includes(c.kind))
            .map((c) => (
              <option
                key={`${o.object_id}/${c.name}`}
                value={`${o.object_id}/${c.name}`}
              >
                {o.name}.{c.name}
              </option>
            )),
        )}
      </select>
    </label>
  );
  const choice = (name: string, label: string, options: [string, string][]) => (
    <label key={name} htmlFor={`${id}-${name}`}>
      {label}
      <select
        id={`${id}-${name}`}
        value={fields[name] ?? ""}
        onChange={(event) => change(name, event.target.value)}
      >
        <option value="">Choose reviewed meaning</option>
        {options.map(([value, text]) => (
          <option key={value} value={value}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
  const text = (name: string, label: string) => (
    <label key={name} htmlFor={`${id}-${name}`}>
      {label}
      <input
        id={`${id}-${name}`}
        value={fields[name] ?? ""}
        onChange={(event) => change(name, event.target.value)}
        maxLength={512}
      />
    </label>
  );
  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (caught) {
      setConfirmed(false);
      setError(
        caught instanceof ApiError
          ? (errorLabels[caught.code] ?? caught.message)
          : caught instanceof Error
            ? caught.message
            : "Capability review could not finish.",
      );
    } finally {
      setBusy(false);
    }
  }
  const fresh = report && new Date(report.expires_at).getTime() > Date.now();
  return (
    <section
      className="capability-editor"
      aria-label={`${labels[kind]} review`}
    >
      <h4>{labels[kind]} mapping and quality</h4>
      <ul>
        {state.prerequisites.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      {error && <Alert>{error}</Alert>}
      {notice && <p role="status">{notice}</p>}
      <fieldset disabled={busy}>
        <legend>Business meaning and scope</legend>
        <label htmlFor={`${id}-meaning`}>
          What does this capability measure?
          <textarea
            id={`${id}-meaning`}
            value={meaning}
            maxLength={2000}
            onChange={(event) => {
              setMeaning(event.target.value);
              setReport(null);
              setConfirmed(false);
            }}
            placeholder="Describe the authoritative source, units, scope and inclusion rules."
          />
        </label>
      </fieldset>
      <details>
        <summary>Advanced: map source fields</summary>
        <fieldset className="capability-fields" disabled={busy}>
          <label htmlFor={`${id}-object`}>
            Authoritative fact source
            <select
              id={`${id}-object`}
              value={fields.object ?? ""}
              onChange={(event) => {
                setFields({ object: event.target.value });
                setUseAdvanced(false);
                setReport(null);
                setConfirmed(false);
              }}
            >
              <option value="">Choose an approved source object</option>
              {discovery.objects.map((object) => (
                <option key={object.object_id} value={object.object_id}>
                  {object.schema_name}.{object.name}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor={`${id}-key`}>
            Complete fact identity
            <select
              id={`${id}-key`}
              value={fields.key ?? "0"}
              onChange={(event) => change("key", event.target.value)}
            >
              {root?.unique_keys.map((key, index) => (
                <option value={index} key={key.join("/")}>
                  {key.join(" + ")}
                </option>
              ))}
            </select>
          </label>
          {fields.grain !== "transaction_events" &&
            workspace.identity_mapping?.key_columns.map((column, index) => (
              <label key={column} htmlFor={`${id}-product-${index}`}>
                Product identity: {column}
                <select
                  id={`${id}-product-${index}`}
                  value={fields[`product_${index}`] ?? ""}
                  onChange={(event) =>
                    change(`product_${index}`, event.target.value)
                  }
                >
                  <option value="">Choose the matching fact column</option>
                  {root?.columns.map((c) => (
                    <option key={c.name}>{c.name}</option>
                  ))}
                </select>
              </label>
            ))}
          {fields.grain !== "transaction_events" &&
            choice("productReference", "Product association", [
              [
                "catalogue_identity",
                "Verify catalogue identity (names not required)",
              ],
              [
                "fact_identity",
                "Independent original fact identity (owner verified)",
              ],
            ])}
          {fields.grain !== "transaction_events" &&
            field("unit", "Quantity / selling unit", ["text"])}
          {text("timezone", "Source timezone (IANA, for example Asia/Beirut)")}
          {field("location", "Location", ["text", "integer"], true)}
          {field("branch", "Branch", ["text", "integer"], true)}
          {field("warehouse", "Warehouse", ["text", "integer"], true)}
          {(kind === "sales" || kind === "financial") &&
            choice("grain", "Event grain", [
              ["product_events", "Product sales lines"],
              [
                "transaction_events",
                "Complete transaction/header totals; no product association",
              ],
            ])}
          {fields.grain === "transaction_events" &&
            (workspace.identity_mapping?.row_scopes ?? []).map((scope, index) =>
              field(
                `scope_${index}`,
                `Approved business partition for ${scope.column}`,
                ["text", "integer"],
              ),
            )}
          {kind === "inventory" && (
            <>
              {field("on_hand", "Authoritative current balance", [
                "decimal",
                "integer",
              ])}
              {choice("availability_rule", "Availability meaning", [
                ["unsupported", "On-hand only; availability unknown"],
                ["source", "Authoritative available balance"],
                [
                  "subtract_complete_reservations",
                  "On-hand minus complete reservations",
                ],
              ])}
              {field(
                "available",
                "Authoritative available balance",
                ["decimal", "integer"],
                true,
              )}
              {field(
                "reserved",
                "Reserved quantity",
                ["decimal", "integer"],
                true,
              )}
              {choice("reservations_complete", "Reservation evidence", [
                ["yes", "Complete reservations; not already netted"],
                ["no", "Not established"],
              ])}
            </>
          )}
          {kind === "pricing" && (
            <>
              {field("amount", "Current selling amount", [
                "decimal",
                "integer",
              ])}
              {field("currency", "Currency code", ["text"])}
              {field("price_list", "Price list", ["text"], true)}
              {field("valid_from", "Effective from", ["date"], true)}
              {field("valid_until", "Effective until", ["date"], true)}
              {choice("precedence", "Variant precedence", [
                ["require_unique", "One applicable price per scope"],
                ["latest_effective", "Latest effective price per scope"],
              ])}
            </>
          )}
          {(kind === "sales" || kind === "financial") && (
            <>
              {fields.grain !== "transaction_events" &&
                field("quantity", "Completed-event quantity", [
                  "decimal",
                  "integer",
                ])}
              {field("occurred_at", "Sales event time", ["date"])}
              {choice("timestamp_basis", "Timestamp basis", [
                ["utc", "Stored as UTC"],
                ["source_local", "Local wall-clock in source timezone"],
                [
                  "business_date",
                  "Business dates; complete local calendar days only",
                ],
              ])}
              {field("status", "Transaction status", ["text"])}
              {text(
                "completed_statuses",
                "Completed status values (comma separated)",
              )}
              {text(
                "excluded_statuses",
                "Explicitly excluded status values (comma separated)",
              )}
              {text(
                "transactions",
                "Transaction identity refs (for example o2/Id, o2/Branch)",
              )}
              {choice("transaction_identity", "Transaction identity evidence", [
                [
                  "enforced_header_key",
                  "Complete enforced transaction/header key",
                ],
                [
                  "reviewed_denormalized_key",
                  "Owner-verified repeated transaction key at line grain",
                ],
              ])}
              {choice("returns_semantics", "Returns coverage", [
                ["no_returns", "Verified no returns for this contract"],
                [
                  "separate_complete",
                  "Complete separate refund ledger (map in advanced review)",
                ],
                ["unsupported", "Returns unknown; gross sales only"],
              ])}
            </>
          )}
          {kind === "financial" && (
            <>
              {field("gross_amount", "Gross event amount", [
                "decimal",
                "integer",
              ])}
              {choice("net_rule", "Net amount calculation", [
                ["source_net", "Authoritative net amount including discounts"],
                [
                  "gross_minus_discount",
                  "Gross minus authoritative discount amount",
                ],
              ])}
              {fields.net_rule === "gross_minus_discount"
                ? field(
                    "discount_amount",
                    "Discount amount at the same event grain",
                    ["decimal", "integer"],
                  )
                : field("net_amount", "Net event amount including discounts", [
                    "decimal",
                    "integer",
                  ])}
              {field("currency", "Currency code", ["text"])}
              {choice("net_basis", "Refund recognition", [
                [
                  "before_refunds",
                  "Net amount before separately mapped refunds",
                ],
                [
                  "already_net_of_refunds",
                  "Net source amount already includes refunds",
                ],
              ])}
            </>
          )}
          {(kind === "pricing" || kind === "financial") &&
            choice("tax_basis", "Tax basis", [
              ["included", "Amounts include tax"],
              ["excluded", "Amounts exclude tax"],
              ...(kind === "pricing"
                ? [["unknown", "Tax treatment unknown"] as [string, string]]
                : []),
            ])}
        </fieldset>
      </details>
      <details>
        <summary>Advanced mapping, joins and refund ledger</summary>
        <p>
          Complete verified foreign-key joins are included by the guided form.
          Review complex joins, partitions, sign rules, low-stock thresholds and
          complete refund definitions here. Arbitrary SQL is not accepted.
        </p>
        <button
          className="btn-secondary"
          disabled={busy}
          onClick={() => {
            try {
              setAdvanced(
                JSON.stringify(buildMapping(workspace, kind, fields), null, 2),
              );
              setUseAdvanced(true);
            } catch (caught) {
              setError(
                caught instanceof Error
                  ? caught.message
                  : "Complete the source fields first.",
              );
            }
          }}
        >
          Prepare advanced mapping
        </button>
        <label htmlFor={`${id}-advanced`}>
          Typed mapping contract
          <textarea
            id={`${id}-advanced`}
            value={advanced}
            onChange={(event) => {
              setAdvanced(event.target.value);
              setUseAdvanced(true);
              setReport(null);
              setConfirmed(false);
            }}
            spellCheck={false}
          />
        </label>
      </details>
      <button
        disabled={busy || meaning.trim().length < 12}
        onClick={() =>
          void run(async () => {
            const mapping = useAdvanced
              ? (JSON.parse(advanced) as Record<string, unknown>)
              : buildMapping(workspace, kind, fields);
            const value = await api.validateCapability(
              businessId,
              source.id,
              kind,
              { mapping, discovery, meaning },
            );
            setReport(value);
            setHistorical(false);
            setAdvanced(JSON.stringify(value.mapping, null, 2));
            setUseAdvanced(true);
            setConfirmed(false);
          })
        }
      >
        <BusyLabel
          busy={busy}
          idle={`Validate ${labels[kind].toLowerCase()}`}
        />
      </button>
      {report && (
        <div className="capability-quality" aria-live="polite">
          <h4>Quality and coverage</h4>
          <dl className="quality-metrics">
            <div>
              <dt>Relevant records</dt>
              <dd>{report.total_records?.toLocaleString() ?? "Incomplete"}</dd>
            </div>
            <div>
              <dt>Valid</dt>
              <dd>{report.valid_records?.toLocaleString() ?? "Unknown"}</dd>
            </div>
            <div>
              <dt>Invalid</dt>
              <dd>{report.invalid_records?.toLocaleString() ?? "Unknown"}</dd>
            </div>
            <div>
              <dt>Invalid percentage</dt>
              <dd>{report.invalid_percent ?? "Unknown"}%</dd>
            </div>
          </dl>
          <p>
            {statusLabel(report.impact)}. Tolerance:{" "}
            {report.max_invalid_percent}%; operational completeness requires
            zero invalid facts. No transactions or balances are dropped.
          </p>
          <p>{report.denominator}</p>
          <ul>
            {report.findings.map((finding) => (
              <li key={finding}>{finding}</li>
            ))}
          </ul>
          {!fresh && (
            <Alert>This report expired. Revalidate before approval.</Alert>
          )}
          {!!report.diagnostics.length && (
            <div className="quality-table-wrap">
              <table>
                <caption>Invalid operational facts (bounded)</caption>
                <thead>
                  <tr>
                    <th scope="col">Original identity</th>
                    <th scope="col">Reason</th>
                    <th scope="col">Exclusion</th>
                  </tr>
                </thead>
                <tbody>
                  {report.diagnostics.map((row, index) => (
                    <tr key={index}>
                      <td>
                        {row.identifier
                          .map((v) => v ?? "Unavailable")
                          .join(" / ")}
                      </td>
                      <td>
                        {row.explanation}
                        <small>{row.locator_note}</small>
                      </td>
                      <td>Blocked</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {report.diagnostics_truncated && (
                <p>Additional invalid facts are counted but not displayed.</p>
              )}
            </div>
          )}
          {report.eligible && fresh && !historical && (
            <>
              <label className="quality-confirmation">
                <input
                  type="checkbox"
                  checked={confirmed}
                  disabled={busy}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                I confirm the business meaning, complete scope and{" "}
                {report.total_records} validated facts with zero exclusions.
                Enable only {labels[kind].toLowerCase()}.
              </label>
              <button
                disabled={busy || !confirmed}
                onClick={() =>
                  void run(async () => {
                    await api.approveCapability(
                      businessId,
                      source.id,
                      kind,
                      report,
                    );
                    setConfirmed(false);
                    setNotice(`${labels[kind]} approved and enabled.`);
                    await onChanged();
                  })
                }
              >
                Approve and enable {labels[kind].toLowerCase()}
              </button>
            </>
          )}
        </div>
      )}
      {!!state.history?.length && (
        <details>
          <summary>Approval history</summary>
          <ol>
            {state.history.map((event) => (
              <li key={event.version}>
                Version {event.version}: {event.approval_type};{" "}
                {new Date(event.approved_at).toLocaleString()}.{" "}
                <button
                  className="btn-secondary"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      const value = await api.capabilityQuality(
                        businessId,
                        source.id,
                        kind,
                        event.report_id,
                      );
                      setReport(value);
                      setMeaning(value.meaning);
                      setAdvanced(JSON.stringify(value.mapping, null, 2));
                      setUseAdvanced(true);
                      setHistorical(true);
                      setConfirmed(false);
                      setNotice(
                        "Historical validation is read-only. Revalidate this mapping to request a fresh approval.",
                      );
                    })
                  }
                >
                  View validation for version {event.version}
                </button>
              </li>
            ))}
          </ol>
        </details>
      )}
      {state.approval_status === "APPROVED" && (
        <button
          className="btn-secondary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              await api.revokeCapability(businessId, source.id, kind);
              setConfirmed(false);
              setNotice(
                `${labels[kind]} approval revoked. Other capabilities are unchanged.`,
              );
              await onChanged();
            })
          }
        >
          Revoke {labels[kind].toLowerCase()} approval
        </button>
      )}
    </section>
  );
}
