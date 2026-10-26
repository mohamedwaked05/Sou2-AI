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
  customer_visible: boolean;
}

export interface ChatMessage {
  id: string;
  sequence_number: number;
  role: "owner" | "assistant";
  content: string;
  created_at: string;
  reply_to_message_id: string | null;
  generation_state: "pending" | "processing" | "completed" | "failed" | null;
  sources: {
    label: string;
    document_id: string | null;
    filename: string;
    page_start: number | null;
    page_end: number | null;
    section_title: string | null;
    available: boolean;
    excerpt?: string | null;
  }[];
}

export interface Conversation {
  id: string;
  creator_user_id: string;
  channel: "owner_web";
  title: string;
  next_turn_number: number;
  last_message_at: string | null;
  latest_message_preview: string | null;
  archived: boolean;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
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

export type DataSourceStatus =
  "CONFIGURED" | "VALIDATED" | "ACTIVE" | "UNHEALTHY" | "DISABLED";

export interface OperationalMappingProfile {
  key: string;
  version: number;
  display_name: string;
  completed_sale_statuses: string[];
  excluded_sale_statuses: string[];
  return_treatment: string;
  active_reservation_statuses: string[];
  reservation_treatment: string;
  branch_meaning: string;
  warehouse_meaning: string;
  quantity_interpretation: string;
  revenue_interpretation: string;
  currency: string;
  source_timezone: string;
}

export interface ConnectionProfile {
  key: string;
  display_name: string;
  description: string;
  adapter_type: string;
  mapping: OperationalMappingProfile | null;
  capabilities: string[];
  discovery_required?: boolean;
}

export interface DataSource {
  catalogue_coverage?: CatalogueCoverage | null;
  quality_approval_status?: string | null;
  catalogue_approval_expires_at?: string | null;
  id: string;
  display_name: string;
  adapter_type: string;
  connection_profile_key: string;
  mapping: OperationalMappingProfile | null;
  mapping_version?: number | null;
  status: DataSourceStatus;
  last_validated_at: string | null;
  last_successful_health_check_at: string | null;
  failure_code: string | null;
  capabilities: string[];
  created_at: string;
  updated_at: string;
}

export function catalogueApprovalStatus(source: DataSource, now = Date.now()) {
  const expiry =
    source.catalogue_approval_expires_at ?? source.catalogue_coverage?.expires_at;
  if (
    source.quality_approval_status?.includes("expired") ||
    (expiry && new Date(expiry).getTime() <= now)
  )
    return "Approval expired — revalidation required";
  if (source.quality_approval_status?.startsWith("catalogue_"))
    return "Catalogue review required";
  if (source.status !== "ACTIVE") return "Review required";
  if (source.quality_approval_status === "partial") return "Active — partial catalogue";
  if (source.quality_approval_status === "full" || source.mapping) return "Active";
  return "Approval required";
}

export type WhatsAppConnectionStatus =
  "CONFIGURED" | "VALIDATED" | "ACTIVE" | "UNHEALTHY" | "DISABLED";

export interface ProductMapping {
  contract_version: 1;
  capability: "products";
  object_id: string;
  key_columns: string[];
  name_columns: string[];
  row_scopes?: { column: string; value: string | number }[];
  sku_column: string | null;
  categories: {
    object_id: string;
    joins: { product_column: string; related_column: string }[];
    label_column: string;
  }[];
  identifiers: {
    object_id: string;
    joins: { product_column: string; related_column: string }[];
    value_column: string;
    kind: "barcode" | "alternate_id";
  }[];
}

export interface SchemaDiscovery {
  source_fingerprint?: string;
  engine: "postgresql" | "sqlserver";
  relationships?: {
    source_object_id: string;
    source_columns: string[];
    target_object_id: string;
    target_columns: string[];
  }[];
  objects: {
    object_id: string;
    schema_name: string;
    name: string;
    columns: {
      name: string;
      kind: string;
      nullable: boolean;
      type_signature?: string | null;
    }[];
    unique_keys: string[][];
  }[];
}

export interface OperationalWorkspace {
  proposals?: Record<string, unknown>;
  discovery: SchemaDiscovery;
  context: Record<string, unknown>;
  product_mapping: ProductMapping;
  mapping_schema: Record<string, unknown>;
  limitations: string[];
  history: { id: string; version: number; mapping: Record<string, unknown> }[];
}

export interface MappingReview {
  id: string;
  version: number;
  status: "proposing" | "review" | "approved" | "failed";
  discovery: SchemaDiscovery;
  proposal: {
    mapping: ProductMapping | null;
    uncertainties: string[];
    rationale: string;
  } | null;
  approved_mapping: ProductMapping | null;
  schema_fingerprint: string;
  failure_code: string | null;
  validation_notes: string[];
}

export interface CatalogueProduct {
  external_product_id: string;
  names: string[];
  sku: string | null;
  categories: string[];
  stock: null;
}

export interface CatalogueResult {
  coverage?: CatalogueCoverage | null;
  mapping_version: number;
  status: "resolved" | "ambiguous" | "not_found";
  items: CatalogueProduct[];
  truncated: boolean;
  capabilities: ["products"];
}

export interface CatalogueCoverage {
  approval_type: "full" | "partial";
  approval_id?: string | null;
  approval_version?: number | null;
  excluded_records: number;
  total_records: number | null;
  valid_records: number | null;
  expires_at?: string | null;
  disclosure?: string | null;
}

export interface QualityReport {
  id: string;
  business_id: string;
  source_id: string;
  revision_id: string;
  mapping_version: number;
  mapping: ProductMapping;
  schema_fingerprint: string;
  source_fingerprint: string;
  validated_at: string;
  expires_at: string;
  total_records: number | null;
  valid_records: number | null;
  invalid_records: number | null;
  invalid_percent: string | null;
  max_invalid_percent: string;
  reason_counts: Record<string, number>;
  relationship_counts?: Record<string, number>;
  structural_status: "PASS" | "BLOCKED" | "INCOMPLETE";
  impact:
    | "SAFE_FOR_CAPABILITY"
    | "SAFE_WITH_EXCLUSIONS"
    | "BLOCKED_BY_MAPPING"
    | "BLOCKED_BY_DATA_QUALITY"
    | "INSUFFICIENT_CAPABILITY_EVIDENCE"
    | "SAFE_FOR_PARTIAL_CATALOGUE"
    | "REQUIRES_OWNER_REVIEW"
    | "UNSAFE"
    | "UNKNOWN_IMPACT";
  findings: string[];
  eligible: boolean;
  recommended_action: string;
  diagnostics: {
    identifier: string | null;
    name: string | null;
    rules: string[];
    explanation: string;
    relationship_concerns: string[];
    safe_exclusion: boolean;
    severity: "warning" | "blocked";
    suggested_action: string;
    locator_note: string;
  }[];
  diagnostics_truncated: boolean;
  integrity: string;
}

export interface WhatsAppConnection {
  id: string;
  display_name: string;
  provider_type: "meta_whatsapp";
  connection_profile_key: "meta_whatsapp_cloud";
  status: WhatsAppConnectionStatus;
  auto_reply_enabled: boolean;
  last_validated_at: string | null;
  last_successful_health_check_at: string | null;
  failure_code: string | null;
  capabilities: string[];
  created_at: string;
  updated_at: string;
}

export interface CustomerConversation {
  id: string;
  masked_customer_label: string;
  state: "AI_ACTIVE" | "HUMAN_HANDOFF";
  last_message_at: string | null;
  latest_message_preview: string | null;
  created_at: string;
  updated_at: string;
}

export interface CustomerMessage {
  id: string;
  direction: "inbound" | "outbound";
  sender: "customer" | "ai" | "owner";
  content: string;
  status:
    | "RECEIVED"
    | "PROCESSING"
    | "COMPLETED"
    | "PENDING_SEND"
    | "SENDING"
    | "SENT"
    | "DELIVERED"
    | "READ"
    | "FAILED";
  reply_to_message_id: string | null;
  failure_code: string | null;
  created_at: string;
  updated_at: string;
}

export type CapabilityFamily = "inventory" | "pricing" | "sales" | "financial";
export interface CapabilityQualityReport {
  id: string;
  capability: CapabilityFamily;
  integrity: string;
  mapping: Record<string, unknown>;
  discovery: SchemaDiscovery;
  meaning: string;
  validated_at: string;
  expires_at: string;
  total_records: number | null;
  valid_records: number | null;
  invalid_records: number | null;
  invalid_percent: string | null;
  max_invalid_percent: string;
  denominator: string;
  structural_status: string;
  impact: string;
  eligible: boolean;
  reason_counts: Record<string, number>;
  record_counts: Record<string, number>;
  findings: string[];
  diagnostics: {
    object_id: string;
    identifier: (string | null)[];
    rules: string[];
    explanation: string;
    safe_exclusion: boolean;
    locator_note: string;
  }[];
  diagnostics_truncated: boolean;
}
export interface CapabilityState {
  capability: CapabilityFamily;
  status: string;
  connection_status: string;
  mapping_status: string;
  quality_status: string;
  approval_status: string;
  runtime_status: string;
  runtime_invalid_records?: number | null;
  prerequisites: string[];
  candidates: {
    object_id: string;
    unique_keys: string[][];
    numeric_fields: string[];
    time_fields: string[];
  }[];
  report: CapabilityQualityReport | null;
  latest_validation?: CapabilityQualityReport | null;
  history?: {
    version: number;
    report_id: string;
    approval_type: string;
    approved_at: string;
    expires_at: string;
  }[];
  approval_version: number | null;
  expires_at: string | null;
  tools: string[];
}
export interface CapabilityWorkspace {
  discovery: SchemaDiscovery | null;
  connection_status: string;
  context: Record<string, unknown>;
  identity_mapping: ProductMapping | null;
  capabilities: CapabilityState[];
  access_inventory: {
    objects?: {
      schema_name: string;
      object_name: string;
      columns: {
        name: string;
        kind: string;
        readable: boolean;
        approved_scope: boolean;
      }[];
    }[];
    truncated?: boolean;
    limitation?: string;
    reason?: string;
  };
  limitations: string[];
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code = "request_failed",
    message = "Something went wrong. Please try again.",
    public retryAfterSeconds?: number,
  ) {
    super(message);
  }
}

export function ownerChatErrorMessage(error: unknown) {
  if (!(error instanceof ApiError)) {
    return "We couldn't reach the assistant. Please try again.";
  }
  if (error.code === "owner_chat_rate_limited" && error.retryAfterSeconds) {
    return `The chat request limit was reached. Try again in ${error.retryAfterSeconds} seconds.`;
  }
  const messages: Record<string, string> = {
    assistant_rate_limited:
      "The assistant is handling too many requests right now. Please try again later.",
    assistant_timeout: "The assistant took too long to respond. Please try again.",
    assistant_transport_failure:
      "The assistant can't be reached right now. Please try again shortly.",
    assistant_invalid_response:
      "The assistant couldn't produce a usable response. Please try again.",
    conversation_busy:
      "This conversation is already processing a message. Please retry shortly.",
    owner_turn_failed:
      "That message couldn't be completed. Send a new message to try again.",
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
    error?: {
      code?: string;
      message?: string;
      fields?: { location?: (string | number)[]; message?: string }[];
    };
  } | null;
  const fields = body?.error?.fields
    ?.slice(0, 3)
    .map(
      (field) =>
        `${field.location?.filter((part) => part !== "body").join(".")}: ${field.message?.slice(0, 220)}`,
    )
    .join("; ");
  return new ApiError(
    response.status,
    body?.error?.code,
    fields ? `${body?.error?.message} ${fields}` : body?.error?.message,
    Number(response.headers.get("Retry-After")) || undefined,
  );
}

async function performRefresh(): Promise<boolean> {
  const response = await request<{ access_token: string }>(
    "/auth/refresh",
    { method: "POST" },
    { retryAuth: false },
  ).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  });
  if (!response) {
    accessToken = null;
    return false;
  }
  accessToken = response.access_token;
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
  options: { retryAuth?: boolean; timeoutMs?: number } = {},
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  init.signal?.addEventListener("abort", abort, { once: true });
  if (init.signal?.aborted) abort();
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(
        new ApiError(0, "request_timeout", "The request took too long. Please retry."),
      );
      controller.abort();
    }, options.timeoutMs ?? 30_000);
  });
  async function execute(retryAuth: boolean): Promise<T> {
    controller.signal.throwIfAborted();
    const headers = new Headers(init.headers);
    if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
    const response = await fetch(`${base}${path}`, {
      ...init,
      signal: controller.signal,
      headers,
      credentials: "include",
    });
    if (response.status === 401 && retryAuth && (await refreshSession())) {
      return execute(false);
    }
    if (!response.ok) throw await parseError(response);
    return response.status === 204 ? (undefined as T) : (response.json() as Promise<T>);
  }
  try {
    // A single deadline covers fetch, auth refresh/retry and response body parsing.
    return await Promise.race([execute(options.retryAuth ?? true), timeout]);
  } finally {
    clearTimeout(timer!);
    init.signal?.removeEventListener("abort", abort);
  }
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
    request<Business>(`/businesses/${id}/onboarding/confirm`, {
      method: "POST",
    }),
  messages: (id: string, conversationId?: string) =>
    request<{
      items: ChatMessage[];
      next_cursor: string | null;
      conversation_id?: string | null;
    }>(
      conversationId
        ? `/businesses/${id}/conversations/${conversationId}/messages`
        : `/businesses/${id}/owner-chat/messages`,
    ),
  send: (
    id: string,
    content: string,
    idempotencyKey: string = crypto.randomUUID(),
    conversationId?: string,
  ) =>
    request(
      conversationId
        ? `/businesses/${id}/conversations/${conversationId}/messages`
        : `/businesses/${id}/owner-chat/messages`,
      { method: "POST", ...json({ content, idempotency_key: idempotencyKey }) },
      // Above the supported 300-second turn lease plus the replay wait margin.
      { timeoutMs: 360_000 },
    ),
  conversations: (business: string, includeArchived = false, cursor?: string) => {
    const query = new URLSearchParams();
    if (includeArchived) query.set("include_archived", "true");
    if (cursor) query.set("cursor", cursor);
    const suffix = query.size ? `?${query.toString()}` : "";
    return request<{ items: Conversation[]; next_cursor: string | null }>(
      `/businesses/${business}/conversations${suffix}`,
    );
  },
  createConversation: (business: string) =>
    request<Conversation>(`/businesses/${business}/conversations`, {
      method: "POST",
    }),
  conversation: (business: string, conversation: string) =>
    request<Conversation>(`/businesses/${business}/conversations/${conversation}`),
  conversationMessages: (business: string, conversation: string) =>
    request<{ items: ChatMessage[]; next_cursor: string | null }>(
      `/businesses/${business}/conversations/${conversation}/messages`,
    ),
  sendConversationMessage: (
    business: string,
    conversation: string,
    content: string,
    idempotencyKey: string = crypto.randomUUID(),
  ) =>
    request(
      `/businesses/${business}/conversations/${conversation}/messages`,
      { method: "POST", ...json({ content, idempotency_key: idempotencyKey }) },
      { timeoutMs: 360_000 },
    ),
  archiveConversation: (business: string, conversation: string) =>
    request<Conversation>(
      `/businesses/${business}/conversations/${conversation}/archive`,
      { method: "POST" },
    ),
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
  setDocumentCustomerVisibility: (
    business: string,
    document: string,
    customerVisible: boolean,
  ) =>
    request<Document>(
      `/businesses/${business}/knowledge/documents/${document}/customer-visibility`,
      { method: "PATCH", ...json({ customer_visible: customerVisible }) },
    ),
  dataSourceProfiles: (business: string) =>
    request<ConnectionProfile[]>(
      `/businesses/${business}/data-sources/available-profiles`,
    ),
  dataSources: (business: string) =>
    request<DataSource[]>(`/businesses/${business}/data-sources`),
  createDataSource: (
    business: string,
    body: {
      display_name: string;
      connection_profile_key: string;
      mapping_profile_key: string;
      mapping_profile_version: number;
    },
  ) =>
    request<DataSource>(`/businesses/${business}/data-sources`, {
      method: "POST",
      ...json(body),
    }),
  validateDataSource: (business: string, source: string) =>
    request<DataSource>(`/businesses/${business}/data-sources/${source}/validate`, {
      method: "POST",
    }),
  activateDataSource: (business: string, source: string) =>
    request<DataSource>(`/businesses/${business}/data-sources/${source}/activate`, {
      method: "POST",
    }),
  checkDataSource: (business: string, source: string) =>
    request<DataSource>(`/businesses/${business}/data-sources/${source}/health`, {
      method: "POST",
    }),
  disableDataSource: (business: string, source: string) =>
    request<DataSource>(`/businesses/${business}/data-sources/${source}/disable`, {
      method: "POST",
    }),
  discoverSource: (business: string, source: string) =>
    request<SchemaDiscovery>(
      `/businesses/${business}/data-sources/${source}/discovery`,
    ),
  mappingReviews: (business: string, source: string) =>
    request<MappingReview[]>(`/businesses/${business}/data-sources/${source}/mappings`),
  rejectMapping: (business: string, source: string, revision: string) =>
    request<MappingReview>(
      `/businesses/${business}/data-sources/${source}/mappings/${revision}/reject`,
      { method: "POST" },
    ),
  catalogueQuality: (business: string, source: string) =>
    request<QualityReport | null>(
      `/businesses/${business}/data-sources/${source}/quality`,
    ),
  validateCatalogueQuality: (
    business: string,
    source: string,
    revision: string,
    mapping: ProductMapping,
  ) =>
    request<QualityReport>(
      `/businesses/${business}/data-sources/${source}/quality/validate`,
      {
        method: "POST",
        ...json({ revision_id: revision, mapping }),
      },
      { timeoutMs: 45_000 },
    ),
  approveCatalogueQuality: (business: string, source: string, report: QualityReport) =>
    request<CatalogueCoverage>(
      `/businesses/${business}/data-sources/${source}/quality/approve`,
      {
        method: "POST",
        ...json({
          approval_type: report.invalid_records === 0 ? "full" : "partial",
          report_id: report.id,
          integrity: report.integrity,
          excluded_records: report.invalid_records,
          total_records: report.total_records,
          confirm_semantics: true,
          acknowledge_uncertainties: true,
          confirm_catalogue_only: true,
        }),
      },
      { timeoutMs: 45_000 },
    ),
  revokeCatalogueQuality: (business: string, source: string) =>
    request<void>(`/businesses/${business}/data-sources/${source}/quality/revoke`, {
      method: "POST",
    }),
  capabilityWorkspace: (business: string, source: string) =>
    request<CapabilityWorkspace>(
      `/businesses/${business}/data-sources/${source}/capabilities`,
      {},
      { timeoutMs: 45_000 },
    ),
  capabilityQuality: (
    business: string,
    source: string,
    family: CapabilityFamily,
    reportId: string,
  ) =>
    request<CapabilityQualityReport>(
      `/businesses/${business}/data-sources/${source}/capabilities/${family}/quality?report_id=${encodeURIComponent(reportId)}`,
    ),
  validateCapability: (
    business: string,
    source: string,
    family: CapabilityFamily,
    body: {
      mapping: Record<string, unknown>;
      discovery: SchemaDiscovery;
      meaning: string;
    },
  ) =>
    request<CapabilityQualityReport>(
      `/businesses/${business}/data-sources/${source}/capabilities/${family}/validate`,
      { method: "POST", ...json(body) },
      { timeoutMs: 45_000 },
    ),
  approveCapability: (
    business: string,
    source: string,
    family: CapabilityFamily,
    report: CapabilityQualityReport,
  ) =>
    request<{ approval_id: string; version: number; status: string }>(
      `/businesses/${business}/data-sources/${source}/capabilities/${family}/approve`,
      {
        method: "POST",
        ...json({
          report_id: report.id,
          integrity: report.integrity,
          total_records: report.total_records,
          excluded_records: 0,
          confirm_semantics: true,
          confirm_scope: true,
          confirm_enable_capability: true,
        }),
      },
      { timeoutMs: 45_000 },
    ),
  revokeCapability: (business: string, source: string, family: CapabilityFamily) =>
    request<void>(
      `/businesses/${business}/data-sources/${source}/capabilities/${family}/revoke`,
      { method: "POST" },
    ),
  operationalWorkspace: (business: string, source: string) =>
    request<OperationalWorkspace>(
      `/businesses/${business}/data-sources/${source}/operational-mappings`,
    ),
  reviewOperations: (
    business: string,
    source: string,
    action: "validate" | "preview" | "approve",
    body: Record<string, unknown>,
  ) =>
    request<Record<string, unknown>>(
      `/businesses/${business}/data-sources/${source}/operational-mappings/${action}`,
      {
        method: "POST",
        ...json(body),
      },
    ),
  proposeMapping: (business: string, source: string, key: string) =>
    request<MappingReview>(
      `/businesses/${business}/data-sources/${source}/mappings/propose`,
      {
        method: "POST",
        ...json({ idempotency_key: key }),
      },
      { timeoutMs: 300_000 },
    ),
  approveMapping: (
    business: string,
    source: string,
    revision: string,
    mapping: ProductMapping,
  ) =>
    request<MappingReview>(
      `/businesses/${business}/data-sources/${source}/mappings/${revision}/approve`,
      {
        method: "POST",
        ...json({
          mapping,
          confirm_semantics: true,
          acknowledge_uncertainties: true,
        }),
      },
    ),
  searchCatalogue: (
    business: string,
    source: string,
    selector: {
      query?: string;
      external_product_id?: string;
      mapping_version?: number;
      limit?: number;
    },
  ) =>
    request<CatalogueResult>(
      `/businesses/${business}/data-sources/${source}/products/search`,
      {
        method: "POST",
        ...json(selector),
      },
    ),
  whatsAppConnections: (business: string) =>
    request<WhatsAppConnection[]>(`/businesses/${business}/channels/whatsapp`),
  configureWhatsApp: (business: string, displayName: string) =>
    request<WhatsAppConnection>(`/businesses/${business}/channels/whatsapp`, {
      method: "POST",
      ...json({
        display_name: displayName,
        connection_profile_key: "meta_whatsapp_cloud",
      }),
    }),
  validateWhatsApp: (business: string, connection: string) =>
    request<WhatsAppConnection>(
      `/businesses/${business}/channels/whatsapp/${connection}/validate`,
      { method: "POST" },
    ),
  activateWhatsApp: (business: string, connection: string) =>
    request<WhatsAppConnection>(
      `/businesses/${business}/channels/whatsapp/${connection}/activate`,
      { method: "POST" },
    ),
  checkWhatsApp: (business: string, connection: string) =>
    request<WhatsAppConnection>(
      `/businesses/${business}/channels/whatsapp/${connection}/health`,
      { method: "POST" },
    ),
  disableWhatsApp: (business: string, connection: string) =>
    request<WhatsAppConnection>(
      `/businesses/${business}/channels/whatsapp/${connection}/disable`,
      { method: "POST" },
    ),
  setWhatsAppAutoReply: (business: string, connection: string, enabled: boolean) =>
    request<WhatsAppConnection>(
      `/businesses/${business}/channels/whatsapp/${connection}/auto-reply`,
      { method: "PATCH", ...json({ enabled }) },
    ),
  customerConversations: (business: string, cursor?: string) => {
    const suffix = cursor ? `?${new URLSearchParams({ cursor }).toString()}` : "";
    return request<{
      items: CustomerConversation[];
      next_cursor: string | null;
    }>(`/businesses/${business}/channels/whatsapp/conversations${suffix}`);
  },
  customerMessages: (business: string, conversation: string, cursor?: string) => {
    const suffix = cursor ? `?${new URLSearchParams({ cursor }).toString()}` : "";
    return request<{ items: CustomerMessage[]; next_cursor: string | null }>(
      `/businesses/${business}/channels/whatsapp/conversations/${conversation}/messages${suffix}`,
    );
  },
  handoffCustomerConversation: (business: string, conversation: string) =>
    request<CustomerConversation>(
      `/businesses/${business}/channels/whatsapp/conversations/${conversation}/handoff`,
      { method: "POST" },
    ),
  resumeCustomerConversation: (business: string, conversation: string) =>
    request<CustomerConversation>(
      `/businesses/${business}/channels/whatsapp/conversations/${conversation}/resume`,
      { method: "POST" },
    ),
  sendCustomerReply: (business: string, conversation: string, content: string) =>
    request<CustomerMessage>(
      `/businesses/${business}/channels/whatsapp/conversations/${conversation}/messages`,
      { method: "POST", ...json({ content, confirmed: true }) },
    ),
};

export function resetApiSessionForTests() {
  accessToken = null;
  refreshInFlight = null;
}
