import {
  Activity,
  AlertCircle,
  Archive,
  BarChart3,
  BookOpen,
  Bot,
  Building2,
  Check,
  ChevronRight,
  Clock3,
  Database,
  FileText,
  LayoutDashboard,
  LoaderCircle,
  LogOut,
  Menu,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  RefreshCw,
  Send,
  Settings,
  Smartphone,
  Store,
  Trash2,
  Upload,
  UserRound,
  UsersRound,
  X,
} from "lucide-react";
import {
  FormEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Link, Navigate, useLocation, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  Business,
  ChatMessage,
  Conversation,
  CustomerConversation,
  CustomerMessage,
  Document,
  ownerChatErrorMessage,
  Usage,
  User,
  WhatsAppConnection,
  WorkingDay,
} from "./api";
import { ScheduleEditor } from "./businesses";
import { CATEGORIES, categoryLabel, LOCATIONS } from "./constants";
import { DataSourcesPage } from "./data-sources";
import {
  Alert,
  BusyLabel,
  Logo,
  PageHeading,
  Skeleton,
  StatusBadge,
  ThemeButton,
} from "./ui";

function errorMessage(error: unknown) {
  return error instanceof ApiError
    ? error.message
    : "We couldn't complete that request. Try again.";
}

const navItems = [
  ["overview", "Overview", LayoutDashboard],
  ["chat", "AI Chat", Bot],
  ["conversations", "Conversations", MessageSquare],
  ["knowledge", "Knowledge Base", BookOpen],
  ["analytics", "Analytics", BarChart3],
  ["customers", "Customers", UsersRound],
  ["data-sources", "Data Sources", Database],
  ["settings", "Business Settings", Settings],
] as const;

const PINNED_BOTTOM_THRESHOLD_PX = 96;
const COMPOSER_MAX_LINES = 6;

function usePinnedToBottom(
  contentVersion: unknown,
  ready: boolean,
  resetKey: string,
  trailingContentVisible = false,
) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const positionedRef = useRef(false);
  const resetKeyRef = useRef(resetKey);

  const onScroll = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const distanceFromBottom =
      viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop;
    pinnedRef.current = distanceFromBottom <= PINNED_BOTTOM_THRESHOLD_PX;
  }, []);

  useLayoutEffect(() => {
    if (resetKeyRef.current !== resetKey) {
      resetKeyRef.current = resetKey;
      positionedRef.current = false;
      pinnedRef.current = true;
    }

    const viewport = viewportRef.current;
    if (!ready || !viewport) return;

    const bottom = viewport.scrollHeight;
    if (!positionedRef.current) {
      viewport.scrollTop = bottom;
      positionedRef.current = true;
      pinnedRef.current = true;
      return;
    }
    if (!pinnedRef.current) return;

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      viewport.scrollTop = bottom;
    } else if (typeof viewport.scrollTo === "function") {
      viewport.scrollTo({ top: bottom, behavior: "smooth" });
    } else {
      viewport.scrollTop = bottom;
    }
  }, [contentVersion, ready, resetKey, trailingContentVisible]);

  return { viewportRef, onScroll };
}

function pixelValue(value: string) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function resizeComposer(textarea: HTMLTextAreaElement, empty: boolean) {
  textarea.style.height = "auto";
  textarea.style.overflowY = "hidden";

  const style = window.getComputedStyle(textarea);
  const fontSize = pixelValue(style.fontSize) || 16;
  const parsedLineHeight = pixelValue(style.lineHeight);
  const lineHeight =
    style.lineHeight === "normal"
      ? fontSize * 1.2
      : parsedLineHeight > 4
        ? parsedLineHeight
        : (parsedLineHeight || 1.55) * fontSize;
  const verticalChrome =
    pixelValue(style.paddingTop) +
    pixelValue(style.paddingBottom) +
    pixelValue(style.borderTopWidth) +
    pixelValue(style.borderBottomWidth);
  const oneLineHeight = lineHeight + verticalChrome;
  const maximumHeight = lineHeight * COMPOSER_MAX_LINES + verticalChrome;
  const contentHeight = empty ? oneLineHeight : textarea.scrollHeight;

  textarea.style.height = `${Math.max(oneLineHeight, Math.min(contentHeight, maximumHeight))}px`;
  textarea.style.overflowY =
    !empty && textarea.scrollHeight > maximumHeight ? "auto" : "hidden";
}

export function WorkspaceRoute({
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
  const { id = "", page = "overview" } = useParams();
  const [business, setBusiness] = useState<Business | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let current = true;
    api
      .business(id)
      .then((item) => current && setBusiness(item))
      .catch((caught) => current && setError(errorMessage(caught)));
    return () => {
      current = false;
    };
  }, [id]);
  if (error)
    return (
      <main className="grid min-h-dvh place-items-center p-6">
        <Alert>{error}</Alert>
      </main>
    );
  if (!business) return <WorkspaceLoading />;
  return (
    <AppShell
      business={business}
      user={user}
      page={page}
      dark={dark}
      setDark={setDark}
      onLogout={onLogout}
    >
      <WorkspacePage page={page} business={business} onBusinessChange={setBusiness} />
    </AppShell>
  );
}

function WorkspaceLoading() {
  return (
    <div className="app-shell">
      <aside className="sidebar hidden lg:block">
        <Skeleton className="h-full" />
      </aside>
      <main className="min-w-0 flex-1 p-6">
        <Skeleton className="h-12" />
        <Skeleton className="mt-8 h-96" />
      </main>
    </div>
  );
}

function AppShell({
  business,
  user,
  page,
  dark,
  setDark,
  onLogout,
  children,
}: {
  business: Business;
  user: User;
  page: string;
  dark: boolean;
  setDark: (value: boolean) => void;
  onLogout: () => Promise<void>;
  children: React.ReactNode;
}) {
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(() => {
    const saved = localStorage.getItem("sou2ai-sidebar-collapsed");
    return saved ? saved === "true" : window.innerWidth < 1100;
  });
  const [mobileOpen, setMobileOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const mobileMenuButton = useRef<HTMLButtonElement>(null);
  const firstMobileLink = useRef<HTMLAnchorElement>(null);
  const mobileSidebar = useRef<HTMLElement>(null);
  const accountTrigger = useRef<HTMLButtonElement>(null);
  const accountMenu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    localStorage.setItem("sou2ai-sidebar-collapsed", String(collapsed));
  }, [collapsed]);
  useEffect(() => {
    setMobileOpen(false);
    setAccountOpen(false);
  }, [location.pathname]);
  useEffect(() => {
    if (!mobileOpen) return;
    const menuButton = mobileMenuButton.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    firstMobileLink.current?.focus();
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileOpen(false);
      if (event.key === "Tab") {
        const focusable = mobileSidebar.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
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
    };
    document.addEventListener("keydown", close);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", close);
      menuButton?.focus();
    };
  }, [mobileOpen]);
  useEffect(() => {
    if (!accountOpen) return;
    accountMenu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const close = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setAccountOpen(false);
      accountTrigger.current?.focus();
    };
    const closeOutside = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !accountMenu.current?.contains(event.target) &&
        !accountTrigger.current?.contains(event.target)
      ) {
        setAccountOpen(false);
      }
    };
    document.addEventListener("keydown", close);
    document.addEventListener("pointerdown", closeOutside);
    return () => {
      document.removeEventListener("keydown", close);
      document.removeEventListener("pointerdown", closeOutside);
    };
  }, [accountOpen]);

  const sidebar = (mobile: boolean) => (
    <aside
      ref={mobile ? mobileSidebar : undefined}
      className={`sidebar ${collapsed && !mobile ? "sidebar-collapsed" : ""} ${mobile ? "sidebar-mobile" : ""}`}
      aria-label="Business sidebar"
    >
      <div className="sidebar-brand">
        <Link to="/businesses" aria-label="Sou2AI business picker">
          <Logo compact={collapsed && !mobile} />
        </Link>
        {mobile && (
          <button
            type="button"
            className="icon-button"
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
          >
            <X />
          </button>
        )}
      </div>
      <div className="current-business">
        <span>
          <Store />
        </span>
        {(!collapsed || mobile) && (
          <div>
            <strong>{business.name}</strong>
            <small>{categoryLabel(business.category)}</small>
          </div>
        )}
      </div>
      <nav className="sidebar-nav" aria-label="Business navigation">
        {navItems.map(([path, label, Icon], index) => (
          <Link
            ref={mobile && index === 0 ? firstMobileLink : undefined}
            key={path}
            to={`/businesses/${business.id}/${path}`}
            className={page === path ? "active" : ""}
            aria-current={page === path ? "page" : undefined}
            title={collapsed && !mobile ? label : undefined}
          >
            <Icon aria-hidden="true" />
            {(!collapsed || mobile) && <span>{label}</span>}
          </Link>
        ))}
      </nav>
      <div className="sidebar-footer">
        <Link
          to="/businesses"
          title={collapsed && !mobile ? "Switch Business" : undefined}
        >
          <Building2 />
          {(!collapsed || mobile) && <span>Switch Business</span>}
        </Link>
        <Link to="/account" title={collapsed && !mobile ? "Account" : undefined}>
          <UserRound />
          {(!collapsed || mobile) && <span>Account</span>}
        </Link>
        <button
          type="button"
          onClick={() => void onLogout()}
          title={collapsed && !mobile ? "Sign out" : undefined}
        >
          <LogOut />
          {(!collapsed || mobile) && <span>Sign out</span>}
        </button>
      </div>
      {!mobile && (
        <button
          type="button"
          className="collapse-button"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={() => setCollapsed((value) => !value)}
        >
          {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
        </button>
      )}
    </aside>
  );

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <div className="desktop-sidebar" inert={mobileOpen ? true : undefined}>
        {sidebar(false)}
      </div>
      {mobileOpen && (
        <div className="mobile-nav-layer">
          <button
            type="button"
            className="nav-scrim"
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
          />
          {sidebar(true)}
        </div>
      )}
      <main className="workspace" inert={mobileOpen ? true : undefined}>
        <header className="topbar">
          <button
            type="button"
            ref={mobileMenuButton}
            className="icon-button mobile-menu"
            aria-label="Open navigation"
            aria-expanded={mobileOpen}
            onClick={() => setMobileOpen(true)}
          >
            <Menu />
          </button>
          <div className="topbar-title">
            <span>{navItems.find(([path]) => path === page)?.[1] ?? "Sou2AI"}</span>
            <StatusBadge status={business.status} />
          </div>
          <div className="topbar-actions">
            <ThemeButton dark={dark} onChange={() => setDark(!dark)} />
            <div className="account-menu-wrap">
              <button
                type="button"
                ref={accountTrigger}
                className="account-trigger"
                aria-label={accountOpen ? "Close account menu" : "Open account menu"}
                aria-expanded={accountOpen}
                aria-haspopup="menu"
                aria-controls="account-menu"
                onClick={() => setAccountOpen((value) => !value)}
              >
                <span>
                  {user.first_name.slice(0, 1)}
                  {user.last_name.slice(0, 1)}
                </span>
                <span className="hidden sm:block">
                  <strong>
                    {user.first_name} {user.last_name}
                  </strong>
                  <small>Owner</small>
                </span>
              </button>
              {accountOpen && (
                <div
                  ref={accountMenu}
                  id="account-menu"
                  role="menu"
                  className="account-menu"
                >
                  <Link role="menuitem" to="/account">
                    <UserRound />
                    Profile and theme
                  </Link>
                  <button type="button" role="menuitem" onClick={() => void onLogout()}>
                    <LogOut />
                    Sign out
                  </button>
                </div>
              )}
            </div>
          </div>
        </header>
        <div id="main-content" className="workspace-content" tabIndex={-1}>
          {children}
        </div>
      </main>
    </div>
  );
}

function WorkspacePage({
  page,
  business,
  onBusinessChange,
}: {
  page: string;
  business: Business;
  onBusinessChange: (business: Business) => void;
}) {
  if (page === "overview") return <OverviewPage business={business} />;
  if (page === "chat") return <ChatPage business={business} />;
  if (page === "conversations") return <ConversationsPage business={business} />;
  if (page === "knowledge") return <KnowledgePage business={business} />;
  if (page === "settings")
    return <BusinessSettingsPage business={business} onSaved={onBusinessChange} />;
  if (page === "analytics")
    return (
      <FuturePage
        icon={BarChart3}
        title="Analytics"
        description="Operational analytics are not connected yet."
        text="Sou2AI will show analytics only after an approved tenant-scoped operational data source is implemented."
      />
    );
  if (page === "customers")
    return (
      <FuturePage
        icon={UsersRound}
        title="Customers"
        description="Customer channels are not available yet."
        text="No customer records or external messaging conversations are stored in the current milestone."
      />
    );
  if (page === "data-sources") return <DataSourcesPage business={business} />;
  return <Navigate to={`/businesses/${business.id}/overview`} replace />;
}

function OverviewPage({ business }: { business: Business }) {
  const [documents, setDocuments] = useState<Document[] | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [messages, setMessages] = useState<ChatMessage[] | null>(null);
  const [unavailable, setUnavailable] = useState<string[]>([]);
  useEffect(() => {
    if (!business.is_active) {
      setDocuments([]);
      setMessages([]);
      return;
    }
    void Promise.allSettled([
      api.documents(business.id),
      api.usage(business.id),
      api.messages(business.id),
    ]).then(([docs, currentUsage, history]) => {
      const failed: string[] = [];
      setDocuments(docs.status === "fulfilled" ? docs.value : []);
      if (docs.status === "rejected") failed.push("knowledge documents");
      setUsage(currentUsage.status === "fulfilled" ? currentUsage.value : null);
      if (currentUsage.status === "rejected") failed.push("AI token allowance");
      setMessages(history.status === "fulfilled" ? history.value.items : []);
      if (history.status === "rejected") failed.push("owner conversation");
      setUnavailable(failed);
    });
  }, [business.id, business.is_active]);
  const ready =
    documents?.filter((document) => document.status === "READY").length ?? 0;
  const checklist = [
    [
      Boolean(business.description && business.category),
      "Complete business information",
      "settings",
    ],
    [
      Boolean(business.city && business.address_line),
      "Set the business location",
      "settings",
    ],
    [business.working_hours.length === 7, "Add all seven working days", "settings"],
    [Boolean(business.default_language), "Choose a business language", "settings"],
    [Boolean(business.onboarding_submitted_at), "Confirm onboarding", "settings"],
    [ready > 0, "Add a ready knowledge document", "knowledge"],
  ] as const;
  return (
    <>
      <PageHeading
        title={`Welcome back to ${business.name}`}
        description="A truthful view of your current Sou2AI setup."
        action={
          business.is_active ? (
            <Link className="btn" to={`/businesses/${business.id}/chat`}>
              <Bot size={18} />
              Open AI Chat
            </Link>
          ) : undefined
        }
      />
      {unavailable.length > 0 && (
        <div className="overview-alert">
          <Alert>
            Some overview data could not be loaded ({unavailable.join(", ")}). Try
            refreshing this page.
          </Alert>
        </div>
      )}
      <div className="metrics-grid">
        <Metric
          icon={Activity}
          label="Lifecycle status"
          value={business.status.toLowerCase()}
        />
        <Metric
          icon={Check}
          label="Business profile"
          value={business.profile_complete ? "Complete" : "Incomplete"}
        />
        <Metric
          icon={BookOpen}
          label="Knowledge documents"
          value={
            !business.is_active
              ? "Unavailable while inactive"
              : documents === null
                ? "Loading…"
                : `${documents.length} total · ${ready} ready`
          }
        />
        <Metric
          icon={Clock3}
          label="AI token allowance"
          value={
            usage
              ? `${numberFormatter.format(usage.tokens_remaining)} remaining`
              : unavailable.includes("AI token allowance")
                ? "Unavailable"
                : business.is_active
                  ? "Loading…"
                  : "Available when active"
          }
        />
      </div>
      <div className="overview-grid">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>Setup checklist</h2>
              <p>
                {checklist.filter(([done]) => done).length} of {checklist.length}{" "}
                complete
              </p>
            </div>
          </div>
          <ul className="checklist">
            {checklist.map(([done, label, route]) => (
              <li key={label}>
                <span className={done ? "done" : ""}>{done ? <Check /> : null}</span>
                <span>{label}</span>
                {!done && (
                  <Link
                    aria-label={`Open ${label}`}
                    to={`/businesses/${business.id}/${route}`}
                  >
                    <ChevronRight />
                  </Link>
                )}
              </li>
            ))}
          </ul>
        </section>
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>Owner conversation</h2>
              <p>Only this business&apos;s private owner chat is shown.</p>
            </div>
            <Link className="text-link" to={`/businesses/${business.id}/conversations`}>
              View history
            </Link>
          </div>
          {messages === null ? (
            <Skeleton className="h-32" />
          ) : messages.length ? (
            <div className="recent-message">
              <MessageSquare />
              <div>
                <strong>
                  {messages.at(-1)?.role === "owner"
                    ? "Your latest message"
                    : "Latest Sou2AI response"}
                </strong>
                <p>{messages.at(-1)?.content}</p>
              </div>
            </div>
          ) : (
            <TruthfulEmpty
              icon={MessageSquare}
              title="No owner messages yet"
              text="Start a conversation after this business is active."
            />
          )}
        </section>
      </div>
    </>
  );
}

function Metric({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Activity;
  label: string;
  value: string;
}) {
  return (
    <article className="metric">
      <span>
        <Icon />
      </span>
      <div>
        <p>{label}</p>
        <strong>{value}</strong>
      </div>
    </article>
  );
}

function ChatPage({ business }: { business: Business }) {
  const location = useLocation();
  const selectedId = new URLSearchParams(location.search).get("conversation");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(business.is_active);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const composingRef = useRef(false);
  const messageScroll = usePinnedToBottom(messages, !loading, business.id, sending);
  const load = useCallback(async () => {
    if (!business.is_active) return;
    try {
      if (selectedId) {
        const [selected, history] = await Promise.all([
          api.conversation(business.id, selectedId),
          api.conversationMessages(business.id, selectedId),
        ]);
        setConversation(selected);
        setMessages(history.items);
      } else {
        setConversation(null);
        setMessages((await api.messages(business.id)).items);
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [business.id, business.is_active, selectedId]);
  useEffect(() => {
    void load();
  }, [load]);
  useLayoutEffect(() => {
    if (textareaRef.current) resizeComposer(textareaRef.current, text.length === 0);
  }, [text]);
  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = text.trim();
    if (!content || sending || conversation?.archived) return;
    setSending(true);
    setError("");
    try {
      if (selectedId) {
        await api.sendConversationMessage(business.id, selectedId, content);
      } else {
        await api.send(business.id, content);
      }
      setText("");
      await load();
    } catch (caught) {
      setError(ownerChatErrorMessage(caught));
      await load();
    } finally {
      setSending(false);
    }
  }
  return (
    <div className="chat-page">
      <PageHeading
        title="AI Chat"
        description={
          conversation
            ? `${conversation.title} · private owner conversation for ${business.name}.`
            : `Private owner conversation for ${business.name}.`
        }
        action={
          <Link
            className="btn-secondary"
            to={`/businesses/${business.id}/conversations`}
          >
            <MessageSquare size={18} />
            All conversations
          </Link>
        }
      />
      {!business.is_active ? (
        <Alert tone="info">
          AI Chat is available only while this business is active. Current status:{" "}
          {business.status.toLowerCase()}.
        </Alert>
      ) : (
        <section className="chat-panel" aria-busy={loading || sending}>
          <div
            ref={messageScroll.viewportRef}
            className="messages"
            role="log"
            aria-label="Owner chat messages"
            aria-live="polite"
            onScroll={messageScroll.onScroll}
          >
            {loading ? (
              <>
                <Skeleton className="h-20 max-w-xl" />
                <Skeleton className="ml-auto h-16 max-w-lg" />
              </>
            ) : messages.length ? (
              messages.map((message) => <Message key={message.id} message={message} />)
            ) : (
              <div className="chat-welcome">
                <span>
                  <Bot />
                </span>
                <h2>How can I help?</h2>
                <p>
                  Ask about your approved business profile, working hours, policies, or
                  ready knowledge documents.
                </p>
              </div>
            )}
            {sending && (
              <div className="generating">
                <LoaderCircle className="animate-spin" />
                <span>Sou2AI is generating a response…</span>
              </div>
            )}
          </div>
          <form onSubmit={send} className="composer">
            {error && <Alert>{error}</Alert>}
            {conversation?.archived && (
              <Alert tone="info">This conversation is archived and read-only.</Alert>
            )}
            <label>
              <span className="sr-only">Message Sou2AI</span>
              <textarea
                ref={textareaRef}
                name="message"
                rows={1}
                maxLength={4000}
                disabled={sending || conversation?.archived}
                value={text}
                onChange={(event) => setText(event.target.value)}
                onCompositionStart={() => {
                  composingRef.current = true;
                }}
                onCompositionEnd={() => {
                  composingRef.current = false;
                }}
                onKeyDown={(event) => {
                  if (
                    event.key !== "Enter" ||
                    event.shiftKey ||
                    composingRef.current ||
                    event.nativeEvent.isComposing
                  )
                    return;
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }}
                placeholder="Ask about your business knowledge…"
              />
            </label>
            <div>
              <span>{numberFormatter.format(text.length)} / 4,000</span>
              <button
                type="submit"
                className="btn"
                disabled={sending || !text.trim() || conversation?.archived}
                aria-label="Send message"
              >
                <Send size={18} />
                Send
              </button>
            </div>
          </form>
        </section>
      )}
    </div>
  );
}

function Message({ message }: { message: ChatMessage }) {
  return (
    <article className={`message ${message.role === "owner" ? "owner" : "assistant"}`}>
      <span className="message-avatar">
        {message.role === "owner" ? "You" : <Bot />}
      </span>
      <div>
        <div className="message-bubble">{message.content}</div>
        {message.role === "owner" && message.generation_state === "failed" && (
          <p role="status">Response failed. Send a new message to try again.</p>
        )}
        {message.role === "owner" && message.generation_state === "processing" && (
          <p role="status">Response is being generated.</p>
        )}
        {message.role === "owner" && message.generation_state === "pending" && (
          <p role="status">Response is pending.</p>
        )}
        {message.sources.length > 0 && (
          <div className="citations">
            <strong>Sources</strong>
            {message.sources.map((source, index) => (
              <div key={`${source.filename}-${index}`}>
                <FileText />{" "}
                <span>
                  {source.label || source.filename}
                  {source.page_start
                    ? ` · page ${source.page_start}${source.page_end && source.page_end !== source.page_start ? `–${source.page_end}` : ""}`
                    : ""}
                </span>
                {!source.available && <em>Unavailable</em>}
              </div>
            ))}
          </div>
        )}
        <time dateTime={message.created_at}>{formatDateTime(message.created_at)}</time>
      </div>
    </article>
  );
}

function ConversationsPage({ business }: { business: Business }) {
  const [channel, setChannel] = useState<"owner" | "whatsapp">("owner");
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [selected, setSelected] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"create" | "archive" | "">("");
  const [archiveTarget, setArchiveTarget] = useState<Conversation | null>(null);
  const selectedConversationId = selected?.id ?? null;
  const conversationScroll = usePinnedToBottom(
    messages,
    messages !== null,
    selected?.id ?? business.id,
  );
  const loadConversations = useCallback(async () => {
    if (channel !== "owner") return;
    try {
      const response = await api.conversations(business.id, true);
      setConversations(response.items);
      setSelected((current) => {
        if (current) {
          return response.items.find((item) => item.id === current.id) ?? null;
        }
        return (
          response.items.find((item) => !item.archived) ?? response.items[0] ?? null
        );
      });
    } catch (caught) {
      setConversations([]);
      setError(errorMessage(caught));
    }
  }, [business.id, channel]);
  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);
  useEffect(() => {
    if (channel !== "owner") return;
    if (!selectedConversationId) {
      setMessages([]);
      return;
    }
    setMessages(null);
    api
      .conversationMessages(business.id, selectedConversationId)
      .then((response) => setMessages(response.items))
      .catch((caught) => {
        setMessages([]);
        setError(errorMessage(caught));
      });
  }, [business.id, channel, selectedConversationId]);

  async function createNewConversation() {
    setBusy("create");
    setError("");
    try {
      const created = await api.createConversation(business.id);
      setConversations((items) => [created, ...(items ?? [])]);
      setSelected(created);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy("");
    }
  }

  async function archiveSelected() {
    if (!archiveTarget) return;
    setBusy("archive");
    setError("");
    try {
      const archived = await api.archiveConversation(business.id, archiveTarget.id);
      setConversations(
        (items) =>
          items?.map((item) => (item.id === archived.id ? archived : item)) ?? [],
      );
      setSelected(archived);
      setArchiveTarget(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy("");
    }
  }

  if (channel === "whatsapp") {
    return (
      <WhatsAppConversations
        business={business}
        onSelectOwner={() => setChannel("owner")}
      />
    );
  }

  return (
    <>
      <PageHeading
        title="Conversations"
        description="Manage private owner conversation history for this business."
        action={
          <button
            type="button"
            className="btn"
            disabled={busy === "create"}
            onClick={() => void createNewConversation()}
          >
            {busy === "create" ? (
              <LoaderCircle className="animate-spin" />
            ) : (
              <Plus size={18} />
            )}
            New conversation
          </button>
        }
      />
      <ConversationChannelTabs channel="owner" onChange={setChannel} />
      {error && <Alert>{error}</Alert>}
      {conversations === null ? (
        <div className="conversation-workspace">
          <Skeleton className="h-64" />
          <Skeleton className="h-64" />
        </div>
      ) : conversations.length ? (
        <div className="conversation-workspace">
          <aside className="panel conversation-index" aria-label="Owner conversations">
            <div className="panel-heading">
              <div>
                <h2>Private owner chats</h2>
                <p>Archived conversations remain readable.</p>
              </div>
              <span className="count-badge">{conversations.length}</span>
            </div>
            <div className="conversation-index-list">
              {conversations.map((conversation) => (
                <button
                  type="button"
                  key={conversation.id}
                  className={conversation.id === selected?.id ? "selected" : ""}
                  aria-pressed={conversation.id === selected?.id}
                  onClick={() => setSelected(conversation)}
                >
                  <span>
                    <strong>{conversation.title}</strong>
                    {conversation.archived && <em>Archived</em>}
                  </span>
                  <p>{conversation.latest_message_preview ?? "No messages yet"}</p>
                  <time
                    dateTime={conversation.last_message_at ?? conversation.created_at}
                  >
                    {formatDateTime(
                      conversation.last_message_at ?? conversation.created_at,
                    )}
                  </time>
                </button>
              ))}
            </div>
          </aside>
          <section className="panel conversation-history">
            {selected && (
              <div className="panel-heading conversation-detail-heading">
                <div>
                  <h2>{selected.title}</h2>
                  <p>
                    {selected.archived
                      ? "Archived · read-only private owner history"
                      : "Private owner web conversation"}
                  </p>
                </div>
                <div className="conversation-actions">
                  {!selected.archived && business.is_active && (
                    <Link
                      className="btn"
                      to={`/businesses/${business.id}/chat?conversation=${selected.id}`}
                    >
                      <MessageSquare size={17} />
                      Open chat
                    </Link>
                  )}
                  {!selected.archived && (
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => setArchiveTarget(selected)}
                    >
                      <Archive size={17} />
                      Archive
                    </button>
                  )}
                </div>
              </div>
            )}
            {messages === null ? (
              <Skeleton className="h-64" />
            ) : messages.length ? (
              <div
                ref={conversationScroll.viewportRef}
                className="conversation-list"
                role="log"
                aria-label="Owner conversation history"
                onScroll={conversationScroll.onScroll}
              >
                {messages.map((message) => (
                  <Message key={message.id} message={message} />
                ))}
              </div>
            ) : (
              <TruthfulEmpty
                icon={MessageSquare}
                title="No messages yet"
                text={
                  selected?.archived
                    ? "This archived conversation has no messages."
                    : "Open this conversation in AI Chat to send its first message."
                }
              />
            )}
          </section>
        </div>
      ) : (
        <section className="panel conversation-history">
          <TruthfulEmpty
            icon={MessageSquare}
            title="No conversations yet"
            text="Create a private owner conversation without replacing earlier history."
          />
        </section>
      )}
      {archiveTarget && (
        <div className="dialog-layer" role="presentation">
          <button
            type="button"
            className="dialog-scrim"
            aria-label="Cancel archive"
            onClick={() => setArchiveTarget(null)}
          />
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="archive-conversation-title"
            className="dialog"
          >
            <span className="dialog-danger">
              <Archive />
            </span>
            <h2 id="archive-conversation-title">Archive conversation?</h2>
            <p>
              <strong>{archiveTarget.title}</strong> will remain readable, but it can no
              longer receive new messages.
            </p>
            <div className="form-actions">
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setArchiveTarget(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn-danger"
                disabled={busy === "archive"}
                onClick={() => void archiveSelected()}
              >
                <BusyLabel busy={busy === "archive"} idle="Archive conversation" />
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function ConversationChannelTabs({
  channel,
  onChange,
}: {
  channel: "owner" | "whatsapp";
  onChange: (channel: "owner" | "whatsapp") => void;
}) {
  return (
    <div className="channel-tabs" role="tablist" aria-label="Conversation channel">
      <button
        type="button"
        role="tab"
        aria-selected={channel === "owner"}
        className={channel === "owner" ? "selected" : ""}
        onClick={() => onChange("owner")}
      >
        <MessageSquare size={17} /> Owner
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={channel === "whatsapp"}
        className={channel === "whatsapp" ? "selected" : ""}
        onClick={() => onChange("whatsapp")}
      >
        <Smartphone size={17} /> WhatsApp
      </button>
    </div>
  );
}

function WhatsAppConversations({
  business,
  onSelectOwner,
}: {
  business: Business;
  onSelectOwner: () => void;
}) {
  const [conversations, setConversations] = useState<CustomerConversation[] | null>(
    null,
  );
  const [selected, setSelected] = useState<CustomerConversation | null>(null);
  const [messages, setMessages] = useState<CustomerMessage[] | null>(null);
  const [reply, setReply] = useState("");
  const [confirmReply, setConfirmReply] = useState(false);
  const [busy, setBusy] = useState<"reply" | "handoff" | "resume" | "">("");
  const [error, setError] = useState("");
  const selectedId = selected?.id ?? null;
  const scroll = usePinnedToBottom(
    messages,
    messages !== null,
    selectedId ?? business.id,
  );

  const load = useCallback(async () => {
    setError("");
    try {
      const result = await api.customerConversations(business.id);
      setConversations(result.items);
      setSelected((current) =>
        current
          ? (result.items.find((item) => item.id === current.id) ?? null)
          : (result.items[0] ?? null),
      );
    } catch (caught) {
      setConversations([]);
      setError(errorMessage(caught));
    }
  }, [business.id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!selectedId) {
      setMessages([]);
      return;
    }
    setMessages(null);
    api
      .customerMessages(business.id, selectedId)
      .then((result) => setMessages(result.items))
      .catch((caught) => {
        setMessages([]);
        setError(errorMessage(caught));
      });
  }, [business.id, selectedId]);

  async function changeHandoff(handoff: boolean) {
    if (!selected) return;
    setBusy(handoff ? "handoff" : "resume");
    setError("");
    try {
      const updated = handoff
        ? await api.handoffCustomerConversation(business.id, selected.id)
        : await api.resumeCustomerConversation(business.id, selected.id);
      setSelected(updated);
      setConversations(
        (items) =>
          items?.map((item) => (item.id === updated.id ? updated : item)) ?? [],
      );
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy("");
    }
  }

  async function sendConfirmedReply() {
    if (!selected || !reply.trim()) return;
    setBusy("reply");
    setError("");
    try {
      const created = await api.sendCustomerReply(
        business.id,
        selected.id,
        reply.trim(),
      );
      setMessages((items) => [...(items ?? []), created]);
      setReply("");
      setConfirmReply(false);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <PageHeading
        title="Conversations"
        description="View customer-safe WhatsApp conversations and delivery states."
      />
      <ConversationChannelTabs
        channel="whatsapp"
        onChange={(next) => next === "owner" && onSelectOwner()}
      />
      {error && <Alert>{error}</Alert>}
      {conversations === null ? (
        <div className="conversation-workspace">
          <Skeleton className="h-64" />
          <Skeleton className="h-64" />
        </div>
      ) : conversations.length ? (
        <div className="conversation-workspace">
          <aside className="panel conversation-index" aria-label="WhatsApp customers">
            <div className="panel-heading">
              <div>
                <h2>WhatsApp customers</h2>
                <p>Customer identities stay masked.</p>
              </div>
              <span className="count-badge">{conversations.length}</span>
            </div>
            <div className="conversation-index-list">
              {conversations.map((conversation) => (
                <button
                  type="button"
                  key={conversation.id}
                  className={conversation.id === selected?.id ? "selected" : ""}
                  aria-pressed={conversation.id === selected?.id}
                  onClick={() => setSelected(conversation)}
                >
                  <span>
                    <strong>{conversation.masked_customer_label}</strong>
                    <em>
                      {conversation.state === "HUMAN_HANDOFF" ? "Handoff" : "AI active"}
                    </em>
                  </span>
                  <p>{conversation.latest_message_preview ?? "No messages yet"}</p>
                  <time
                    dateTime={conversation.last_message_at ?? conversation.created_at}
                  >
                    {formatDateTime(
                      conversation.last_message_at ?? conversation.created_at,
                    )}
                  </time>
                </button>
              ))}
            </div>
          </aside>
          <section className="panel conversation-history customer-history">
            {selected && (
              <div className="panel-heading conversation-detail-heading">
                <div>
                  <h2>{selected.masked_customer_label}</h2>
                  <p>
                    {selected.state === "HUMAN_HANDOFF"
                      ? "Human handoff · AI replies paused"
                      : "AI active · customer-visible knowledge only"}
                  </p>
                </div>
                <div className="conversation-actions">
                  {selected.state === "HUMAN_HANDOFF" ? (
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={Boolean(busy)}
                      onClick={() => void changeHandoff(false)}
                    >
                      <BusyLabel busy={busy === "resume"} idle="Resume AI" />
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={Boolean(busy)}
                      onClick={() => void changeHandoff(true)}
                    >
                      <BusyLabel busy={busy === "handoff"} idle="Enter handoff" />
                    </button>
                  )}
                </div>
              </div>
            )}
            {messages === null ? (
              <Skeleton className="h-64" />
            ) : messages.length ? (
              <div
                ref={scroll.viewportRef}
                className="conversation-list customer-message-list"
                role="log"
                aria-label="WhatsApp conversation history"
                onScroll={scroll.onScroll}
              >
                {messages.map((message) => (
                  <article
                    key={message.id}
                    className={`customer-message customer-message-${message.direction}`}
                  >
                    <div>
                      <strong>
                        {message.sender === "customer"
                          ? selected?.masked_customer_label
                          : message.sender === "owner"
                            ? "Owner"
                            : "Sou2AI"}
                      </strong>
                      <p>{message.content}</p>
                      <span>
                        {message.direction === "outbound"
                          ? message.status.toLowerCase().replace("_", " ")
                          : "received"}
                      </span>
                      <time dateTime={message.created_at}>
                        {formatDateTime(message.created_at)}
                      </time>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <TruthfulEmpty
                icon={Smartphone}
                title="No WhatsApp messages"
                text="Verified inbound text messages will appear here."
              />
            )}
            {selected && (
              <div className="customer-reply-composer">
                <label className="field-label">
                  Manual reply
                  <textarea
                    className="input"
                    rows={3}
                    maxLength={4000}
                    value={reply}
                    placeholder="Write a customer-safe text reply"
                    onChange={(event) => setReply(event.target.value)}
                  />
                </label>
                <button
                  type="button"
                  className="btn"
                  disabled={!reply.trim() || Boolean(busy)}
                  onClick={() => setConfirmReply(true)}
                >
                  <Send size={17} /> Send manual reply
                </button>
              </div>
            )}
          </section>
        </div>
      ) : (
        <section className="panel conversation-history">
          <TruthfulEmpty
            icon={Smartphone}
            title="No WhatsApp conversations"
            text="Connect and activate WhatsApp in Business Settings. Verified customer messages will appear here."
          />
        </section>
      )}
      {confirmReply && selected && (
        <div className="dialog-layer" role="presentation">
          <button
            type="button"
            className="dialog-scrim"
            aria-label="Cancel manual reply"
            onClick={() => setConfirmReply(false)}
          />
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="manual-reply-title"
            className="dialog"
          >
            <span className="dialog-danger">
              <Send />
            </span>
            <h2 id="manual-reply-title">Send this WhatsApp reply?</h2>
            <p>
              This explicitly sends an external text message to{" "}
              {selected.masked_customer_label}.
            </p>
            <div className="dialog-actions">
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setConfirmReply(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy === "reply"}
                onClick={() => void sendConfirmedReply()}
              >
                <BusyLabel busy={busy === "reply"} idle="Confirm and send" />
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function KnowledgePage({ business }: { business: Business }) {
  const [documents, setDocuments] = useState<Document[] | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Document | null>(null);
  const deleteDialog = useRef<HTMLDivElement>(null);
  const deleteCancel = useRef<HTMLButtonElement>(null);
  const load = useCallback(async () => {
    if (!business.is_active) {
      setDocuments([]);
      return;
    }
    try {
      setDocuments(await api.documents(business.id));
    } catch (caught) {
      setError(errorMessage(caught));
      setDocuments([]);
    }
  }, [business.id, business.is_active]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (
      !documents?.some((document) =>
        ["PENDING", "PROCESSING"].includes(document.status),
      )
    )
      return;
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [documents, load]);
  useEffect(() => {
    if (!deleteTarget) return;
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement as HTMLElement | null;
    document.body.style.overflow = "hidden";
    deleteCancel.current?.focus();
    const handleKeys = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDeleteTarget(null);
      if (event.key !== "Tab") return;
      const focusable = deleteDialog.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
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
    };
    document.addEventListener("keydown", handleKeys);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKeys);
      previousFocus?.focus();
    };
  }, [deleteTarget]);
  async function act(
    key: string,
    action: () => Promise<unknown>,
    successMessage?: string,
  ) {
    setBusy(key);
    setError("");
    setSuccess("");
    try {
      await action();
      await load();
      if (successMessage) setSuccess(successMessage);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy("");
    }
  }
  return (
    <>
      <PageHeading
        title="Knowledge Base"
        description="Manage private PDF, DOCX, and TXT documents for this business."
      />
      {!business.is_active ? (
        <Alert tone="info">
          Knowledge documents are available only while this business is active.
        </Alert>
      ) : (
        <>
          <section className="upload-panel">
            <div>
              <span>
                <Upload />
              </span>
              <div>
                <h2>Upload a document</h2>
                <p>PDF, DOCX, or UTF-8 TXT. Files remain private and tenant-scoped.</p>
              </div>
            </div>
            <div className="upload-controls">
              <label className="file-picker">
                <input
                  name="document"
                  type="file"
                  accept=".pdf,.docx,.txt"
                  disabled={busy === "upload"}
                  onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                />
                <span>{file?.name ?? "Choose a file"}</span>
              </label>
              <button
                type="button"
                className="btn"
                disabled={!file || busy === "upload"}
                onClick={() =>
                  file &&
                  void act(
                    "upload",
                    async () => {
                      await api.upload(business.id, file);
                      setFile(null);
                    },
                    "Document uploaded and queued for processing.",
                  )
                }
              >
                <BusyLabel busy={busy === "upload"} idle="Upload document" />
              </button>
            </div>
          </section>
          {error && (
            <div className="mt-5">
              <Alert>{error}</Alert>
            </div>
          )}
          {success && (
            <div className="mt-5">
              <Alert tone="success">{success}</Alert>
            </div>
          )}
          <section className="panel documents-panel">
            <div className="panel-heading">
              <div>
                <h2>Documents</h2>
                <p>Processing status updates automatically.</p>
              </div>
              <span className="count-badge">{documents?.length ?? 0}</span>
            </div>
            {documents === null ? (
              <Skeleton className="h-64" />
            ) : documents.length ? (
              <div className="document-list">
                {documents.map((document) => (
                  <article key={document.id} className="document-row">
                    <span className="document-icon">
                      <FileText />
                    </span>
                    <div className="document-name">
                      <strong>{document.original_filename}</strong>
                      <span>
                        {formatBytes(document.file_size_bytes)}
                        {document.page_count ? ` · ${document.page_count} pages` : ""} ·
                        Added {formatDate(document.created_at)}
                      </span>
                      {document.failure_code && (
                        <em>Processing failed: {document.failure_code}</em>
                      )}
                    </div>
                    <DocumentStatus status={document.status} />
                    <div className="document-actions">
                      <button
                        type="button"
                        className="btn-secondary"
                        disabled={Boolean(busy)}
                        onClick={() =>
                          void act(
                            document.id,
                            () =>
                              api.setDocumentCustomerVisibility(
                                business.id,
                                document.id,
                                !document.customer_visible,
                              ),
                            document.customer_visible
                              ? "Document is private again."
                              : "Document is now approved for customer answers.",
                          )
                        }
                      >
                        {document.customer_visible ? "Customer-visible" : "Private"}
                      </button>
                      {document.status === "FAILED" && (
                        <button
                          type="button"
                          className="btn-secondary"
                          disabled={Boolean(busy)}
                          onClick={() =>
                            void act(
                              document.id,
                              () => api.retryDocument(business.id, document.id),
                              "Document queued for another processing attempt.",
                            )
                          }
                        >
                          Retry
                        </button>
                      )}
                      <label className="btn-secondary" aria-disabled={Boolean(busy)}>
                        Replace
                        <input
                          name={`replacement_${document.id}`}
                          className="sr-only"
                          type="file"
                          accept=".pdf,.docx,.txt"
                          disabled={Boolean(busy)}
                          onChange={(event) => {
                            const replacement = event.target.files?.[0];
                            if (replacement)
                              void act(
                                document.id,
                                () =>
                                  api.replaceDocument(
                                    business.id,
                                    document.id,
                                    replacement,
                                  ),
                                "Replacement uploaded and queued for processing.",
                              );
                          }}
                        />
                      </label>
                      <button
                        type="button"
                        className="icon-button danger"
                        aria-label={`Delete ${document.original_filename}`}
                        onClick={() => setDeleteTarget(document)}
                      >
                        <Trash2 />
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <TruthfulEmpty
                icon={BookOpen}
                title="No knowledge documents"
                text="Upload the first approved document for this business."
              />
            )}
          </section>
        </>
      )}
      {deleteTarget && (
        <div className="dialog-layer" role="presentation">
          <button
            type="button"
            className="dialog-scrim"
            aria-label="Cancel deletion"
            onClick={() => setDeleteTarget(null)}
          />
          <div
            ref={deleteDialog}
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="delete-title"
            className="dialog"
            tabIndex={-1}
          >
            <span className="dialog-danger">
              <AlertCircle />
            </span>
            <h2 id="delete-title">Delete document?</h2>
            <p>
              <strong>{deleteTarget.original_filename}</strong> and its processed
              knowledge will be removed. This cannot be undone.
            </p>
            <div className="form-actions">
              <button
                type="button"
                ref={deleteCancel}
                className="btn-secondary"
                onClick={() => setDeleteTarget(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn-danger"
                disabled={busy === "delete"}
                onClick={() =>
                  void act(
                    "delete",
                    async () => {
                      await api.deleteDocument(business.id, deleteTarget.id);
                      setDeleteTarget(null);
                    },
                    "Document deleted.",
                  )
                }
              >
                Delete document
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function DocumentStatus({ status }: { status: Document["status"] }) {
  return (
    <span role="status" className={`document-status document-${status.toLowerCase()}`}>
      {["PENDING", "PROCESSING"].includes(status) && (
        <LoaderCircle aria-hidden="true" className="animate-spin" />
      )}
      {status.toLowerCase()}
    </span>
  );
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

const dateFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const numberFormatter = new Intl.NumberFormat();
const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function formatDate(value: string) {
  return dateFormatter.format(new Date(value));
}

function formatDateTime(value: string) {
  return dateTimeFormatter.format(new Date(value));
}

function WhatsAppSettings({ business }: { business: Business }) {
  const [connection, setConnection] = useState<WhatsAppConnection | null | undefined>(
    undefined,
  );
  const [displayName, setDisplayName] = useState("Customer WhatsApp");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [confirmDisable, setConfirmDisable] = useState(false);

  const load = useCallback(async () => {
    try {
      const rows = await api.whatsAppConnections(business.id);
      setConnection(rows[0] ?? null);
    } catch (caught) {
      setConnection(null);
      setError(errorMessage(caught));
    }
  }, [business.id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(
    name: string,
    action: () => Promise<WhatsAppConnection>,
    message: string,
  ) {
    setBusy(name);
    setError("");
    setSuccess("");
    try {
      setConnection(await action());
      setSuccess(message);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="panel whatsapp-settings">
      <div className="panel-heading">
        <div>
          <h2>WhatsApp customer messaging</h2>
          <p>Meta WhatsApp Cloud API · text only · deployment-managed credentials.</p>
        </div>
        {connection && (
          <span
            className={`channel-status channel-status-${connection.status.toLowerCase()}`}
          >
            {connection.status.toLowerCase()}
          </span>
        )}
      </div>
      {connection === undefined ? (
        <Skeleton className="h-24" />
      ) : connection === null ? (
        <div className="whatsapp-connect-grid">
          <div>
            <strong>Connect the supported WhatsApp source</strong>
            <p>
              Sou2AI accepts only the allowlisted Meta profile. Tokens, app secrets,
              phone numbers, arbitrary URLs, and webhook credentials are never entered
              here.
            </p>
          </div>
          <label className="field-label">
            Safe display name
            <input
              className="input"
              minLength={2}
              maxLength={120}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </label>
          <label className="field-label">
            Connection profile
            <select className="input" value="meta_whatsapp_cloud" disabled>
              <option value="meta_whatsapp_cloud">Meta WhatsApp Cloud</option>
            </select>
          </label>
          <button
            type="button"
            className="btn"
            disabled={busy === "connect" || displayName.trim().length < 2}
            onClick={() =>
              void act(
                "connect",
                () => api.configureWhatsApp(business.id, displayName.trim()),
                "WhatsApp configuration created. Validate it before activation.",
              )
            }
          >
            <BusyLabel busy={busy === "connect"} idle="Connect WhatsApp" />
          </button>
        </div>
      ) : (
        <div className="whatsapp-connection-detail">
          <div className="whatsapp-connection-meta">
            <div>
              <span>Display name</span>
              <strong>{connection.display_name}</strong>
            </div>
            <div>
              <span>Last validation</span>
              <strong>
                {connection.last_validated_at
                  ? formatDateTime(connection.last_validated_at)
                  : "Not validated"}
              </strong>
            </div>
            <div>
              <span>Last healthy check</span>
              <strong>
                {connection.last_successful_health_check_at
                  ? formatDateTime(connection.last_successful_health_check_at)
                  : "No successful check"}
              </strong>
            </div>
          </div>
          {connection.failure_code && (
            <Alert>Connection check failed safely: {connection.failure_code}</Alert>
          )}
          <div className="whatsapp-actions">
            {connection.status !== "ACTIVE" && (
              <button
                type="button"
                className="btn-secondary"
                disabled={Boolean(busy)}
                onClick={() =>
                  void act(
                    "validate",
                    () => api.validateWhatsApp(business.id, connection.id),
                    "Connection profile validated.",
                  )
                }
              >
                <BusyLabel busy={busy === "validate"} idle="Validate" />
              </button>
            )}
            {connection.status === "VALIDATED" && (
              <button
                type="button"
                className="btn"
                disabled={Boolean(busy) || !business.is_active}
                onClick={() =>
                  void act(
                    "activate",
                    () => api.activateWhatsApp(business.id, connection.id),
                    "WhatsApp customer messaging activated.",
                  )
                }
              >
                <BusyLabel busy={busy === "activate"} idle="Activate" />
              </button>
            )}
            {connection.status === "ACTIVE" && (
              <>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={Boolean(busy)}
                  onClick={() =>
                    void act(
                      "health",
                      () => api.checkWhatsApp(business.id, connection.id),
                      "Connection check completed.",
                    )
                  }
                >
                  <RefreshCw size={16} /> Test connection
                </button>
                <label className="auto-reply-toggle">
                  <input
                    type="checkbox"
                    checked={connection.auto_reply_enabled}
                    disabled={Boolean(busy)}
                    onChange={(event) =>
                      void act(
                        "auto-reply",
                        () =>
                          api.setWhatsAppAutoReply(
                            business.id,
                            connection.id,
                            event.target.checked,
                          ),
                        event.target.checked
                          ? "Automatic replies enabled."
                          : "Automatic replies paused.",
                      )
                    }
                  />
                  Automatic customer replies
                </label>
              </>
            )}
            {connection.status !== "DISABLED" && (
              <button
                type="button"
                className="btn-quiet-danger"
                disabled={Boolean(busy)}
                onClick={() => setConfirmDisable(true)}
              >
                Disable
              </button>
            )}
          </div>
          <p className="safe-note">
            Customer answers use only public profile fields and explicitly
            customer-visible knowledge. Owner memory and operational tools stay private.
          </p>
        </div>
      )}
      {error && <Alert>{error}</Alert>}
      {success && <Alert tone="success">{success}</Alert>}
      {confirmDisable && connection && (
        <div className="dialog-layer" role="presentation">
          <button
            type="button"
            className="dialog-scrim"
            aria-label="Cancel WhatsApp disable"
            onClick={() => setConfirmDisable(false)}
          />
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="disable-whatsapp-title"
            className="dialog"
          >
            <span className="dialog-danger">
              <AlertCircle />
            </span>
            <h2 id="disable-whatsapp-title">Disable WhatsApp?</h2>
            <p>Inbound events will remain safely acknowledged without AI replies.</p>
            <div className="dialog-actions">
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setConfirmDisable(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn-quiet-danger"
                onClick={() => {
                  setConfirmDisable(false);
                  void act(
                    "disable",
                    () => api.disableWhatsApp(business.id, connection.id),
                    "WhatsApp customer messaging disabled.",
                  );
                }}
              >
                Disable WhatsApp
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function BusinessSettingsPage({
  business,
  onSaved,
}: {
  business: Business;
  onSaved: (business: Business) => void;
}) {
  const [form, setForm] = useState({
    name: business.name,
    category: business.category ?? "",
    custom_category: business.custom_category ?? "",
    description: business.description ?? "",
    default_language: business.default_language ?? "",
    governorate: business.governorate ?? "",
    district: business.district ?? "",
    city: business.city ?? "",
    address_line: business.address_line ?? "",
    working_hours: business.working_hours,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const districts = LOCATIONS[form.governorate] ?? {};
  const cities = districts[form.district] ?? [];
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const saved = await api.updateBusiness(business.id, {
        ...form,
        custom_category: form.category === "OTHER" ? form.custom_category : null,
      });
      onSaved(saved);
      setSuccess("Business settings saved.");
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageHeading
        title="Business Settings"
        description="Update supported profile, location, language, and working-hours fields."
      />
      <div className="settings-status">
        <div>
          <span>Lifecycle status</span>
          <StatusBadge status={business.status} />
        </div>
        <p>Lifecycle is read-only. Confirmation does not activate a business.</p>
      </div>
      <WhatsAppSettings business={business} />
      <form onSubmit={submit} className="settings-form">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>Business profile</h2>
              <p>Use accurate details that apply to this tenant.</p>
            </div>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="field-label">
              Business name
              <input
                required
                name="name"
                minLength={2}
                maxLength={120}
                className="input"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
              />
            </label>
            <label className="field-label">
              Category
              <select
                required
                name="category"
                className="input"
                value={form.category}
                onChange={(event) => setForm({ ...form, category: event.target.value })}
              >
                <option value="">Select category</option>
                {CATEGORIES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            {form.category === "OTHER" && (
              <label className="field-label sm:col-span-2">
                Custom category
                <input
                  required
                  name="custom_category"
                  minLength={2}
                  maxLength={100}
                  className="input"
                  value={form.custom_category}
                  onChange={(event) =>
                    setForm({ ...form, custom_category: event.target.value })
                  }
                />
              </label>
            )}
            <label className="field-label">
              Default language
              <select
                required
                name="default_language"
                className="input"
                value={form.default_language}
                onChange={(event) =>
                  setForm({ ...form, default_language: event.target.value })
                }
              >
                <option value="">Select language</option>
                <option value="en">English</option>
                <option value="ar">Arabic</option>
              </select>
            </label>
            <label className="field-label sm:col-span-2">
              Description
              <textarea
                required
                name="description"
                minLength={20}
                maxLength={2000}
                className="input min-h-32 resize-y"
                value={form.description}
                onChange={(event) =>
                  setForm({ ...form, description: event.target.value })
                }
              />
            </label>
          </div>
        </section>
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>Location</h2>
              <p>Select one supported Lebanese location.</p>
            </div>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="field-label">
              Governorate
              <select
                required
                name="governorate"
                className="input"
                value={form.governorate}
                onChange={(event) =>
                  setForm({
                    ...form,
                    governorate: event.target.value,
                    district: "",
                    city: "",
                  })
                }
              >
                <option value="">Select governorate</option>
                {Object.keys(LOCATIONS).map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <label className="field-label">
              District
              <select
                required
                name="district"
                disabled={!form.governorate}
                className="input"
                value={form.district}
                onChange={(event) =>
                  setForm({ ...form, district: event.target.value, city: "" })
                }
              >
                <option value="">Select district</option>
                {Object.keys(districts).map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <label className="field-label">
              City or area
              <select
                required
                name="city"
                disabled={!form.district}
                className="input"
                value={form.city}
                onChange={(event) => setForm({ ...form, city: event.target.value })}
              >
                <option value="">Select city or area</option>
                {cities.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <label className="field-label">
              Street address
              <input
                required
                name="address_line"
                minLength={5}
                maxLength={255}
                className="input"
                value={form.address_line}
                onChange={(event) =>
                  setForm({ ...form, address_line: event.target.value })
                }
              />
            </label>
          </div>
        </section>
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>Working hours</h2>
              <p>Closed days have no shifts. Open days support up to three.</p>
            </div>
          </div>
          <ScheduleEditor
            value={form.working_hours}
            onChange={(working_hours: WorkingDay[]) =>
              setForm({ ...form, working_hours })
            }
          />
        </section>
        {error && <Alert>{error}</Alert>}
        {success && <Alert tone="success">{success}</Alert>}
        <div className="settings-save">
          <button type="submit" disabled={busy} className="btn">
            <BusyLabel busy={busy} idle="Save business settings" />
          </button>
        </div>
      </form>
    </>
  );
}

function FuturePage({
  icon: Icon,
  title,
  description,
  text,
}: {
  icon: typeof BarChart3;
  title: string;
  description: string;
  text: string;
}) {
  return (
    <>
      <PageHeading title={title} description={description} />
      <section className="future-state">
        <span>
          <Icon />
        </span>
        <p className="eyebrow">Not connected</p>
        <h2>{title} will appear here when supported</h2>
        <p>{text}</p>
      </section>
    </>
  );
}

function TruthfulEmpty({
  icon: Icon,
  title,
  text,
}: {
  icon: typeof MessageSquare;
  title: string;
  text: string;
}) {
  return (
    <div className="truthful-empty">
      <span>
        <Icon />
      </span>
      <div>
        <h3>{title}</h3>
        <p>{text}</p>
      </div>
    </div>
  );
}
