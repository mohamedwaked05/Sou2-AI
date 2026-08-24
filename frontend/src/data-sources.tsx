import {
  AlertCircle,
  Check,
  CircleDot,
  Database,
  Link2Off,
  PlugZap,
  RefreshCw,
  ShieldCheck,
  Warehouse,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";
import {
  api,
  ApiError,
  Business,
  ConnectionProfile,
  DataSource,
  DataSourceStatus,
} from "./api";
import { Alert, BusyLabel, PageHeading, Skeleton } from "./ui";

const capabilityLabels: Record<string, string> = {
  products: "Products",
  inventory: "Current inventory",
  sales_summaries: "Sales summaries",
  best_sellers: "Best sellers",
  restocking_recommendations: "Restocking recommendations",
};

const statusLabels: Record<DataSourceStatus, string> = {
  CONFIGURED: "Configured",
  VALIDATED: "Validated",
  ACTIVE: "Active",
  UNHEALTHY: "Needs attention",
  DISABLED: "Disabled",
};

function safeErrorMessage(error: unknown) {
  if (!(error instanceof ApiError)) {
    return "We couldn't complete that request. Try again.";
  }
  const messages: Record<string, string> = {
    operational_source_unavailable:
      "The source could not be reached. Check the local service and try again.",
    operational_mapping_invalid:
      "The source does not match its approved mapping profile.",
    data_source_state_conflict:
      "The source changed or is not ready for that action. Refresh and try again.",
    active_data_source_conflict:
      "Another PostgreSQL operational source is already active for this business.",
  };
  return messages[error.code] ?? error.message;
}

function formatTimestamp(value: string | null) {
  if (!value) return "Not yet";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function DataSourceBadge({ status }: { status: DataSourceStatus }) {
  return (
    <span className={`source-status source-status-${status.toLowerCase()}`}>
      <CircleDot aria-hidden="true" size={13} />
      {statusLabels[status]}
    </span>
  );
}

function MappingSummary({ profile }: { profile: ConnectionProfile }) {
  const { mapping } = profile;
  return (
    <div className="mapping-preview">
      <div>
        <span>Mapping</span>
        <strong>
          {mapping.display_name} · v{mapping.version}
        </strong>
      </div>
      <div>
        <span>Source rules</span>
        <strong>
          {mapping.currency} · {mapping.source_timezone}
        </strong>
      </div>
      <p>
        Completed and returned sales are finalized; pending and cancelled sales are
        excluded. Active, unexpired reservations reduce available stock.
      </p>
    </div>
  );
}

function ConnectDialog({
  profile,
  busy,
  onClose,
  onSubmit,
}: {
  profile: ConnectionProfile;
  busy: boolean;
  onClose: () => void;
  onSubmit: (displayName: string) => Promise<void>;
}) {
  const titleId = useId();
  const nameId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const [displayName, setDisplayName] = useState("Lebanese Minimarket Demo");

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement as HTMLElement | null;
    document.body.style.overflow = "hidden";
    cancel.current?.focus();
    function handleKeys(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) onClose();
      if (event.key !== "Tab") return;
      const focusable = dialog.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", handleKeys);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKeys);
      previousFocus?.focus();
    };
  }, [busy, onClose]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    await onSubmit(displayName);
  }

  return (
    <div className="dialog-layer" role="presentation">
      <button
        type="button"
        className="dialog-scrim"
        aria-label="Cancel connection setup"
        disabled={busy}
        onClick={onClose}
      />
      <div
        ref={dialog}
        className="dialog source-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <span className="dialog-source-icon">
          <Database aria-hidden="true" />
        </span>
        <h2 id={titleId}>Connect PostgreSQL demo store</h2>
        <p>
          Sou2AI will use a deployment-managed, read-only profile. Credentials and
          arbitrary database addresses are never entered here.
        </p>
        <form onSubmit={(event) => void submit(event)}>
          <label className="field-label" htmlFor={nameId}>
            Display name
            <input
              id={nameId}
              className="input"
              name="display_name"
              autoComplete="off"
              minLength={2}
              maxLength={120}
              required
              disabled={busy}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </label>
          <label className="field-label">
            Connection profile
            <select className="input" value={profile.key} disabled>
              <option value={profile.key}>{profile.display_name}</option>
            </select>
          </label>
          <MappingSummary profile={profile} />
          <div className="form-actions">
            <button
              ref={cancel}
              type="button"
              className="btn-secondary"
              disabled={busy}
              onClick={onClose}
            >
              Cancel
            </button>
            <button type="submit" className="btn" disabled={busy}>
              <BusyLabel busy={busy} idle="Save configuration" />
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function DisableDialog({
  source,
  busy,
  onClose,
  onConfirm,
}: {
  source: DataSource;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}) {
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement as HTMLElement | null;
    document.body.style.overflow = "hidden";
    cancel.current?.focus();
    function handleKeys(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) onClose();
      if (event.key !== "Tab") return;
      const buttons = dialog.current?.querySelectorAll<HTMLButtonElement>(
        "button:not([disabled])",
      );
      if (!buttons?.length) return;
      const first = buttons[0];
      const last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", handleKeys);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKeys);
      previousFocus?.focus();
    };
  }, [busy, onClose]);

  return (
    <div className="dialog-layer" role="presentation">
      <button
        type="button"
        className="dialog-scrim"
        aria-label="Cancel disconnect"
        disabled={busy}
        onClick={onClose}
      />
      <div
        ref={dialog}
        className="dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <span className="dialog-danger">
          <Link2Off aria-hidden="true" />
        </span>
        <h2 id={titleId}>Disable this data source?</h2>
        <p>
          <strong>{source.display_name}</strong> will stop being available to Sou2AI.
          Its safe configuration remains so you can validate it again later.
        </p>
        <div className="form-actions">
          <button
            ref={cancel}
            type="button"
            className="btn-secondary"
            disabled={busy}
            onClick={onClose}
          >
            Keep connected
          </button>
          <button
            type="button"
            className="btn-danger"
            disabled={busy}
            onClick={() => void onConfirm()}
          >
            <BusyLabel busy={busy} idle="Disable source" />
          </button>
        </div>
      </div>
    </div>
  );
}

function SourceCard({
  source,
  busyAction,
  onAction,
  onDisable,
}: {
  source: DataSource;
  busyAction: string;
  onAction: (
    source: DataSource,
    action: "validate" | "activate" | "health",
  ) => Promise<void>;
  onDisable: (source: DataSource) => void;
}) {
  const busy = busyAction.startsWith(`${source.id}:`);
  const mapping = source.mapping;
  return (
    <article className={`source-card source-card-${source.status.toLowerCase()}`}>
      <header className="source-card-header">
        <div className="source-identity">
          <span>
            <Database aria-hidden="true" />
          </span>
          <div>
            <h2>{source.display_name}</h2>
            <p>PostgreSQL · deployment-managed read-only profile</p>
          </div>
        </div>
        <DataSourceBadge status={source.status} />
      </header>

      {source.status === "UNHEALTHY" ? (
        <div className="source-warning" role="status">
          <AlertCircle aria-hidden="true" />
          <div>
            <strong>Connection needs attention</strong>
            <p>
              The source is unavailable or no longer matches its approved mapping. No
              private connection details were exposed.
            </p>
          </div>
        </div>
      ) : null}

      <dl className="source-metadata">
        <div>
          <dt>Last validation</dt>
          <dd>{formatTimestamp(source.last_validated_at)}</dd>
        </div>
        <div>
          <dt>Last successful check</dt>
          <dd>{formatTimestamp(source.last_successful_health_check_at)}</dd>
        </div>
        <div>
          <dt>Mapping profile</dt>
          <dd>
            {mapping.display_name} · v{mapping.version}
          </dd>
        </div>
        <div>
          <dt>Source rules</dt>
          <dd>
            {mapping.currency} · {mapping.source_timezone}
          </dd>
        </div>
      </dl>

      <div className="source-semantics">
        <div>
          <ShieldCheck aria-hidden="true" />
          <p>
            Completed and returned sales are finalized; pending and cancelled sales are
            excluded. Refunds reduce quantity and revenue.
          </p>
        </div>
        <div>
          <Warehouse aria-hidden="true" />
          <p>
            Branches are sales locations. Warehouses hold stock. Active, unexpired
            reservations reduce available quantity.
          </p>
        </div>
      </div>

      <section className="source-capabilities" aria-labelledby={`caps-${source.id}`}>
        <h3 id={`caps-${source.id}`}>Available capabilities</h3>
        <ul>
          {source.capabilities.map((capability) => (
            <li key={capability}>
              <Check aria-hidden="true" size={14} />
              {capabilityLabels[capability] ?? capability}
            </li>
          ))}
        </ul>
      </section>

      <footer className="source-actions">
        {source.status === "CONFIGURED" ||
        source.status === "UNHEALTHY" ||
        source.status === "DISABLED" ? (
          <button
            type="button"
            className="btn-secondary"
            disabled={busy}
            onClick={() => void onAction(source, "validate")}
          >
            <BusyLabel
              busy={busyAction === `${source.id}:validate`}
              idle={source.status === "DISABLED" ? "Validate again" : "Validate"}
            />
          </button>
        ) : null}
        {source.status === "VALIDATED" ? (
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void onAction(source, "activate")}
          >
            <BusyLabel busy={busyAction === `${source.id}:activate`} idle="Activate" />
          </button>
        ) : null}
        {source.status === "VALIDATED" ||
        source.status === "ACTIVE" ||
        source.status === "UNHEALTHY" ? (
          <button
            type="button"
            className="btn-secondary"
            disabled={busy}
            onClick={() => void onAction(source, "health")}
          >
            <RefreshCw aria-hidden="true" size={16} />
            <BusyLabel
              busy={busyAction === `${source.id}:health`}
              idle="Test connection"
            />
          </button>
        ) : null}
        {source.status !== "DISABLED" ? (
          <button
            type="button"
            className="btn-quiet-danger"
            disabled={busy}
            onClick={() => onDisable(source)}
          >
            <Link2Off aria-hidden="true" size={16} />
            Disable
          </button>
        ) : null}
      </footer>
    </article>
  );
}

export function DataSourcesPage({ business }: { business: Business }) {
  const [sources, setSources] = useState<DataSource[] | null>(null);
  const [profiles, setProfiles] = useState<ConnectionProfile[]>([]);
  const [showConnect, setShowConnect] = useState(false);
  const [disableTarget, setDisableTarget] = useState<DataSource | null>(null);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const [configured, available] = await Promise.all([
        api.dataSources(business.id),
        api.dataSourceProfiles(business.id),
      ]);
      setSources(configured);
      setProfiles(available);
    } catch (caught) {
      setSources([]);
      setProfiles([]);
      setError(safeErrorMessage(caught));
    }
  }, [business.id]);

  useEffect(() => {
    setSources(null);
    setShowConnect(false);
    setDisableTarget(null);
    setSuccess("");
    void load();
  }, [load]);

  async function create(displayName: string) {
    const profile = profiles[0];
    if (!profile) return;
    setBusyAction("create");
    setError("");
    setSuccess("");
    try {
      const created = await api.createDataSource(business.id, {
        display_name: displayName,
        connection_profile_key: profile.key,
        mapping_profile_key: profile.mapping.key,
        mapping_profile_version: profile.mapping.version,
      });
      setSources((current) => [...(current ?? []), created]);
      setShowConnect(false);
      setSuccess("Data source configured. Validate it before activation.");
    } catch (caught) {
      setError(safeErrorMessage(caught));
    } finally {
      setBusyAction("");
    }
  }

  async function act(source: DataSource, action: "validate" | "activate" | "health") {
    setBusyAction(`${source.id}:${action}`);
    setError("");
    setSuccess("");
    try {
      const handlers = {
        validate: api.validateDataSource,
        activate: api.activateDataSource,
        health: api.checkDataSource,
      };
      const updated = await handlers[action](business.id, source.id);
      setSources(
        (current) =>
          current?.map((item) => (item.id === updated.id ? updated : item)) ?? [
            updated,
          ],
      );
      const messages = {
        validate:
          updated.status === "UNHEALTHY"
            ? "Validation finished, but the source needs attention."
            : "Connection and mapping validated.",
        activate: "Data source activated.",
        health:
          updated.status === "UNHEALTHY"
            ? "Connection test failed safely."
            : "Connection test succeeded.",
      };
      setSuccess(messages[action]);
    } catch (caught) {
      setError(safeErrorMessage(caught));
    } finally {
      setBusyAction("");
    }
  }

  async function disable() {
    if (!disableTarget) return;
    setBusyAction(`${disableTarget.id}:disable`);
    setError("");
    setSuccess("");
    try {
      const updated = await api.disableDataSource(business.id, disableTarget.id);
      setSources(
        (current) =>
          current?.map((item) => (item.id === updated.id ? updated : item)) ?? [
            updated,
          ],
      );
      setDisableTarget(null);
      setSuccess("Data source disabled.");
    } catch (caught) {
      setError(safeErrorMessage(caught));
    } finally {
      setBusyAction("");
    }
  }

  const availableProfile = profiles[0];
  return (
    <>
      <PageHeading
        title="Data Sources"
        description="Manage tenant-scoped, read-only operational connections for this business. Live records stay in the source system."
        action={
          availableProfile ? (
            <button type="button" className="btn" onClick={() => setShowConnect(true)}>
              <PlugZap aria-hidden="true" size={18} />
              Connect source
            </button>
          ) : undefined
        }
      />

      {error ? <Alert>{error}</Alert> : null}
      {success ? <Alert tone="success">{success}</Alert> : null}

      {sources === null ? (
        <section className="source-loading" aria-label="Loading data sources">
          <Skeleton className="h-24" />
          <Skeleton className="h-64" />
        </section>
      ) : sources.length === 0 ? (
        <section className="source-empty">
          <div className="source-empty-icon">
            <Database aria-hidden="true" />
          </div>
          <p className="eyebrow">Operational integrations</p>
          <h2>Connect trusted live business data</h2>
          <p>
            Products, inventory, sales summaries, best sellers, and restocking
            recommendations can be read from an approved source without copying its
            operational records into Sou2AI.
          </p>
          {availableProfile ? (
            <article className="supported-source-card">
              <div>
                <span>
                  <Database aria-hidden="true" />
                </span>
                <div>
                  <h3>{availableProfile.display_name}</h3>
                  <p>{availableProfile.description}</p>
                </div>
              </div>
              <ul aria-label="Connection safeguards">
                <li>
                  <ShieldCheck aria-hidden="true" /> Read-only database role
                </li>
                <li>
                  <Check aria-hidden="true" /> Approved semantic mapping
                </li>
              </ul>
              <button
                type="button"
                className="btn"
                onClick={() => setShowConnect(true)}
              >
                Connect demo source
              </button>
            </article>
          ) : (
            <Alert tone="info">
              No deployment-managed connection profile is available for this
              environment.
            </Alert>
          )}
        </section>
      ) : (
        <div className="source-list">
          {sources.map((source) => (
            <SourceCard
              key={source.id}
              source={source}
              busyAction={busyAction}
              onAction={act}
              onDisable={setDisableTarget}
            />
          ))}
        </div>
      )}

      {showConnect && availableProfile ? (
        <ConnectDialog
          profile={availableProfile}
          busy={busyAction === "create"}
          onClose={() => setShowConnect(false)}
          onSubmit={create}
        />
      ) : null}
      {disableTarget ? (
        <DisableDialog
          source={disableTarget}
          busy={busyAction === `${disableTarget.id}:disable`}
          onClose={() => setDisableTarget(null)}
          onConfirm={disable}
        />
      ) : null}
    </>
  );
}
