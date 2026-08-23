import { ArrowLeft, LogOut, Mail, UserRound } from "lucide-react";
import { ReactNode, useEffect, useState } from "react";
import {
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { api, User } from "./api";
import {
  ForgotPasswordPage,
  LoginPage,
  RegisterPage,
  ResetPasswordPage,
  VerifyEmailPage,
} from "./auth";
import { BusinessPicker, OnboardingPage } from "./businesses";
import { Logo, ThemeButton } from "./ui";
import { WorkspaceRoute } from "./workspace";

function useTheme() {
  const [dark, setDark] = useState(() => {
    const saved = localStorage.getItem("sou2ai-theme");
    if (saved) return saved === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  });
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", dark ? "#070d18" : "#f8fafc");
    localStorage.setItem("sou2ai-theme", dark ? "dark" : "light");
  }, [dark]);
  return [dark, setDark] as const;
}

function SessionGate({ user, children }: { user: User | null; children: ReactNode }) {
  return user ? children : <Navigate to="/login" replace />;
}

function AccountPage({
  user,
  dark,
  setDark,
  onLogout,
}: {
  user: User;
  dark: boolean;
  setDark: (value: boolean) => void;
  onLogout: () => Promise<void>;
}) {
  return (
    <main className="account-page">
      <header>
        <Link to="/businesses" aria-label="Back to businesses">
          <Logo />
        </Link>
        <ThemeButton dark={dark} onChange={() => setDark(!dark)} />
      </header>
      <section className="account-card">
        <Link
          className="auth-back static mb-8 inline-flex"
          to="/businesses"
          aria-label="Back to businesses"
        >
          <ArrowLeft />
        </Link>
        <div className="account-avatar">
          <UserRound />
        </div>
        <p className="eyebrow">Owner account</p>
        <h1>
          {user.first_name} {user.last_name}
        </h1>
        <div className="account-details">
          <div>
            <Mail />
            <span>
              <small>Email address</small>
              <strong>{user.email}</strong>
            </span>
          </div>
          <div>
            <UserRound />
            <span>
              <small>Account status</small>
              <strong>{user.status.toLowerCase()}</strong>
            </span>
          </div>
        </div>
        <div className="account-theme">
          <div>
            <strong>Appearance</strong>
            <p>Choose the theme used across Sou2AI.</p>
          </div>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setDark(!dark)}
          >
            Use {dark ? "light" : "dark"} theme
          </button>
        </div>
        <button
          type="button"
          className="btn-danger w-full"
          onClick={() => void onLogout()}
        >
          <LogOut />
          Sign out
        </button>
      </section>
    </main>
  );
}

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [restoring, setRestoring] = useState(true);
  const [dark, setDark] = useTheme();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    const segment = location.pathname.split("/").filter(Boolean).at(-1) ?? "login";
    const titles: Record<string, string> = {
      login: "Sign in",
      register: "Create account",
      "forgot-password": "Forgot password",
      "verify-email": "Verify email",
      "reset-password": "Reset password",
      businesses: "Businesses",
      onboarding: "Business onboarding",
      overview: "Overview",
      chat: "AI Chat",
      conversations: "Conversations",
      knowledge: "Knowledge Base",
      analytics: "Analytics",
      customers: "Customers",
      "data-sources": "Data Sources",
      settings: "Business Settings",
      account: "Account",
    };
    document.title = `${titles[segment] ?? "Business workspace"} · Sou2AI`;
  }, [location.pathname]);

  useEffect(() => {
    let current = true;
    api
      .restoreSession()
      .then((restored) => {
        if (current) setUser(restored);
      })
      .catch(() => {
        if (current) setUser(null);
      })
      .finally(() => {
        if (current) setRestoring(false);
      });
    return () => {
      current = false;
    };
  }, []);

  async function logout() {
    try {
      await api.logout();
    } finally {
      setUser(null);
      navigate("/login", { replace: true });
    }
  }

  if (restoring) {
    return (
      <main className="session-loading">
        <Logo />
        <span className="skeleton h-2 w-32" />
        <p>Restoring your secure session…</p>
      </main>
    );
  }

  return (
    <Routes>
      <Route
        path="/login"
        element={
          user ? (
            <Navigate to="/businesses" replace />
          ) : (
            <LoginPage dark={dark} setDark={setDark} onAuthenticated={setUser} />
          )
        }
      />
      <Route
        path="/register"
        element={
          user ? (
            <Navigate to="/businesses" replace />
          ) : (
            <RegisterPage dark={dark} setDark={setDark} />
          )
        }
      />
      <Route
        path="/forgot-password"
        element={<ForgotPasswordPage dark={dark} setDark={setDark} />}
      />
      <Route
        path="/verify-email"
        element={<VerifyEmailPage dark={dark} setDark={setDark} />}
      />
      <Route
        path="/reset-password"
        element={<ResetPasswordPage dark={dark} setDark={setDark} />}
      />
      <Route
        path="/businesses"
        element={
          <SessionGate user={user}>
            {user && (
              <BusinessPicker
                user={user}
                dark={dark}
                setDark={setDark}
                onLogout={logout}
              />
            )}
          </SessionGate>
        }
      />
      <Route
        path="/businesses/:id/onboarding"
        element={
          <SessionGate user={user}>
            <OnboardingPage />
          </SessionGate>
        }
      />
      <Route
        path="/businesses/:id/:page"
        element={
          <SessionGate user={user}>
            {user && (
              <WorkspaceRoute
                user={user}
                dark={dark}
                setDark={setDark}
                onLogout={logout}
              />
            )}
          </SessionGate>
        }
      />
      <Route
        path="/account"
        element={
          <SessionGate user={user}>
            {user && (
              <AccountPage
                user={user}
                dark={dark}
                setDark={setDark}
                onLogout={logout}
              />
            )}
          </SessionGate>
        }
      />
      <Route
        path="*"
        element={<Navigate to={user ? "/businesses" : "/login"} replace />}
      />
    </Routes>
  );
}
