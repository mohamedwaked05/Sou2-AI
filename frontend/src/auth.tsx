import { ArrowLeft, ArrowRight, CheckCircle2, Mail } from "lucide-react";
import { FormEvent, ReactNode, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, User } from "./api";
import { Alert, BusyLabel, Logo, PasswordField, ThemeButton } from "./ui";

function messageFor(error: unknown) {
  return error instanceof ApiError
    ? error.message
    : "We couldn't reach Sou2AI. Check your connection and try again.";
}

function AuthFrame({
  children,
  dark,
  setDark,
  wide = false,
}: {
  children: ReactNode;
  dark: boolean;
  setDark: (value: boolean) => void;
  wide?: boolean;
}) {
  return (
    <main className="auth-page">
      <a className="skip-link" href="#auth-content">
        Skip to content
      </a>
      <div className="auth-wave" aria-hidden="true" />
      <ThemeButton dark={dark} onChange={() => setDark(!dark)} />
      <section
        id="auth-content"
        className={`auth-card ${wide ? "max-w-xl" : "max-w-md"}`}
      >
        <div className="flex justify-center">
          <Logo />
        </div>
        {children}
      </section>
    </main>
  );
}

function AuthTabs({ active }: { active: "login" | "register" }) {
  return (
    <nav className="auth-tabs" aria-label="Authentication">
      <Link aria-current={active === "login" ? "page" : undefined} to="/login">
        Sign in
      </Link>
      <Link aria-current={active === "register" ? "page" : undefined} to="/register">
        Sign up
      </Link>
    </nav>
  );
}

export function LoginPage({
  dark,
  setDark,
  onAuthenticated,
}: {
  dark: boolean;
  setDark: (value: boolean) => void;
  onAuthenticated: (user: User) => void;
}) {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await api.login({ email, password, keep_me_signed_in: remember });
      const user = await api.me();
      onAuthenticated(user);
      navigate("/businesses", { replace: true });
    } catch (caught) {
      setPassword("");
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthFrame dark={dark} setDark={setDark}>
      <AuthTabs active="login" />
      <div className="auth-heading">
        <h1>Welcome back</h1>
        <p>Sign in to manage your business with Sou2AI.</p>
      </div>
      <form onSubmit={submit} className="form-stack">
        <label className="field-label">
          Email address
          <input
            required
            name="email"
            type="email"
            autoComplete="email"
            spellCheck={false}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="input"
          />
        </label>
        <PasswordField
          label="Password"
          name="password"
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
        />
        <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
          <label className="check-label">
            <input
              type="checkbox"
              name="remember"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
            />
            Keep me signed in
          </label>
          <Link className="text-link" to="/forgot-password">
            Forgot password?
          </Link>
        </div>
        {error && <Alert>{error}</Alert>}
        <button disabled={busy} className="btn w-full" type="submit">
          <BusyLabel busy={busy} idle="Sign in" />
          {!busy && <ArrowRight aria-hidden="true" size={18} />}
        </button>
      </form>
      <p className="auth-footer">
        Don&apos;t have an account? <Link to="/register">Create account</Link>
      </p>
    </AuthFrame>
  );
}

export function RegisterPage({
  dark,
  setDark,
}: {
  dark: boolean;
  setDark: (value: boolean) => void;
}) {
  const [fields, setFields] = useState({
    first_name: "",
    last_name: "",
    email: "",
    password: "",
    password_confirmation: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [registeredEmail, setRegisteredEmail] = useState("");
  const update = (key: keyof typeof fields, value: string) =>
    setFields((current) => ({ ...current, [key]: value }));

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setError("");
    if (fields.password !== fields.password_confirmation) {
      setError("Password confirmation does not match.");
      return;
    }
    setBusy(true);
    try {
      await api.register(fields);
      setRegisteredEmail(fields.email);
      setFields((current) => ({ ...current, password: "", password_confirmation: "" }));
    } catch (caught) {
      setFields((current) => ({ ...current, password: "", password_confirmation: "" }));
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }

  if (registeredEmail) {
    return <CheckEmailPage dark={dark} setDark={setDark} email={registeredEmail} />;
  }

  return (
    <AuthFrame dark={dark} setDark={setDark} wide>
      <AuthTabs active="register" />
      <div className="auth-heading">
        <h1>Create an account</h1>
        <p>Set up your secure Sou2AI owner account.</p>
      </div>
      <form onSubmit={submit} className="form-stack">
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="field-label">
            First name
            <input
              required
              name="first_name"
              autoComplete="given-name"
              value={fields.first_name}
              onChange={(event) => update("first_name", event.target.value)}
              className="input"
            />
          </label>
          <label className="field-label">
            Last name
            <input
              required
              name="last_name"
              autoComplete="family-name"
              value={fields.last_name}
              onChange={(event) => update("last_name", event.target.value)}
              className="input"
            />
          </label>
        </div>
        <label className="field-label">
          Email address
          <input
            required
            name="email"
            type="email"
            autoComplete="email"
            spellCheck={false}
            value={fields.email}
            onChange={(event) => update("email", event.target.value)}
            className="input"
          />
        </label>
        <PasswordField
          label="Password"
          name="password"
          value={fields.password}
          onChange={(value) => update("password", value)}
          autoComplete="new-password"
        />
        <PasswordField
          label="Confirm password"
          name="password_confirmation"
          value={fields.password_confirmation}
          onChange={(value) => update("password_confirmation", value)}
          autoComplete="new-password"
        />
        {error && <Alert>{error}</Alert>}
        <button type="submit" disabled={busy} className="btn w-full">
          <BusyLabel busy={busy} idle="Create account" />
          {!busy && <ArrowRight size={18} />}
        </button>
      </form>
      <p className="auth-footer">
        Already have an account? <Link to="/login">Sign in</Link>
      </p>
    </AuthFrame>
  );
}

function CheckEmailPage({
  dark,
  setDark,
  email,
}: {
  dark: boolean;
  setDark: (value: boolean) => void;
  email: string;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function resend() {
    setBusy(true);
    try {
      setMessage((await api.resend(email)).message);
    } catch (caught) {
      setMessage(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthFrame dark={dark} setDark={setDark}>
      <div className="auth-heading mt-8">
        <span className="auth-illustration">
          <Mail aria-hidden="true" />
        </span>
        <h1>Check your email</h1>
        <p>
          We sent a verification link to{" "}
          <strong className="text-blue-600 dark:text-blue-400">{email}</strong>.
        </p>
      </div>
      <p className="mx-auto max-w-sm text-center text-sm text-slate-600 dark:text-slate-300">
        Open the message and select the verification link to finish setting up your
        account.
      </p>
      {message && (
        <div className="mt-5">
          <Alert tone="info">{message}</Alert>
        </div>
      )}
      <button
        type="button"
        disabled={busy}
        onClick={() => void resend()}
        className="btn-secondary mt-6 w-full"
      >
        <BusyLabel busy={busy} idle="Resend verification email" />
      </button>
      <p className="auth-footer">
        <Link to="/login">Back to sign in</Link>
      </p>
    </AuthFrame>
  );
}

export function VerifyEmailPage({
  dark,
  setDark,
}: {
  dark: boolean;
  setDark: (value: boolean) => void;
}) {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  async function verify() {
    setBusy(true);
    setError("");
    try {
      setResult((await api.verify(token)).message);
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthFrame dark={dark} setDark={setDark}>
      <div className="auth-heading mt-8">
        <span className="auth-illustration">
          <CheckCircle2 />
        </span>
        <h1>Verify your email</h1>
        <p>Confirm this address before signing in to Sou2AI.</p>
      </div>
      {!token ? (
        <Alert>This verification link is invalid or incomplete.</Alert>
      ) : (
        !result && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void verify()}
            className="btn w-full"
          >
            <BusyLabel busy={busy} idle="Verify email" />
          </button>
        )
      )}
      {result && <Alert tone="success">{result}</Alert>}
      {error && <Alert>{error}</Alert>}
      <p className="auth-footer">
        <Link to="/login">Back to sign in</Link>
      </p>
    </AuthFrame>
  );
}

export function ForgotPasswordPage({
  dark,
  setDark,
}: {
  dark: boolean;
  setDark: (value: boolean) => void;
}) {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      setMessage((await api.forgot(email)).message);
    } catch (caught) {
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthFrame dark={dark} setDark={setDark}>
      <Link className="auth-back" to="/login" aria-label="Back to sign in">
        <ArrowLeft />
      </Link>
      <div className="auth-heading">
        <h1>Forgot password?</h1>
        <p>Enter your email and we&apos;ll send you a secure reset link.</p>
      </div>
      <form onSubmit={submit} className="form-stack">
        <label className="field-label">
          Email address
          <input
            required
            name="email"
            type="email"
            autoComplete="email"
            spellCheck={false}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="input"
          />
        </label>
        {message && <Alert tone="success">{message}</Alert>}
        {error && <Alert>{error}</Alert>}
        <button type="submit" disabled={busy} className="btn w-full">
          <BusyLabel busy={busy} idle="Send reset link" />
        </button>
      </form>
      <p className="auth-footer">
        <Link to="/login">Back to sign in</Link>
      </p>
    </AuthFrame>
  );
}

export function ResetPasswordPage({
  dark,
  setDark,
}: {
  dark: boolean;
  setDark: (value: boolean) => void;
}) {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (password !== confirmation) {
      setError("Password confirmation does not match.");
      return;
    }
    setBusy(true);
    try {
      setMessage(
        (await api.reset({ token, password, password_confirmation: confirmation }))
          .message,
      );
      setPassword("");
      setConfirmation("");
    } catch (caught) {
      setPassword("");
      setConfirmation("");
      setError(messageFor(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthFrame dark={dark} setDark={setDark}>
      <Link className="auth-back" to="/login" aria-label="Back to sign in">
        <ArrowLeft />
      </Link>
      <div className="auth-heading">
        <h1>Reset password</h1>
        <p>Enter your new password below.</p>
      </div>
      {!token ? (
        <Alert>This password reset link is invalid or incomplete.</Alert>
      ) : (
        <form onSubmit={submit} className="form-stack">
          <PasswordField
            label="New password"
            name="password"
            value={password}
            onChange={setPassword}
            autoComplete="new-password"
          />
          <PasswordField
            label="Confirm new password"
            name="password_confirmation"
            value={confirmation}
            onChange={setConfirmation}
            autoComplete="new-password"
          />
          {message && <Alert tone="success">{message}</Alert>}
          {error && <Alert>{error}</Alert>}
          <button type="submit" disabled={busy} className="btn w-full">
            <BusyLabel busy={busy} idle="Reset password" />
          </button>
        </form>
      )}
      <p className="auth-footer">
        <Link to="/login">Back to sign in</Link>
      </p>
    </AuthFrame>
  );
}
