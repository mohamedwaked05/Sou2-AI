export type Language = "ar" | "en";
export type BusinessStatus = "PENDING" | "ACTIVE" | "DISABLED";

export interface User {
  id: string;
  email: string;
  first_name: string;
  last_name: string;
  email_verified_at: string | null;
  status: string;
}

export interface Shift {
  start: string;
  end: string;
}
export interface WorkingDay {
  weekday: string;
  is_closed: boolean;
  shifts: Shift[];
}

export interface Business {
  id: string;
  name: string;
  description: string | null;
  category: string | null;
  custom_category: string | null;
  default_language: Language | null;
  governorate: string | null;
  district: string | null;
  city: string | null;
  address_line: string | null;
  status: BusinessStatus;
  is_active: boolean;
  profile_complete: boolean;
  first_incomplete_section: string | null;
  onboarding_submitted_at: string | null;
  working_hours: WorkingDay[];
}

export interface Document {
  id: string;
  original_filename: string;
  mime_type: string;
  file_size_bytes: number;
  status: "PENDING" | "PROCESSING" | "READY" | "FAILED";
  failure_code: string | null;
  page_count: number | null;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  sequence_number: number;
  role: "owner" | "assistant";
  content: string;
  created_at: string;
  sources: {
    label: string;
    document_id: string | null;
    filename: string;
    page_start: number | null;
    page_end: number | null;
    section_title: string | null;
    available: boolean;
  }[];
}

export interface Usage {
  window_start: string;
  window_end: string;
  reset_at: string;
  daily_token_allowance: number;
  owner_reserved_tokens: number;
  input_tokens_used: number;
  output_tokens_used: number;
  total_tokens_used: number;
  tokens_currently_reserved: number;
  tokens_remaining: number;
  usage_percentage: number;
  status: "normal" | "approaching_limit" | "nearly_exhausted" | "exhausted";
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code = "request_failed",
    message = "Something went wrong. Please try again.",
  ) {
    super(message);
  }
}

export function ownerChatErrorMessage(error: unknown) {
  if (!(error instanceof ApiError)) {
    return "We couldn't reach the assistant. Please try again.";
  }
  const messages: Record<string, string> = {
    assistant_rate_limited:
      "The assistant is handling too many requests right now. Please try again later.",
    assistant_timeout: "The assistant took too long to respond. Please try again.",
    assistant_transport_failure:
      "The assistant can't be reached right now. Please try again shortly.",
    assistant_invalid_response:
      "The assistant couldn't produce a usable response. Please try again.",
  };
  return messages[error.code] ?? error.message;
}

const configuredBase = import.meta.env.VITE_API_BASE_URL?.trim();
const base = (configuredBase || "/api/v1").replace(/\/$/, "");
let accessToken: string | null = null;
let refreshInFlight: Promise<boolean> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

async function parseError(response: Response) {
  const body = (await response.json().catch(() => null)) as {
    error?: { code?: string; message?: string };
  } | null;
  return new ApiError(response.status, body?.error?.code, body?.error?.message);
}

async function performRefresh(): Promise<boolean> {
  const response = await fetch(`${base}/auth/refresh`, {
    method: "POST",
    credentials: "include",
  });
  if (!response.ok) {
    accessToken = null;
    return false;
  }
  const body = (await response.json()) as { access_token: string };
  accessToken = body.access_token;
  return true;
}

function refreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = performRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  options: { retryAuth?: boolean } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  if (
    response.status === 401 &&
    (options.retryAuth ?? true) &&
    (await refreshSession())
  ) {
    return request<T>(path, init, { retryAuth: false });
  }
  if (!response.ok) throw await parseError(response);
  return response.status === 204 ? (undefined as T) : (response.json() as Promise<T>);
}

function json(body: unknown): RequestInit {
  return {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export const api = {
  restoreSession: async () => {
    if (!(await refreshSession())) {
      throw new ApiError(401, "refresh_token_invalid", "Your session has ended.");
    }
    return request<User>("/auth/me", {}, { retryAuth: false });
  },
  login: async (body: {
    email: string;
    password: string;
    keep_me_signed_in: boolean;
  }) => {
    const response = await request<{ access_token: string }>(
      "/auth/login",
      { method: "POST", ...json(body) },
      { retryAuth: false },
    );
    accessToken = response.access_token;
    return response;
  },
  register: (body: object) =>
    request<{ message: string }>(
      "/auth/register",
      { method: "POST", ...json(body) },
      { retryAuth: false },
    ),
  verify: (token: string) =>
    request<{ message: string }>(
      "/auth/verify-email",
      { method: "POST", ...json({ token }) },
      { retryAuth: false },
    ),
  resend: (email: string) =>
    request<{ message: string }>(
      "/auth/resend-verification",
      { method: "POST", ...json({ email }) },
      { retryAuth: false },
    ),
  forgot: (email: string) =>
    request<{ message: string }>(
      "/auth/forgot-password",
      { method: "POST", ...json({ email }) },
      { retryAuth: false },
    ),
  reset: (body: object) =>
    request<{ message: string }>(
      "/auth/reset-password",
      { method: "POST", ...json(body) },
      { retryAuth: false },
    ),
  me: () => request<User>("/auth/me"),
  logout: async () => {
    try {
      await request<{ message: string }>(
        "/auth/logout",
        { method: "POST" },
        { retryAuth: false },
      );
    } finally {
      accessToken = null;
    }
  },
  changePassword: (body: object) =>
    request<{ message: string }>("/auth/change-password", {
      method: "POST",
      ...json(body),
    }),
  businesses: () => request<Business[]>("/businesses"),
  business: (id: string) => request<Business>(`/businesses/${id}`),
  createBusiness: (name: string) =>
    request<Business>("/businesses", { method: "POST", ...json({ name }) }),
  updateBusiness: (id: string, body: object) =>
    request<Business>(`/businesses/${id}`, { method: "PATCH", ...json(body) }),
  confirm: (id: string) =>
    request<Business>(`/businesses/${id}/onboarding/confirm`, { method: "POST" }),
  messages: (id: string) =>
    request<{ items: ChatMessage[]; next_cursor: string | null }>(
      `/businesses/${id}/owner-chat/messages`,
    ),
  send: (id: string, content: string) =>
    request(`/businesses/${id}/owner-chat/messages`, {
      method: "POST",
      ...json({ content, idempotency_key: crypto.randomUUID() }),
    }),
  usage: (id: string) => request<Usage>(`/businesses/${id}/ai-usage/current`),
  documents: (id: string) =>
    request<Document[]>(`/businesses/${id}/knowledge/documents`),
  upload: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Document>(`/businesses/${id}/knowledge/documents`, {
      method: "POST",
      body: form,
    });
  },
  retryDocument: (business: string, document: string) =>
    request<Document>(`/businesses/${business}/knowledge/documents/${document}/retry`, {
      method: "POST",
    }),
  replaceDocument: (business: string, document: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Document>(
      `/businesses/${business}/knowledge/documents/${document}/replacement`,
      { method: "POST", body: form },
    );
  },
  deleteDocument: (business: string, document: string) =>
    request<void>(`/businesses/${business}/knowledge/documents/${document}`, {
      method: "DELETE",
    }),
};

export function resetApiSessionForTests() {
  accessToken = null;
  refreshInFlight = null;
}
