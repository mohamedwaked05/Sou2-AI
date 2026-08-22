import { FormEvent, ReactNode, useCallback, useEffect, useState } from "react";
import {
  Link,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
} from "react-router-dom";
import {
  Building2,
  FileText,
  LogOut,
  Menu,
  Moon,
  Plus,
  Send,
  Sun,
  Upload,
} from "lucide-react";
import {
  api,
  ApiError,
  Business,
  ChatMessage,
  Document,
  setAccessToken,
  User,
} from "./api";

const categories = [
  "GROCERY_SUPERMARKET",
  "BAKERY",
  "RESTAURANT",
  "CAFE",
  "PHARMACY",
  "OTHER",
];
const days = [
  "MONDAY",
  "TUESDAY",
  "WEDNESDAY",
  "THURSDAY",
  "FRIDAY",
  "SATURDAY",
  "SUNDAY",
];
const locations: Record<string, Record<string, string[]>> = {
  Beirut: { Beirut: ["Beirut"] },
  "Mount Lebanon": {
    Metn: ["Antelias", "Jdeideh", "Sin El Fil"],
    Baabda: ["Baabda", "Hazmieh"],
    Aley: ["Aley", "Choueifat"],
  },
  North: { Tripoli: ["Tripoli", "Mina"] },
  South: { Saida: ["Saida", "Abra", "Ghaziyeh"] },
};
function errorMessage(error: unknown) {
  return error instanceof ApiError
    ? error.message
    : "Network error. Check your connection and try again.";
}
function Logo() {
  return (
    <img src="/sou2ai-logo.png" alt="Sou2AI" className="h-9 w-auto object-contain" />
  );
}
function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main className="wave grid min-h-screen place-items-center p-5">
      <section className="w-full max-w-md">
        <div className="mb-8 flex justify-center">
          <Logo />
        </div>
        <div className="card">{children}</div>
      </section>
    </main>
  );
}
function useTheme() {
  const [dark, setDark] = useState(
    () =>
      localStorage.getItem("sou2ai-theme") === "dark" ||
      (!localStorage.getItem("sou2ai-theme") &&
        matchMedia("(prefers-color-scheme: dark)").matches),
  );
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("sou2ai-theme", dark ? "dark" : "light");
  }, [dark]);
  return [dark, setDark] as const;
}
function Login({ onLogin }: { onLogin: () => Promise<void> }) {
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(e.currentTarget);
    try {
      const r = await api.login({
        email: String(data.get("email")),
        password: String(data.get("password")),
        keep_me_signed_in: data.get("remember") === "on",
      });
      setAccessToken(r.access_token);
      await onLogin();
      nav("/businesses");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthLayout>
      <h1 className="text-2xl font-bold">Welcome back</h1>
      <p className="mb-6 mt-1 text-slate-500">Sign in to manage your business.</p>
      <form onSubmit={submit} className="space-y-4">
        <label>
          Email
          <input required name="email" type="email" className="input mt-1" />
        </label>
        <label>
          Password
          <input required name="password" type="password" className="input mt-1" />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input name="remember" type="checkbox" /> Keep me signed in
        </label>
        {error && (
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <button disabled={busy} className="btn w-full">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <div className="mt-5 flex justify-between text-sm text-blue-600">
        <Link to="/forgot-password">Forgot password?</Link>
        <Link to="/register">Create account</Link>
      </div>
    </AuthLayout>
  );
}
function Register() {
  const nav = useNavigate();
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(e.currentTarget));
    try {
      const r = await api.register(d);
      setMessage(r.message);
      setTimeout(() => nav("/login"), 1200);
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <AuthLayout>
      <h1 className="text-2xl font-bold">Create your account</h1>
      <form onSubmit={submit} className="mt-5 space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <label>
            First name
            <input required name="first_name" className="input mt-1" />
          </label>
          <label>
            Last name
            <input required name="last_name" className="input mt-1" />
          </label>
        </div>
        <label>
          Email
          <input required name="email" type="email" className="input mt-1" />
        </label>
        <label>
          Password
          <input
            required
            minLength={8}
            name="password"
            type="password"
            className="input mt-1"
          />
        </label>
        <label>
          Confirm password
          <input
            required
            name="password_confirmation"
            type="password"
            className="input mt-1"
          />
        </label>
        {message && <p className="text-emerald-600">{message}</p>}
        {error && (
          <p role="alert" className="text-red-600">
            {error}
          </p>
        )}
        <button className="btn w-full">Create account</button>
      </form>
      <Link className="mt-5 block text-sm text-blue-600" to="/login">
        Already have an account?
      </Link>
    </AuthLayout>
  );
}
function TokenPage({ type }: { type: "verify" | "reset" }) {
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const token = new URLSearchParams(location.search).get("token") ?? "";
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    try {
      const d = new FormData(e.currentTarget);
      const r =
        type === "verify"
          ? await api.verify(token)
          : await api.reset({
              token,
              password: d.get("password"),
              password_confirmation: d.get("password_confirmation"),
            });
      setResult(r.message);
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <AuthLayout>
      <h1 className="text-2xl font-bold">
        {type === "verify" ? "Verify email" : "Reset password"}
      </h1>
      {!token ? (
        <p role="alert" className="mt-4 text-red-600">
          This link is invalid or incomplete.
        </p>
      ) : type === "verify" ? (
        <button
          onClick={() =>
            void submit({ preventDefault() {} } as FormEvent<HTMLFormElement>)
          }
          className="btn mt-5"
        >
          Verify email
        </button>
      ) : (
        <form onSubmit={submit} className="mt-5 space-y-3">
          <label>
            New password
            <input required name="password" type="password" className="input mt-1" />
          </label>
          <label>
            Confirm password
            <input
              required
              name="password_confirmation"
              type="password"
              className="input mt-1"
            />
          </label>
          <button className="btn">Reset password</button>
        </form>
      )}
      {result && <p className="mt-4 text-emerald-600">{result}</p>}
      {error && (
        <p role="alert" className="mt-4 text-red-600">
          {error}
        </p>
      )}
      <Link className="mt-5 block text-blue-600" to="/login">
        Back to sign in
      </Link>
    </AuthLayout>
  );
}
function Forgot() {
  const [done, setDone] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    await api.forgot(String(new FormData(e.currentTarget).get("email")));
    setDone(true);
  }
  return (
    <AuthLayout>
      <h1 className="text-2xl font-bold">Reset your password</h1>
      {done ? (
        <p className="mt-4 text-emerald-600">
          If an account exists for this email, a password-reset link has been sent.
        </p>
      ) : (
        <form onSubmit={submit} className="mt-5 space-y-3">
          <label>
            Email
            <input required name="email" type="email" className="input mt-1" />
          </label>
          <button className="btn">Send reset link</button>
        </form>
      )}
    </AuthLayout>
  );
}
function BusinessPicker() {
  const nav = useNavigate();
  const [items, setItems] = useState<Business[]>([]);
  const [name, setName] = useState("");
  useEffect(() => {
    void api.businesses().then(setItems);
  }, []);
  async function create() {
    const b = await api.createBusiness(name);
    nav(`/businesses/${b.id}/onboarding`);
  }
  return (
    <main className="mx-auto max-w-6xl p-6">
      <header className="flex items-center justify-between">
        <Logo />
        <Link to="/account">Account</Link>
      </header>
      <h1 className="mt-10 text-3xl font-bold">Choose a business</h1>
      <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((b) => (
          <button
            key={b.id}
            onClick={() =>
              nav(
                `/businesses/${b.id}/${b.status === "ACTIVE" ? "overview" : "onboarding"}`,
              )
            }
            className="card text-left hover:border-blue-500"
          >
            <span className="font-semibold">{b.name}</span>
            <p className="mt-2 text-sm text-slate-500">
              {b.category ?? "Category pending"} · {b.city ?? "Location pending"}
            </p>
            <p className="mt-3 text-sm">
              {b.status} ·{" "}
              {b.profile_complete ? "Profile complete" : "Profile incomplete"}
            </p>
          </button>
        ))}
        <div className="card">
          <label>
            New business
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="input mt-2"
              placeholder="Business name"
            />
          </label>
          <button
            disabled={name.trim().length < 2}
            onClick={() => void create()}
            className="btn mt-3 w-full"
          >
            <Plus size={18} />
            Create new business
          </button>
        </div>
      </div>
    </main>
  );
}
function Shell({
  user,
  dark,
  setDark,
  children,
}: {
  user: User;
  dark: boolean;
  setDark: (v: boolean) => void;
  children: ReactNode;
}) {
  const { id = "" } = useParams();
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  const links = [
    ["overview", "Overview"],
    ["chat", "AI Chat"],
    ["conversations", "Conversations"],
    ["knowledge", "Knowledge Base"],
    ["analytics", "Analytics"],
    ["customers", "Customers"],
    ["data-sources", "Data Sources"],
    ["settings", "Business Settings"],
  ];
  return (
    <div className="min-h-screen md:flex">
      <aside
        className={`${open ? "fixed inset-y-0 z-20" : "hidden"} w-64 border-r border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900 md:static md:block`}
      >
        <button className="mb-8" onClick={() => setOpen(false)}>
          <Logo />
        </button>
        <nav className="space-y-1">
          {links.map(([path, label]) => (
            <Link
              key={path}
              className="block rounded-lg px-3 py-2 hover:bg-blue-50 dark:hover:bg-slate-800"
              to={`/businesses/${id}/${path}`}
            >
              {label}
            </Link>
          ))}
        </nav>
        <div className="mt-6 border-t pt-4">
          <Link className="block px-3 py-2" to="/businesses">
            Switch business
          </Link>
          <Link className="block px-3 py-2" to="/account">
            Account
          </Link>
          <button
            className="flex w-full gap-2 px-3 py-2"
            onClick={() => {
              void api.logout();
              setAccessToken(null);
              nav("/login");
            }}
          >
            <LogOut size={18} />
            Sign out
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <header className="flex items-center justify-between border-b border-slate-200 p-4 dark:border-slate-800">
          <button
            className="md:hidden"
            aria-label="Open navigation"
            onClick={() => setOpen(true)}
          >
            <Menu />
          </button>
          <Link to="/businesses" className="hidden md:block">
            Switch business
          </Link>
          <div className="flex items-center gap-3">
            <span className="hidden text-sm sm:block">{user.first_name}</span>
            <button aria-label="Toggle theme" onClick={() => setDark(!dark)}>
              {dark ? <Sun /> : <Moon />}
            </button>
          </div>
        </header>
        {children}
      </main>
    </div>
  );
}
function Onboarding() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const [b, setB] = useState<Business | null>(null);
  const [step, setStep] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    void api.business(id).then((x) => {
      setB(x);
      setStep(
        Math.max(
          0,
          ["business_details", "location", "working_hours"].indexOf(
            x.first_incomplete_section ?? "",
          ),
        ),
      );
    });
  }, [id]);
  if (!b) return <p className="p-6">Loading onboarding…</p>;
  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const d = new FormData(e.currentTarget);
    const body: Record<string, unknown> =
      step === 0
        ? {
            name: d.get("name"),
            category: d.get("category"),
            custom_category: d.get("custom_category") || null,
          }
        : step === 1
          ? {
              governorate: d.get("governorate"),
              district: d.get("district"),
              city: d.get("city"),
              address_line: d.get("address_line"),
            }
          : step === 2
            ? {
                working_hours: days.map((day) => ({
                  weekday: day,
                  is_closed: d.get(`${day}-closed`) === "on",
                  shifts:
                    d.get(`${day}-closed`) === "on"
                      ? []
                      : [{ start: d.get(`${day}-start`), end: d.get(`${day}-end`) }],
                })),
              }
            : {
                description: d.get("description"),
                default_language: d.get("default_language"),
              };
    try {
      const next = await api.updateBusiness(id, body);
      setB(next);
      setStep((s) => Math.min(4, s + 1));
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  if (step === 4)
    return (
      <main className="wave min-h-screen p-6">
        <section className="card mx-auto max-w-2xl">
          <h1 className="text-2xl font-bold">Review your business</h1>
          <p className="mt-3">
            {b.name} · {b.city} · {b.default_language}
          </p>
          <button
            className="btn mt-6"
            onClick={() => void api.confirm(id).then(() => nav("/businesses"))}
          >
            Confirm onboarding
          </button>
        </section>
      </main>
    );
  const loc = locations[b.governorate ?? ""] ?? {};
  return (
    <main className="wave min-h-screen p-6">
      <section className="card mx-auto max-w-2xl">
        <button className="text-sm text-blue-600" onClick={() => nav("/businesses")}>
          Save & exit
        </button>
        <h1 className="mt-4 text-2xl font-bold">Set up {b.name}</h1>
        <p className="mb-6 text-slate-500">Step {step + 1} of 5</p>
        <form onSubmit={save} className="space-y-4">
          {step === 0 && (
            <>
              <label>
                Name
                <input
                  required
                  defaultValue={b.name}
                  name="name"
                  className="input mt-1"
                />
              </label>
              <label>
                Category
                <select
                  required
                  defaultValue={b.category ?? ""}
                  name="category"
                  className="input mt-1"
                >
                  <option value="">Choose</option>
                  {categories.map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </select>
              </label>
              <label>
                Custom category
                <input
                  name="custom_category"
                  defaultValue={b.custom_category ?? ""}
                  className="input mt-1"
                />
              </label>
            </>
          )}
          {step === 1 && (
            <>
              <label>
                Governorate
                <select
                  required
                  defaultValue={b.governorate ?? ""}
                  name="governorate"
                  className="input mt-1"
                >
                  {Object.keys(locations).map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </select>
              </label>
              <label>
                District
                <select
                  required
                  defaultValue={b.district ?? ""}
                  name="district"
                  className="input mt-1"
                >
                  {Object.keys(loc).map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </select>
              </label>
              <label>
                City / area
                <input
                  required
                  name="city"
                  defaultValue={b.city ?? ""}
                  className="input mt-1"
                />
              </label>
              <label>
                Address
                <input
                  required
                  name="address_line"
                  defaultValue={b.address_line ?? ""}
                  className="input mt-1"
                />
              </label>
            </>
          )}
          {step === 2 &&
            days.map((day) => (
              <fieldset key={day} className="grid grid-cols-3 items-center gap-2">
                <legend>{day}</legend>
                <label>
                  <input name={`${day}-closed`} type="checkbox" /> Closed
                </label>
                <input
                  name={`${day}-start`}
                  type="time"
                  defaultValue="09:00"
                  className="input"
                />
                <input
                  name={`${day}-end`}
                  type="time"
                  defaultValue="17:00"
                  className="input"
                />
              </fieldset>
            ))}
          {step === 3 && (
            <>
              <label>
                Primary language
                <select
                  required
                  name="default_language"
                  defaultValue={b.default_language ?? ""}
                  className="input mt-1"
                >
                  <option value="">Choose</option>
                  <option value="en">English</option>
                  <option value="ar">Arabic</option>
                </select>
              </label>
              <label>
                Description
                <textarea
                  required
                  minLength={20}
                  name="description"
                  defaultValue={b.description ?? ""}
                  className="input mt-1 min-h-32"
                />
              </label>
            </>
          )}
          {error && (
            <p role="alert" className="text-red-600">
              {error}
            </p>
          )}
          <button className="btn">Save and continue</button>
        </form>
      </section>
    </main>
  );
}
function Page() {
  const { id = "", page = "overview" } = useParams();
  const [business, setBusiness] = useState<Business | null>(null);
  useEffect(() => {
    void api.business(id).then(setBusiness);
  }, [id]);
  if (!business) return <p className="p-6">Loading…</p>;
  return (
    <section className="mx-auto max-w-6xl p-6">
      {page === "overview" ? (
        <Overview b={business} />
      ) : page === "chat" || page === "conversations" ? (
        <Chat
          id={id}
          disabled={business.status !== "ACTIVE"}
          readOnly={page === "conversations"}
        />
      ) : page === "knowledge" ? (
        <Knowledge id={id} />
      ) : page === "settings" ? (
        <Settings b={business} onSaved={setBusiness} />
      ) : (
        <Empty
          title={
            page === "analytics"
              ? "Analytics arrives with Milestone 16"
              : page === "customers"
                ? "Customer integrations are not yet authorized"
                : "Data sources arrive with Milestone 16"
          }
          text={
            page === "data-sources"
              ? "Future tenant-scoped integrations will be read-only through controlled tools."
              : "There is no real operational data to show yet."
          }
        />
      )}
    </section>
  );
}
function Overview({ b }: { b: Business }) {
  const [docs, setDocs] = useState<Document[]>([]);
  useEffect(() => {
    void api
      .documents(b.id)
      .then(setDocs)
      .catch(() => setDocs([]));
  }, [b.id]);
  return (
    <>
      <h1 className="text-3xl font-bold">{b.name}</h1>
      <div className="mt-6 grid gap-4 sm:grid-cols-3">
        <div className="card">
          <p className="text-sm text-slate-500">Lifecycle</p>
          <b>{b.status}</b>
        </div>
        <div className="card">
          <p className="text-sm text-slate-500">Profile completion</p>
          <b>{b.profile_complete ? "Complete" : "Incomplete"}</b>
        </div>
        <div className="card">
          <p className="text-sm text-slate-500">Knowledge documents</p>
          <b>{docs.length}</b>
        </div>
      </div>
      <div className="card mt-5">
        <h2 className="font-semibold">Setup checklist</h2>
        <ul className="mt-3 list-inside list-disc text-sm">
          <li>
            {b.description ? "Business details complete" : "Add business details"}
          </li>
          <li>{b.city ? "Location complete" : "Add location"}</li>
          <li>
            {b.working_hours.length === 7
              ? "Working hours complete"
              : "Add all working hours"}
          </li>
          <li>
            {b.onboarding_submitted_at ? "Onboarding confirmed" : "Confirm onboarding"}
          </li>
        </ul>
      </div>
    </>
  );
}
function Chat({
  id,
  disabled,
  readOnly,
}: {
  id: string;
  disabled: boolean;
  readOnly: boolean;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    void api.messages(id).then((r) => setMessages(r.items));
  }, [id]);
  async function send(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    try {
      await api.send(id, text);
      setText("");
      setMessages((await api.messages(id)).items);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <h1 className="text-3xl font-bold">
        {readOnly ? "Conversation history" : "AI Chat"}
      </h1>
      {disabled && (
        <p className="card mt-5">AI chat is available only for active businesses.</p>
      )}
      <div className="card mt-5 space-y-4">
        {messages.length ? (
          messages.map((m) => (
            <article key={m.id} className={m.role === "owner" ? "text-right" : ""}>
              <p className="inline-block rounded-xl bg-slate-100 p-3 text-left dark:bg-slate-800">
                {m.content}
              </p>
              {m.sources.map((s) => (
                <p key={s.filename} className="text-xs text-slate-500">
                  Source: {s.filename}
                  {s.page_start ? `, page ${s.page_start}` : ""}
                </p>
              ))}
            </article>
          ))
        ) : (
          <p className="text-slate-500">No messages yet.</p>
        )}
      </div>
      {!readOnly && (
        <form onSubmit={send} className="mt-4 flex gap-2">
          <input
            disabled={disabled || busy}
            value={text}
            onChange={(e) => setText(e.target.value)}
            className="input"
            aria-label="Message"
            placeholder="Ask about your profile, hours, policies, or documents"
          />
          <button disabled={disabled || busy} className="btn">
            <Send size={18} />
            Send
          </button>
        </form>
      )}
    </>
  );
}
function Knowledge({ id }: { id: string }) {
  const [items, setItems] = useState<Document[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const load = useCallback(() => void api.documents(id).then(setItems), [id]);
  useEffect(load, [load]);
  useEffect(() => {
    if (!items.some((x) => x.status === "PENDING" || x.status === "PROCESSING")) return;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [items, load]);
  return (
    <>
      <h1 className="text-3xl font-bold">Knowledge Base</h1>
      <div className="card mt-5 flex flex-wrap gap-3">
        <input
          aria-label="Upload document"
          type="file"
          accept=".pdf,.docx,.txt"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        <button
          className="btn"
          disabled={!file}
          onClick={() => {
            if (file) void api.upload(id, file).then(load);
          }}
        >
          <Upload size={18} />
          Upload
        </button>
      </div>
      <div className="mt-5 space-y-3">
        {items.map((d) => (
          <article
            key={d.id}
            className="card flex flex-wrap items-center justify-between gap-3"
          >
            <span>
              <FileText className="mr-2 inline" size={18} />
              {d.original_filename}
            </span>
            <span>
              {d.status}
              {d.failure_code ? `: ${d.failure_code}` : ""}
            </span>
            <div className="flex gap-2">
              {d.status === "FAILED" && (
                <button
                  className="btn"
                  onClick={() => void api.retryDocument(id, d.id).then(load)}
                >
                  Retry
                </button>
              )}
              <label className="btn">
                Replace
                <input
                  className="hidden"
                  type="file"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) void api.replaceDocument(id, d.id, f).then(load);
                  }}
                />
              </label>
              <button
                className="rounded-xl px-3 text-red-600"
                onClick={() => {
                  if (confirm(`Delete ${d.original_filename}? This cannot be undone.`))
                    void api.deleteDocument(id, d.id).then(load);
                }}
              >
                Delete
              </button>
            </div>
          </article>
        ))}
        {items.length === 0 && (
          <Empty
            title="No knowledge documents"
            text="Upload a private PDF, DOCX, or TXT document to use it in owner chat."
          />
        )}
      </div>
    </>
  );
}
function Settings({ b, onSaved }: { b: Business; onSaved: (b: Business) => void }) {
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const d = new FormData(e.currentTarget);
    onSaved(
      await api.updateBusiness(b.id, {
        name: d.get("name"),
        description: d.get("description"),
        address_line: d.get("address_line"),
      }),
    );
  }
  return (
    <>
      <h1 className="text-3xl font-bold">Business Settings</h1>
      <p className="mt-2 text-slate-500">
        Lifecycle: {b.status} · Onboarding:{" "}
        {b.onboarding_submitted_at ? "confirmed" : "not confirmed"}
      </p>
      <form onSubmit={submit} className="card mt-5 max-w-2xl space-y-4">
        <label>
          Name
          <input name="name" required defaultValue={b.name} className="input mt-1" />
        </label>
        <label>
          Description
          <textarea
            name="description"
            minLength={20}
            defaultValue={b.description ?? ""}
            className="input mt-1 min-h-28"
          />
        </label>
        <label>
          Address
          <input
            name="address_line"
            defaultValue={b.address_line ?? ""}
            className="input mt-1"
          />
        </label>
        <button className="btn">Save profile</button>
      </form>
    </>
  );
}
function Empty({ title, text }: { title: string; text: string }) {
  return (
    <div className="card mt-5 text-center">
      <Building2 className="mx-auto text-blue-600" />
      <h2 className="mt-3 font-semibold">{title}</h2>
      <p className="mt-1 text-slate-500">{text}</p>
    </div>
  );
}
function Account({
  user,
  dark,
  setDark,
}: {
  user: User;
  dark: boolean;
  setDark: (v: boolean) => void;
}) {
  return (
    <main className="mx-auto max-w-2xl p-6">
      <Logo />
      <h1 className="mt-8 text-3xl font-bold">Account</h1>
      <div className="card mt-5">
        <p>
          {user.first_name} {user.last_name}
        </p>
        <p className="text-slate-500">{user.email}</p>
        <button className="btn mt-4" onClick={() => setDark(!dark)}>
          Use {dark ? "light" : "dark"} mode
        </button>
      </div>
      <Link className="mt-5 block text-blue-600" to="/businesses">
        Back to businesses
      </Link>
    </main>
  );
}
export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [dark, setDark] = useTheme();
  async function restore() {
    try {
      await api.refresh();
      setUser(await api.me());
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void restore();
  }, []);
  if (loading)
    return (
      <main className="grid min-h-screen place-items-center">Loading Sou2AI…</main>
    );
  const protectedShell = (child: ReactNode) =>
    user ? (
      <Shell user={user} dark={dark} setDark={setDark}>
        {child}
      </Shell>
    ) : (
      <Navigate to="/login" replace />
    );
  return (
    <Routes>
      <Route path="/login" element={<Login onLogin={restore} />} />
      <Route path="/register" element={<Register />} />
      <Route path="/forgot-password" element={<Forgot />} />
      <Route path="/verify-email" element={<TokenPage type="verify" />} />
      <Route path="/reset-password" element={<TokenPage type="reset" />} />
      <Route
        path="/businesses"
        element={user ? <BusinessPicker /> : <Navigate to="/login" />}
      />
      <Route
        path="/businesses/:id/onboarding"
        element={user ? <Onboarding /> : <Navigate to="/login" />}
      />
      <Route path="/businesses/:id/:page" element={protectedShell(<Page />)} />
      <Route
        path="/account"
        element={
          user ? (
            <Account user={user} dark={dark} setDark={setDark} />
          ) : (
            <Navigate to="/login" />
          )
        }
      />
      <Route path="*" element={<Navigate to={user ? "/businesses" : "/login"} />} />
    </Routes>
  );
}
