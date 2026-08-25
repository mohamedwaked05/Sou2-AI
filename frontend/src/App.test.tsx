import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "./App";
import {
  api,
  ApiError,
  ChatMessage,
  Conversation,
  CustomerConversation,
  CustomerMessage,
  WhatsAppConnection,
} from "./api";

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
});

const workspaceUser = {
  id: "owner-1",
  email: "owner@example.com",
  first_name: "Maya",
  last_name: "Haddad",
  email_verified_at: "2026-08-23T08:00:00Z",
  status: "ACTIVE",
};

const activeBusiness = {
  id: "business-1",
  name: "Maya Bakery",
  description: "A neighborhood bakery serving fresh bread every day.",
  category: "BAKERY",
  custom_category: null,
  default_language: "en" as const,
  governorate: "Beirut",
  district: "Beirut",
  city: "Beirut",
  address_line: "Hamra Street",
  status: "ACTIVE" as const,
  is_active: true,
  profile_complete: true,
  first_incomplete_section: null,
  onboarding_submitted_at: "2026-08-23T08:00:00Z",
  working_hours: [],
};

function historyMessage(
  id: string,
  role: ChatMessage["role"],
  content: string,
  overrides: Partial<ChatMessage> = {},
): ChatMessage {
  return {
    id,
    sequence_number: Number(id.replace(/\D/g, "")) || 1,
    role,
    content,
    created_at: "2026-08-24T08:00:00Z",
    reply_to_message_id: role === "assistant" ? "message-1" : null,
    generation_state: role === "owner" ? "completed" : null,
    sources: [],
    ...overrides,
  };
}

function ownerConversation(
  id: string,
  title: string,
  overrides: Partial<Conversation> = {},
): Conversation {
  return {
    id,
    creator_user_id: "owner-1",
    channel: "owner_web",
    title,
    next_turn_number: 2,
    last_message_at: "2026-08-24T08:00:00Z",
    latest_message_preview: "Hi there!",
    archived: false,
    archived_at: null,
    created_at: "2026-08-24T07:00:00Z",
    updated_at: "2026-08-24T08:00:00Z",
    ...overrides,
  };
}

function mockActiveWorkspace(...messagePages: ChatMessage[][]) {
  vi.spyOn(api, "restoreSession").mockResolvedValueOnce(workspaceUser);
  vi.spyOn(api, "business").mockResolvedValueOnce(activeBusiness);
  const messages = vi.spyOn(api, "messages");
  for (const items of messagePages) {
    messages.mockResolvedValueOnce({ items, next_cursor: null });
  }
  return messages;
}

function renderWorkspace(path: "chat" | "conversations" | "settings") {
  render(
    <MemoryRouter initialEntries={[`/businesses/business-1/${path}`]}>
      <App />
    </MemoryRouter>,
  );
}

function whatsAppConnection(
  overrides: Partial<WhatsAppConnection> = {},
): WhatsAppConnection {
  return {
    id: "channel-1",
    display_name: "Customer WhatsApp",
    provider_type: "meta_whatsapp",
    connection_profile_key: "meta_whatsapp_cloud",
    status: "ACTIVE",
    auto_reply_enabled: true,
    last_validated_at: "2026-08-25T10:00:00Z",
    last_successful_health_check_at: "2026-08-25T10:00:00Z",
    failure_code: null,
    capabilities: ["inbound_text", "outbound_text", "delivery_status"],
    created_at: "2026-08-25T09:00:00Z",
    updated_at: "2026-08-25T10:00:00Z",
    ...overrides,
  };
}

function customerConversation(
  overrides: Partial<CustomerConversation> = {},
): CustomerConversation {
  return {
    id: "customer-conversation-1",
    masked_customer_label: "WhatsApp ••••3456",
    state: "HUMAN_HANDOFF",
    last_message_at: "2026-08-25T10:00:00Z",
    latest_message_preview: "Can I speak with someone?",
    created_at: "2026-08-25T09:00:00Z",
    updated_at: "2026-08-25T10:00:00Z",
    ...overrides,
  };
}

function customerMessage(overrides: Partial<CustomerMessage> = {}): CustomerMessage {
  return {
    id: "customer-message-1",
    direction: "inbound",
    sender: "customer",
    content: "Can I speak with someone?",
    status: "COMPLETED",
    reply_to_message_id: null,
    failure_code: null,
    created_at: "2026-08-25T10:00:00Z",
    updated_at: "2026-08-25T10:00:00Z",
    ...overrides,
  };
}

function mockScrollMetrics() {
  let viewportScrollHeight = 1_000;
  let textareaScrollHeight = 25;
  vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(function (
    this: Element,
  ) {
    return this instanceof HTMLTextAreaElement
      ? textareaScrollHeight
      : viewportScrollHeight;
  });
  vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(300);
  return {
    setViewportScrollHeight(value: number) {
      viewportScrollHeight = value;
    },
    setTextareaScrollHeight(value: number) {
      textareaScrollHeight = value;
    },
  };
}

it("renders the sign-in screen when no session can be restored", async () => {
  vi.spyOn(api, "restoreSession").mockRejectedValueOnce(new Error("no session"));
  render(
    <MemoryRouter initialEntries={["/login"]}>
      <App />
    </MemoryRouter>,
  );
  expect(
    await screen.findByRole("heading", { name: "Welcome back" }),
  ).toBeInTheDocument();
});

it("renders only the supported tenant-scoped workspace navigation", async () => {
  vi.spyOn(api, "restoreSession").mockResolvedValueOnce({
    id: "owner-1",
    email: "owner@example.com",
    first_name: "Maya",
    last_name: "Haddad",
    email_verified_at: "2026-08-23T08:00:00Z",
    status: "ACTIVE",
  });
  vi.spyOn(api, "business").mockResolvedValueOnce({
    id: "business-1",
    name: "Maya Bakery",
    description: "A neighborhood bakery serving fresh bread every day.",
    category: "BAKERY",
    custom_category: null,
    default_language: "en",
    governorate: "Beirut",
    district: "Beirut",
    city: "Beirut",
    address_line: "Hamra Street",
    status: "ACTIVE",
    is_active: true,
    profile_complete: true,
    first_incomplete_section: null,
    onboarding_submitted_at: "2026-08-23T08:00:00Z",
    working_hours: [],
  });

  render(
    <MemoryRouter initialEntries={["/businesses/business-1/analytics"]}>
      <App />
    </MemoryRouter>,
  );

  expect(
    await screen.findByRole("heading", { name: "Analytics", level: 1 }),
  ).toBeInTheDocument();
  const navigation = screen.getByRole("navigation", {
    name: "Business navigation",
  });
  for (const label of [
    "Overview",
    "AI Chat",
    "Conversations",
    "Knowledge Base",
    "Analytics",
    "Customers",
    "Data Sources",
    "Business Settings",
  ]) {
    expect(within(navigation).getByRole("link", { name: label })).toBeInTheDocument();
  }
  expect(screen.queryByText(/subscription|users & roles|audit logs/i)).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Open account menu" }));
  const menu = screen.getByRole("menu");
  expect(
    within(menu).getByRole("menuitem", { name: /profile and theme/i }),
  ).toHaveFocus();
  expect(within(menu).getByRole("menuitem", { name: /sign out/i })).toBeInTheDocument();
});

it("shows the classified owner-chat provider error", async () => {
  vi.spyOn(api, "restoreSession").mockResolvedValueOnce({
    id: "owner-1",
    email: "owner@example.com",
    first_name: "Maya",
    last_name: "Haddad",
    email_verified_at: "2026-08-23T08:00:00Z",
    status: "ACTIVE",
  });
  vi.spyOn(api, "business").mockResolvedValueOnce({
    id: "business-1",
    name: "Maya Bakery",
    description: "A neighborhood bakery.",
    category: "BAKERY",
    custom_category: null,
    default_language: "en",
    governorate: "Beirut",
    district: "Beirut",
    city: "Beirut",
    address_line: "Hamra Street",
    status: "ACTIVE",
    is_active: true,
    profile_complete: true,
    first_incomplete_section: null,
    onboarding_submitted_at: "2026-08-23T08:00:00Z",
    working_hours: [],
  });
  vi.spyOn(api, "messages")
    .mockResolvedValueOnce({ items: [], next_cursor: null })
    .mockResolvedValueOnce({
      items: [
        {
          id: "failed-after-submit",
          sequence_number: 1,
          role: "owner",
          content: "How can I stay focused?",
          created_at: "2026-08-24T08:00:00Z",
          reply_to_message_id: null,
          generation_state: "failed",
          sources: [],
        },
      ],
      next_cursor: null,
    });
  vi.spyOn(api, "send").mockRejectedValueOnce(
    new ApiError(503, "assistant_timeout", "generic error"),
  );

  render(
    <MemoryRouter initialEntries={["/businesses/business-1/chat"]}>
      <App />
    </MemoryRouter>,
  );

  await screen.findByRole("heading", { name: "AI Chat", level: 1 });
  fireEvent.change(screen.getByRole("textbox", { name: "Message Sou2AI" }), {
    target: { value: "How can I stay focused?" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  expect(
    await screen.findByText(
      "The assistant took too long to respond. Please try again.",
    ),
  ).toBeInTheDocument();
  expect(
    await screen.findByText("Response failed. Send a new message to try again."),
  ).toBeInTheDocument();
});

it("renders a failed owner turn as terminal history", async () => {
  vi.spyOn(api, "restoreSession").mockResolvedValueOnce({
    id: "owner-1",
    email: "owner@example.com",
    first_name: "Maya",
    last_name: "Haddad",
    email_verified_at: "2026-08-23T08:00:00Z",
    status: "ACTIVE",
  });
  vi.spyOn(api, "business").mockResolvedValueOnce({
    id: "business-1",
    name: "Maya Bakery",
    description: "A neighborhood bakery.",
    category: "BAKERY",
    custom_category: null,
    default_language: "en",
    governorate: "Beirut",
    district: "Beirut",
    city: "Beirut",
    address_line: "Hamra Street",
    status: "ACTIVE",
    is_active: true,
    profile_complete: true,
    first_incomplete_section: null,
    onboarding_submitted_at: "2026-08-23T08:00:00Z",
    working_hours: [],
  });
  vi.spyOn(api, "messages").mockResolvedValueOnce({
    items: [
      {
        id: "failed-owner-message",
        sequence_number: 1,
        role: "owner",
        content: "What is our return policy?",
        created_at: "2026-08-24T08:00:00Z",
        reply_to_message_id: null,
        generation_state: "failed",
        sources: [],
      },
    ],
    next_cursor: null,
  });

  render(
    <MemoryRouter initialEntries={["/businesses/business-1/chat"]}>
      <App />
    </MemoryRouter>,
  );

  expect(
    await screen.findByText("Response failed. Send a new message to try again."),
  ).toBeInTheDocument();
});

it("opens AI Chat history at the bottom with existing states and citations", async () => {
  mockScrollMetrics();
  mockActiveWorkspace([
    historyMessage("message-1", "owner", "What is our return policy?", {
      generation_state: "processing",
    }),
    historyMessage("message-2", "assistant", "Returns are accepted within 14 days.", {
      sources: [
        {
          label: "S1",
          document_id: "document-1",
          filename: "returns.pdf",
          page_start: 2,
          page_end: 2,
          section_title: "Returns",
          available: true,
        },
      ],
    }),
  ]);

  renderWorkspace("chat");

  const history = await screen.findByRole("log", { name: "Owner chat messages" });
  await waitFor(() => expect(history.scrollTop).toBe(1_000));
  expect(screen.getByText("Response is being generated.")).toBeInTheDocument();
  expect(screen.getByText("Sources")).toBeInTheDocument();
  expect(screen.getByText(/S1/)).toBeInTheDocument();
  expect(api.messages).toHaveBeenCalledWith("business-1");
});

it("opens Conversations history at the bottom", async () => {
  mockScrollMetrics();
  mockActiveWorkspace([]);
  vi.spyOn(api, "conversations").mockResolvedValueOnce({
    items: [ownerConversation("conversation-1", "Hello")],
    next_cursor: null,
  });
  vi.spyOn(api, "conversationMessages").mockResolvedValueOnce({
    items: [
      historyMessage("message-1", "owner", "Hello"),
      historyMessage("message-2", "assistant", "Hi there!"),
    ],
    next_cursor: null,
  });

  renderWorkspace("conversations");

  const history = await screen.findByRole("log", {
    name: "Owner conversation history",
  });
  await waitFor(() => expect(history.scrollTop).toBe(1_000));
  expect(api.conversations).toHaveBeenCalledWith("business-1", true);
});

it("creates and selects a new private owner conversation", async () => {
  mockActiveWorkspace([]);
  vi.spyOn(api, "conversations").mockResolvedValueOnce({
    items: [],
    next_cursor: null,
  });
  const created = ownerConversation("conversation-new", "New conversation", {
    next_turn_number: 1,
    last_message_at: null,
    latest_message_preview: null,
  });
  vi.spyOn(api, "createConversation").mockResolvedValueOnce(created);
  vi.spyOn(api, "conversationMessages").mockResolvedValueOnce({
    items: [],
    next_cursor: null,
  });

  renderWorkspace("conversations");
  fireEvent.click(await screen.findByRole("button", { name: "New conversation" }));

  expect(
    await screen.findByRole("heading", { name: "New conversation" }),
  ).toBeVisible();
  expect(screen.getByRole("heading", { name: "No messages yet" })).toBeInTheDocument();
  expect(api.createConversation).toHaveBeenCalledWith("business-1");
});

it("confirms archiving and keeps the conversation readable", async () => {
  mockActiveWorkspace([]);
  const current = ownerConversation("conversation-1", "Autumn promotion");
  vi.spyOn(api, "conversations").mockResolvedValueOnce({
    items: [current],
    next_cursor: null,
  });
  vi.spyOn(api, "conversationMessages").mockResolvedValueOnce({
    items: [historyMessage("message-1", "owner", "Plan autumn")],
    next_cursor: null,
  });
  vi.spyOn(api, "archiveConversation").mockResolvedValueOnce({
    ...current,
    archived: true,
    archived_at: "2026-08-25T09:00:00Z",
  });

  renderWorkspace("conversations");
  fireEvent.click(await screen.findByRole("button", { name: "Archive" }));
  expect(
    screen.getByRole("alertdialog", { name: "Archive conversation?" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Archive conversation" }));

  await waitFor(() =>
    expect(api.archiveConversation).toHaveBeenCalledWith(
      "business-1",
      "conversation-1",
    ),
  );
  expect(await screen.findByText(/Archived · read-only/)).toBeInTheDocument();
  expect(screen.getByText("Plan autumn")).toBeInTheDocument();
});

it("shows masked WhatsApp conversations and confirms a manual reply", async () => {
  mockActiveWorkspace([]);
  vi.spyOn(api, "conversations").mockResolvedValueOnce({
    items: [],
    next_cursor: null,
  });
  const conversation = customerConversation();
  vi.spyOn(api, "customerConversations").mockResolvedValueOnce({
    items: [conversation],
    next_cursor: null,
  });
  vi.spyOn(api, "customerMessages").mockResolvedValueOnce({
    items: [customerMessage()],
    next_cursor: null,
  });
  const sent = customerMessage({
    id: "manual-1",
    direction: "outbound",
    sender: "owner",
    content: "I can help you.",
    status: "PENDING_SEND",
  });
  vi.spyOn(api, "sendCustomerReply").mockResolvedValueOnce(sent);

  renderWorkspace("conversations");
  fireEvent.click(await screen.findByRole("tab", { name: "WhatsApp" }));
  expect((await screen.findAllByText("WhatsApp ••••3456"))[0]).toBeVisible();
  expect(screen.getByText("Human handoff · AI replies paused")).toBeVisible();
  fireEvent.change(screen.getByLabelText("Manual reply"), {
    target: { value: "I can help you." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send manual reply" }));
  expect(
    screen.getByRole("alertdialog", { name: "Send this WhatsApp reply?" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Confirm and send" }));
  await waitFor(() =>
    expect(api.sendCustomerReply).toHaveBeenCalledWith(
      "business-1",
      "customer-conversation-1",
      "I can help you.",
    ),
  );
  expect(await screen.findByText("pending send")).toBeVisible();
});

it("configures only the allowlisted WhatsApp profile in Business Settings", async () => {
  mockActiveWorkspace([]);
  vi.spyOn(api, "whatsAppConnections").mockResolvedValueOnce([]);
  vi.spyOn(api, "configureWhatsApp").mockResolvedValueOnce(
    whatsAppConnection({
      status: "CONFIGURED",
      auto_reply_enabled: false,
      last_validated_at: null,
      last_successful_health_check_at: null,
    }),
  );

  renderWorkspace("settings");
  expect(
    await screen.findByText(
      "Meta WhatsApp Cloud API · text only · deployment-managed credentials.",
    ),
  ).toBeVisible();
  expect(screen.queryByLabelText(/token|secret|host|url/i)).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "Connect WhatsApp" }));
  await waitFor(() =>
    expect(api.configureWhatsApp).toHaveBeenCalledWith(
      "business-1",
      "Customer WhatsApp",
    ),
  );
  expect(await screen.findByText("configured")).toBeVisible();
  expect(screen.getByRole("button", { name: "Validate" })).toBeEnabled();
});

it("tests, pauses, and confirms disabling an active WhatsApp connection", async () => {
  mockActiveWorkspace([]);
  const active = whatsAppConnection();
  vi.spyOn(api, "whatsAppConnections").mockResolvedValueOnce([active]);
  vi.spyOn(api, "checkWhatsApp").mockResolvedValueOnce(active);
  vi.spyOn(api, "setWhatsAppAutoReply").mockResolvedValueOnce({
    ...active,
    auto_reply_enabled: false,
  });
  vi.spyOn(api, "disableWhatsApp").mockResolvedValueOnce({
    ...active,
    status: "DISABLED",
    auto_reply_enabled: false,
  });

  renderWorkspace("settings");
  fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));
  await waitFor(() =>
    expect(api.checkWhatsApp).toHaveBeenCalledWith("business-1", "channel-1"),
  );
  fireEvent.click(screen.getByLabelText("Automatic customer replies"));
  await waitFor(() =>
    expect(api.setWhatsAppAutoReply).toHaveBeenCalledWith(
      "business-1",
      "channel-1",
      false,
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "Disable" }));
  expect(
    screen.getByRole("alertdialog", { name: "Disable WhatsApp?" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Disable WhatsApp" }));
  await waitFor(() =>
    expect(api.disableWhatsApp).toHaveBeenCalledWith("business-1", "channel-1"),
  );
  expect(await screen.findByText("disabled")).toBeVisible();
});

it("opens a selected archived conversation in read-only AI Chat", async () => {
  mockActiveWorkspace([]);
  vi.spyOn(api, "conversation").mockResolvedValueOnce(
    ownerConversation("conversation-archived", "Supplier decisions", {
      archived: true,
      archived_at: "2026-08-25T09:00:00Z",
    }),
  );
  vi.spyOn(api, "conversationMessages").mockResolvedValueOnce({
    items: [historyMessage("message-1", "owner", "Keep the earlier decision")],
    next_cursor: null,
  });

  render(
    <MemoryRouter
      initialEntries={[
        "/businesses/business-1/chat?conversation=conversation-archived",
      ]}
    >
      <App />
    </MemoryRouter>,
  );

  expect(await screen.findByText("Keep the earlier decision")).toBeInTheDocument();
  expect(screen.getByText(/archived and read-only/i)).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Message Sou2AI" })).toBeDisabled();
  expect(api.conversationMessages).toHaveBeenCalledWith(
    "business-1",
    "conversation-archived",
  );
});

it("keeps AI Chat pinned while generating and after a new message loads", async () => {
  mockScrollMetrics();
  const initial = [historyMessage("message-1", "owner", "Earlier message")];
  const updated = [
    ...initial,
    historyMessage("message-2", "assistant", "Earlier reply"),
    historyMessage("message-3", "owner", "Newest message"),
    historyMessage("message-4", "assistant", "Newest reply"),
  ];
  mockActiveWorkspace(initial, updated);
  let finishSend: (() => void) | undefined;
  const pendingSend = new Promise<unknown>((resolve) => {
    finishSend = () => resolve({});
  });
  vi.spyOn(api, "send").mockReturnValueOnce(pendingSend);
  renderWorkspace("chat");
  const history = await screen.findByRole("log", { name: "Owner chat messages" });
  await waitFor(() => expect(history.scrollTop).toBe(1_000));
  history.scrollTop = 650;
  fireEvent.scroll(history);

  fireEvent.change(screen.getByRole("textbox", { name: "Message Sou2AI" }), {
    target: { value: "Newest message" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  expect(
    await screen.findByText("Sou2AI is generating a response…"),
  ).toBeInTheDocument();
  expect(history.scrollTop).toBe(1_000);
  finishSend?.();
  expect(await screen.findByText("Newest reply")).toBeInTheDocument();
  expect(history.scrollTop).toBe(1_000);
});

it("does not move AI Chat after the user scrolls upward", async () => {
  mockScrollMetrics();
  const initial = [historyMessage("message-1", "owner", "Earlier message")];
  mockActiveWorkspace(initial, [
    ...initial,
    historyMessage("message-2", "assistant", "New reply"),
  ]);
  vi.spyOn(api, "send").mockResolvedValueOnce({});
  renderWorkspace("chat");
  const history = await screen.findByRole("log", { name: "Owner chat messages" });
  await waitFor(() => expect(history.scrollTop).toBe(1_000));
  history.scrollTop = 100;
  fireEvent.scroll(history);

  fireEvent.change(screen.getByRole("textbox", { name: "Message Sou2AI" }), {
    target: { value: "A deliberate new message" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  expect(await screen.findByText("New reply")).toBeInTheDocument();
  expect(history.scrollTop).toBe(100);
});

it("submits once with Enter through the existing form path", async () => {
  mockScrollMetrics();
  mockActiveWorkspace([], []);
  const send = vi.spyOn(api, "send").mockResolvedValueOnce({});
  renderWorkspace("chat");
  const composer = await screen.findByRole("textbox", { name: "Message Sou2AI" });
  fireEvent.change(composer, { target: { value: "Hello there" } });

  fireEvent.keyDown(composer, { key: "Enter", code: "Enter" });

  await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
  expect(send).toHaveBeenCalledWith("business-1", "Hello there");
});

it("keeps Shift+Enter as a newline without submitting", async () => {
  mockScrollMetrics();
  mockActiveWorkspace([]);
  const send = vi.spyOn(api, "send");
  renderWorkspace("chat");
  const composer = await screen.findByRole("textbox", { name: "Message Sou2AI" });
  fireEvent.change(composer, { target: { value: "Line one" } });

  fireEvent.keyDown(composer, { key: "Enter", code: "Enter", shiftKey: true });
  fireEvent.change(composer, { target: { value: "Line one\n" } });

  expect(composer).toHaveValue("Line one\n");
  expect(send).not.toHaveBeenCalled();
});

it("does not submit Enter during IME composition", async () => {
  mockScrollMetrics();
  mockActiveWorkspace([]);
  const send = vi.spyOn(api, "send");
  renderWorkspace("chat");
  const composer = await screen.findByRole("textbox", { name: "Message Sou2AI" });
  fireEvent.change(composer, { target: { value: "مرحبا" } });

  fireEvent.compositionStart(composer);
  fireEvent.keyDown(composer, { key: "Enter", code: "Enter", isComposing: true });
  fireEvent.compositionEnd(composer);

  expect(send).not.toHaveBeenCalled();
});

it("rejects whitespace-only keyboard and button submission", async () => {
  mockScrollMetrics();
  mockActiveWorkspace([]);
  const send = vi.spyOn(api, "send");
  renderWorkspace("chat");
  const composer = await screen.findByRole("textbox", { name: "Message Sou2AI" });
  const button = screen.getByRole("button", { name: "Send message" });
  fireEvent.change(composer, { target: { value: "   \n" } });

  fireEvent.keyDown(composer, { key: "Enter", code: "Enter" });
  fireEvent.click(button);

  expect(button).toBeDisabled();
  expect(send).not.toHaveBeenCalled();
});

it("keeps the Send button as a working submission path", async () => {
  mockScrollMetrics();
  mockActiveWorkspace([], []);
  const send = vi.spyOn(api, "send").mockResolvedValueOnce({});
  renderWorkspace("chat");
  fireEvent.change(await screen.findByRole("textbox", { name: "Message Sou2AI" }), {
    target: { value: "Button message" },
  });

  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
  expect(send).toHaveBeenCalledWith("business-1", "Button message");
});

it("grows the composer with wrapped content", async () => {
  const metrics = mockScrollMetrics();
  mockActiveWorkspace([]);
  renderWorkspace("chat");
  const composer = await screen.findByRole("textbox", {
    name: "Message Sou2AI",
  });
  const initialHeight = Number.parseFloat(composer.style.height);
  metrics.setTextareaScrollHeight(80);

  fireEvent.change(composer, {
    target: { value: "A longer message that wraps onto more than one line." },
  });

  expect(Number.parseFloat(composer.style.height)).toBe(80);
  expect(Number.parseFloat(composer.style.height)).toBeGreaterThan(initialHeight);
  expect(composer.style.overflowY).toBe("hidden");
});

it("caps the composer near six lines and enables internal scrolling", async () => {
  const metrics = mockScrollMetrics();
  mockActiveWorkspace([]);
  renderWorkspace("chat");
  const composer = await screen.findByRole("textbox", {
    name: "Message Sou2AI",
  });
  const oneLineHeight = Number.parseFloat(composer.style.height);
  metrics.setTextareaScrollHeight(1_000);

  fireEvent.change(composer, { target: { value: "Long content\n".repeat(12) } });

  const cappedHeight = Number.parseFloat(composer.style.height);
  expect(cappedHeight).toBeGreaterThan(oneLineHeight);
  expect(cappedHeight).toBeLessThanOrEqual(oneLineHeight * 6);
  expect(cappedHeight).toBeLessThan(1_000);
  expect(composer.style.overflowY).toBe("auto");
});

it("returns the composer to one line after a successful send", async () => {
  const metrics = mockScrollMetrics();
  mockActiveWorkspace([], []);
  vi.spyOn(api, "send").mockResolvedValueOnce({});
  renderWorkspace("chat");
  const composer = await screen.findByRole("textbox", {
    name: "Message Sou2AI",
  });
  const oneLineHeight = composer.style.height;
  metrics.setTextareaScrollHeight(100);
  fireEvent.change(composer, { target: { value: "A wrapped outgoing message" } });
  expect(composer.style.height).not.toBe(oneLineHeight);

  fireEvent.click(screen.getByRole("button", { name: "Send message" }));

  await waitFor(() => expect(composer).toHaveValue(""));
  expect(composer.style.height).toBe(oneLineHeight);
  expect(composer.style.overflowY).toBe("hidden");
});
