import { Eye, EyeOff, LoaderCircle, Moon, Sun } from "lucide-react";
import { ReactNode, useId, useState } from "react";
import type { BusinessStatus } from "./api";

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <span
      className={`brand-logo ${compact ? "brand-logo-compact" : ""}`}
      translate="no"
    >
      <img src="/sou2ai-logo.png" alt="" width="40" height="40" fetchPriority="high" />
      {!compact && <strong>Sou2AI</strong>}
    </span>
  );
}

export function ThemeButton({
  dark,
  onChange,
}: {
  dark: boolean;
  onChange: () => void;
}) {
  return (
    <button
      type="button"
      className="icon-button"
      aria-label={`Use ${dark ? "light" : "dark"} theme`}
      onClick={onChange}
    >
      {dark ? (
        <Sun aria-hidden="true" size={19} />
      ) : (
        <Moon aria-hidden="true" size={19} />
      )}
    </button>
  );
}

export function PasswordField({
  label,
  name,
  value,
  onChange,
  autoComplete,
}: {
  label: string;
  name: string;
  value: string;
  onChange: (value: string) => void;
  autoComplete: string;
}) {
  const [visible, setVisible] = useState(false);
  const id = useId();
  return (
    <label className="field-label" htmlFor={id}>
      {label}
      <span className="input-shell">
        <input
          id={id}
          required
          name={name}
          type={visible ? "text" : "password"}
          autoComplete={autoComplete}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="input pr-12"
        />
        <button
          type="button"
          className="input-action"
          aria-label={`${visible ? "Hide" : "Show"} ${label.toLowerCase()}`}
          aria-pressed={visible}
          onClick={() => setVisible((current) => !current)}
        >
          {visible ? <EyeOff size={18} /> : <Eye size={18} />}
        </button>
      </span>
    </label>
  );
}

export function BusyLabel({ busy, idle }: { busy: boolean; idle: string }) {
  return busy ? (
    <>
      <LoaderCircle aria-hidden="true" className="animate-spin" size={18} />
      <span>Working…</span>
    </>
  ) : (
    <span>{idle}</span>
  );
}

export function Alert({
  children,
  tone = "error",
}: {
  children: ReactNode;
  tone?: "error" | "success" | "info";
}) {
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`alert alert-${tone}`}>
      {children}
    </div>
  );
}

export function StatusBadge({ status }: { status: BusinessStatus }) {
  return (
    <span className={`status status-${status.toLowerCase()}`}>
      {status.toLowerCase()}
    </span>
  );
}

export function PageHeading({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </header>
  );
}

export function Skeleton({ className = "h-24" }: { className?: string }) {
  return <div aria-hidden="true" className={`skeleton ${className}`} />;
}
