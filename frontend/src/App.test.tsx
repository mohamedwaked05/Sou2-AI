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
import { api, ApiError, ChatMessage } from "./api";

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

function mockActiveWorkspace(...messagePages: ChatMessage[][]) {
  vi.spyOn(api, "restoreSession").mockResolvedValueOnce(workspaceUser);
  vi.spyOn(api, "business").mockResolvedValueOnce(activeBusiness);
  const messages = vi.spyOn(api, "messages");
  for (const items of messagePages) {
    messages.mockResolvedValueOnce({ items, next_cursor: null });
  }
  return messages;
}

function renderWorkspace(path: "chat" | "conversations") {
  render(
    <MemoryRouter initialEntries={[`/businesses/business-1/${path}`]}>
      <App />
    </MemoryRouter>,
  );
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
  mockActiveWorkspace([
    historyMessage("message-1", "owner", "Hello"),
    historyMessage("message-2", "assistant", "Hi there!"),
  ]);

  renderWorkspace("conversations");

  const history = await screen.findByRole("log", {
    name: "Owner conversation history",
  });
  await waitFor(() => expect(history.scrollTop).toBe(1_000));
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
