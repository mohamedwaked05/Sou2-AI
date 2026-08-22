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
  status: "PENDING" | "PROCESSING" | "READY" | "FAILED";
  failure_code: string | null;
  page_count: number | null;
  created_at: string;
}
export interface ChatMessage {
  id: string;
  role: "owner" | "assistant";
  content: string;
  created_at: string;
  sources: {
    label: string;
    filename: string;
    page_start: number | null;
    page_end: number | null;
    section_title: string | null;
    available: boolean;
  }[];
}
export interface Usage {
  daily_token_allowance: number;
  tokens_remaining: number;
  usage_percentage: number;
  reset_at: string;
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
const base = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";
let accessToken: string | null = null;
export const setAccessToken = (token: string | null) => {
  accessToken = token;
};
async function refresh() {
  const r = await fetch(`${base}/auth/refresh`, {
    method: "POST",
    credentials: "include",
  });
  if (!r.ok) return false;
  const body = (await r.json()) as { access_token: string };
  accessToken = body.access_token;
  return true;
}
async function request<T>(
  path: string,
  init: RequestInit = {},
  retry = true,
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
    retry &&
    !path.endsWith("/auth/refresh") &&
    (await refresh())
  )
    return request<T>(path, init, false);
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      error?: { code?: string; message?: string };
    } | null;
    throw new ApiError(response.status, body?.error?.code, body?.error?.message);
  }
  return response.status === 204 ? (undefined as T) : (response.json() as Promise<T>);
}
export const api = {
  refresh: async () => {
    if (!(await refresh()))
      throw new ApiError(401, "refresh_token_invalid", "Your session has ended.");
  },
  login: (body: { email: string; password: string; keep_me_signed_in: boolean }) =>
    request<{ access_token: string }>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  register: (body: object) =>
    request<{ message: string }>("/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  verify: (token: string) =>
    request<{ message: string }>("/auth/verify-email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    }),
  resend: (email: string) =>
    request<{ message: string }>("/auth/resend-verification", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    }),
  forgot: (email: string) =>
    request<{ message: string }>("/auth/forgot-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    }),
  reset: (body: object) =>
    request<{ message: string }>("/auth/reset-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  me: () => request<User>("/auth/me"),
  logout: () => request("/auth/logout", { method: "POST" }),
  changePassword: (body: object) =>
    request<{ message: string }>("/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  businesses: () => request<Business[]>("/businesses"),
  business: (id: string) => request<Business>(`/businesses/${id}`),
  createBusiness: (name: string) =>
    request<Business>("/businesses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  updateBusiness: (id: string, body: object) =>
    request<Business>(`/businesses/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  confirm: (id: string) =>
    request<Business>(`/businesses/${id}/onboarding/confirm`, { method: "POST" }),
  messages: (id: string) =>
    request<{ items: ChatMessage[] }>(`/businesses/${id}/owner-chat/messages`),
  send: (id: string, content: string) =>
    request(`/businesses/${id}/owner-chat/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, idempotency_key: crypto.randomUUID() }),
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
