import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  api,
  ApiError,
  type Business,
  type ChatMessage,
  type Conversation,
} from "./api";
import { WorkspaceRoute } from "./workspace";

const user = {
  id: "owner",
  email: "owner@example.test",
  first_name: "Test",
  last_name: "Owner",
  email_verified_at: "2026-10-13T00:00:00Z",
  status: "ACTIVE",
};
function business(id: string): Business {
  return {
    id,
    name: id,
    description: "Offline",
    category: "OTHER",
    custom_category: null,
    default_language: "en",
    governorate: null,
    district: null,
    city: null,
    address_line: null,
    status: "ACTIVE",
    is_active: true,
    profile_complete: true,
    first_incomplete_section: null,
    onboarding_submitted_at: null,
    working_hours: [],
  };
}
function conversation(id: string): Conversation {
  return {
    id,
    title: id,
    creator_user_id: user.id,
    channel: "owner_web",
    next_turn_number: 1,
    last_message_at: null,
    latest_message_preview: null,
    archived: false,
    archived_at: null,
    created_at: "2026-10-13T00:00:00Z",
    updated_at: "2026-10-13T00:00:00Z",
  };
}
function history(content: string) {
  const message: ChatMessage = {
    id: content,
    role: "assistant",
    content,
    sequence_number: 2,
    created_at: "2026-10-13T00:00:00Z",
    reply_to_message_id: null,
    generation_state: null,
    sources: [],
  };
  return { items: [message], next_cursor: null };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function Navigation() {
  const navigate = useNavigate();
  return (
    <>
      <button onClick={() => navigate("/businesses/A/chat")}>Default</button>
      <button onClick={() => navigate("/businesses/A/chat?conversation=first")}>
        First
      </button>
      <button onClick={() => navigate("/businesses/A/chat?conversation=second")}>
        Second
      </button>
      <button onClick={() => navigate("/businesses/B/chat?conversation=second")}>
        Business B
      </button>
      <button onClick={() => navigate(-1)}>Browser back</button>
    </>
  );
}
function workspace(path = "/businesses/A/chat?conversation=first") {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Navigation />
      <Routes>
        <Route
          path="/businesses/:id/:page"
          element={
            <WorkspaceRoute
              user={user}
              dark={false}
              setDark={() => {}}
              onLogout={async () => {}}
            />
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}
async function send(content: string) {
  await waitFor(() =>
    expect(screen.getByRole("textbox", { name: "Message Sou2AI" })).toBeEnabled(),
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Message Sou2AI" }), {
    target: { value: content },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
}
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("Offline tests forbid network"))),
  );
  vi.spyOn(api, "business").mockImplementation(async (id) => business(id));
  vi.spyOn(api, "conversation").mockImplementation(async (_, id) => conversation(id));
  vi.spyOn(api, "conversationMessages").mockImplementation(async (tenant, id) =>
    history(`${tenant}-${id} history`),
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("hides the previous business while the next business loads and ignores late sends", async () => {
  const result = deferred<unknown>();
  const nextBusiness = deferred<Business>();
  vi.mocked(api.business).mockImplementation((id) =>
    id === "B" ? nextBusiness.promise : Promise.resolve(business(id)),
  );
  vi.spyOn(api, "sendConversationMessage").mockReturnValue(result.promise);
  workspace();
  await send("A draft");
  fireEvent.click(screen.getByText("Business B"));
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  await act(async () => nextBusiness.resolve(business("B")));
  await screen.findByText("B-second history");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "B draft" } });
  await act(async () => result.resolve({}));
  expect(screen.getByRole("textbox")).toHaveValue("B draft");
  expect(screen.queryByText("A-first history")).not.toBeInTheDocument();
  expect(api.conversationMessages).toHaveBeenCalledTimes(2);
});

it("ignores a late failed send and its history refresh after conversation navigation", async () => {
  const result = deferred<unknown>();
  vi.spyOn(api, "sendConversationMessage").mockReturnValue(result.promise);
  workspace();
  await send("First draft");
  fireEvent.click(screen.getByText("Second"));
  await screen.findByText("A-second history");
  await act(async () => result.reject(new ApiError(503, "assistant_timeout")));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox")).toBeEnabled();
  expect(api.conversationMessages).toHaveBeenCalledTimes(2);
});

it("ignores old chat history received after a conversation switch", async () => {
  const old = deferred<ReturnType<typeof history>>();
  vi.mocked(api.conversationMessages).mockImplementation(async (tenant, id) =>
    id === "first" ? old.promise : history(`${tenant}-${id} history`),
  );
  workspace();
  await waitFor(() =>
    expect(api.conversationMessages).toHaveBeenCalledWith("A", "first"),
  );
  fireEvent.click(screen.getByText("Second"));
  await screen.findByText("A-second history");
  await act(async () => old.resolve(history("stale history")));
  expect(screen.queryByText("stale history")).not.toBeInTheDocument();
  expect(screen.getByText("A-second history")).toBeInTheDocument();
});

it("reuses the turn key after a lost response and navigation back to the same conversation", async () => {
  const post = vi
    .spyOn(api, "sendConversationMessage")
    .mockRejectedValue(new TypeError("Lost response"));
  workspace();
  await send("Retry draft");
  await screen.findByRole("alert");
  const key = post.mock.calls[0][3];
  fireEvent.click(screen.getByText("Second"));
  await screen.findByText("A-second history");
  fireEvent.click(screen.getByText("First"));
  await screen.findByText("A-first history");
  expect(screen.getByRole("textbox")).toHaveValue("Retry draft");
  post.mockResolvedValue({});
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  expect(post.mock.calls[1]).toEqual(["A", "first", "Retry draft", key]);
  await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue(""));
});

it("retains a key when the original send completes after leaving and returning", async () => {
  const result = deferred<unknown>();
  const post = vi
    .spyOn(api, "sendConversationMessage")
    .mockReturnValueOnce(result.promise)
    .mockResolvedValue({});
  workspace();
  await send("Uncertain draft");
  const key = post.mock.calls[0][3];
  fireEvent.click(screen.getByText("Second"));
  await screen.findByText("A-second history");
  fireEvent.click(screen.getByText("First"));
  await screen.findByText("A-first history");
  await act(async () => result.resolve({}));
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  expect(post.mock.calls[1][3]).toBe(key);
});

it("a changed draft uses a new key and failed history refresh preserves visible history", async () => {
  const post = vi
    .spyOn(api, "sendConversationMessage")
    .mockRejectedValue(new TypeError("Lost response"));
  workspace();
  await screen.findByText("A-first history");
  vi.mocked(api.conversationMessages).mockRejectedValue(
    new Error("History unavailable"),
  );
  await send("Draft one");
  await screen.findByRole("alert");
  expect(screen.getByText("A-first history")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("textbox")).toBeEnabled());
  await send("Draft two");
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  expect(post.mock.calls[1][3]).not.toBe(post.mock.calls[0][3]);
});

it("conversation history selection rejects an out-of-order response", async () => {
  const old = deferred<ReturnType<typeof history>>();
  vi.spyOn(api, "conversations").mockResolvedValue({
    items: [conversation("first"), conversation("second")],
    next_cursor: null,
  });
  vi.mocked(api.conversationMessages).mockImplementation(async (_, id) =>
    id === "first" ? old.promise : history("Selected history"),
  );
  workspace("/businesses/A/conversations");
  await screen.findByRole("button", { name: /^second/ });
  fireEvent.click(screen.getByRole("button", { name: /^second/ }));
  await screen.findByText("Selected history");
  await act(async () => old.resolve(history("Old history")));
  expect(screen.queryByText("Old history")).not.toBeInTheDocument();
});

it("keeps an older conversation selected when opening chat, returning and navigating away", async () => {
  vi.spyOn(api, "conversations").mockResolvedValue({
    items: [conversation("newest"), conversation("older")],
    next_cursor: null,
  });
  workspace("/businesses/A/conversations");
  await screen.findByText("A-newest history");
  fireEvent.click(screen.getByRole("button", { name: /^older/ }));
  await screen.findByText("A-older history");
  expect(screen.getByRole("button", { name: /^older/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByRole("link", { name: "Open chat" })).toHaveAttribute(
    "href",
    "/businesses/A/chat?conversation=older",
  );
  fireEvent.click(screen.getByRole("link", { name: "Open chat" }));
  await screen.findByRole("textbox", { name: "Message Sou2AI" });
  expect(api.conversation).toHaveBeenLastCalledWith("A", "older");
  fireEvent.click(screen.getByRole("link", { name: "All conversations" }));
  await screen.findByRole("link", { name: "Open chat" });
  expect(screen.getByRole("button", { name: /^older/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  fireEvent.click(screen.getByRole("link", { name: "Business Settings" }));
  await screen.findByRole("heading", { name: "Business Settings" });
  fireEvent.click(screen.getByRole("link", { name: "Conversations" }));
  await screen.findByText("A-older history");
  expect(screen.getByRole("link", { name: "Open chat" })).toHaveAttribute(
    "href",
    "/businesses/A/chat?conversation=older",
  );
});

it("restores the selected conversation from the URL and browser back history", async () => {
  vi.spyOn(api, "conversations").mockResolvedValue({
    items: [conversation("newest"), conversation("older")],
    next_cursor: null,
  });
  workspace("/businesses/A/conversations?conversation=older");
  await screen.findByText("A-older history");
  expect(api.conversationMessages).not.toHaveBeenCalledWith("A", "newest");
  fireEvent.click(screen.getByRole("button", { name: /^newest/ }));
  await screen.findByText("A-newest history");
  fireEvent.click(screen.getByRole("button", { name: "Browser back" }));
  await screen.findByText("A-older history");
  expect(screen.getByRole("button", { name: /^older/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

it("shows recorded source metadata and passages and marks missing historical labels honestly", async () => {
  const response = history("Published policy [S1]. Additional claim [S5].");
  response.items[0].sources = [
    {
      label: "S1",
      document_id: "doc",
      filename: "policy.pdf",
      page_start: 3,
      page_end: 4,
      section_title: "Opening hours",
      available: true,
      excerpt: "Monday 08:15 - 19:45",
    },
  ];
  vi.mocked(api.conversationMessages).mockResolvedValue(response);
  workspace();
  await screen.findByRole("link", { name: "S1" });
  expect(screen.getByText(/S1 · policy.pdf/)).toHaveTextContent(
    "page 3–4 · Opening hours",
  );
  fireEvent.click(screen.getByRole("link", { name: "S1" }));
  expect(screen.getByText("Monday 08:15 - 19:45")).toBeVisible();
  expect(screen.getByText(/S5: source was not recorded/)).toBeVisible();
  expect(screen.queryByRole("link", { name: "S5" })).toBeNull();
});

it("pins default-chat retries to the original conversation even when the default changes", async () => {
  let latest = "original";
  const messages = vi.spyOn(api, "messages").mockImplementation(async (_, id) => ({
    ...history(`${id ?? latest} history`),
    conversation_id: id ?? latest,
  }));
  const post = vi.spyOn(api, "send").mockRejectedValue(new TypeError("Lost response"));
  workspace("/businesses/A/chat");
  await send("Retry original");
  await screen.findByRole("alert");
  const key = post.mock.calls[0][2];
  latest = "newer";
  fireEvent.click(screen.getByText("Second"));
  await screen.findByText("A-second history");
  fireEvent.click(screen.getByText("Default"));
  await screen.findByText("original history");
  post.mockResolvedValue({});
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  expect(post.mock.calls[0]).toEqual(["A", "Retry original", key, "original"]);
  expect(post.mock.calls[1]).toEqual(post.mock.calls[0]);
  expect(messages).toHaveBeenCalledWith("A", "original");
});

it("creates an explicit conversation on the first send when history reports none", async () => {
  vi.spyOn(api, "messages").mockResolvedValue({
    items: [],
    next_cursor: null,
    conversation_id: null,
  });
  const create = vi
    .spyOn(api, "createConversation")
    .mockResolvedValue(conversation("created"));
  const post = vi.spyOn(api, "send").mockResolvedValue({});
  workspace("/businesses/A/chat");
  await send("First message");
  await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
  expect(create).toHaveBeenCalledTimes(1);
  expect(post.mock.calls[0][3]).toBe("created");
});

it("reuses a pending conversation creation across navigation and manual replay", async () => {
  const created = deferred<Conversation>();
  vi.spyOn(api, "messages").mockResolvedValue({
    items: [],
    next_cursor: null,
    conversation_id: null,
  });
  const create = vi.spyOn(api, "createConversation").mockReturnValue(created.promise);
  const post = vi.spyOn(api, "send").mockResolvedValue({});
  workspace("/businesses/A/chat");
  await send("First draft");
  fireEvent.click(screen.getByText("Second"));
  await screen.findByText("A-second history");
  fireEvent.click(screen.getByText("Default"));
  await waitFor(() => expect(screen.getByRole("textbox")).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await act(async () => created.resolve(conversation("shared")));
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  expect(create).toHaveBeenCalledTimes(1);
  expect(post.mock.calls[1]).toEqual(post.mock.calls[0]);
  expect(post.mock.calls[1][3]).toBe("shared");
});

it("keeps independent pending turn keys for equal drafts and conversation IDs across businesses", async () => {
  const first = deferred<unknown>();
  const post = vi
    .spyOn(api, "sendConversationMessage")
    .mockReturnValueOnce(first.promise)
    .mockRejectedValueOnce(new TypeError("Lost B response"))
    .mockResolvedValue({ replayed: true });
  workspace("/businesses/A/chat?conversation=second");
  await send("Generated product request");
  const aKey = post.mock.calls[0][3];
  fireEvent.click(screen.getByText("Business B"));
  await screen.findByText("B-second history");
  await send("Generated product request");
  await screen.findByRole("alert");
  const bKey = post.mock.calls[1][3];
  expect(bKey).not.toBe(aKey);
  await act(async () => first.resolve({ replayed: false }));
  expect(screen.getByRole("textbox")).toHaveValue("Generated product request");
  expect(screen.queryByText("A-second history")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("Second"));
  await screen.findByText("A-second history");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
  expect(post.mock.calls[2]).toEqual([
    "A",
    "second",
    "Generated product request",
    aKey,
  ]);
  fireEvent.click(screen.getByText("Business B"));
  await screen.findByText("B-second history");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(post).toHaveBeenCalledTimes(4));
  expect(post.mock.calls[3]).toEqual([
    "B",
    "second",
    "Generated product request",
    bKey,
  ]);
});

it("discards late history from another business with the same conversation ID", async () => {
  const previous = deferred<ReturnType<typeof history>>();
  vi.mocked(api.conversationMessages).mockImplementation(async (tenant, id) =>
    tenant === "A" ? previous.promise : history(`${tenant}-${id} history`),
  );
  workspace("/businesses/A/chat?conversation=second");
  await waitFor(() =>
    expect(api.conversationMessages).toHaveBeenCalledWith("A", "second"),
  );
  fireEvent.click(screen.getByText("Business B"));
  await screen.findByText("B-second history");
  await act(async () => previous.resolve(history("Private A records")));
  expect(screen.queryByText("Private A records")).not.toBeInTheDocument();
  expect(screen.getByText("B-second history")).toBeInTheDocument();
  expect(screen.getByRole("textbox")).toBeEnabled();
});

it.each(["owner_chat_rate_limited", "assistant_unavailable"])(
  "uses a fresh turn key after terminal %s",
  async (code) => {
    const post = vi
      .spyOn(api, "sendConversationMessage")
      .mockRejectedValueOnce(
        new ApiError(
          code === "owner_chat_rate_limited" ? 429 : 503,
          code,
          "Try later.",
          12,
        ),
      )
      .mockResolvedValueOnce({});
    workspace();
    await send("sho fi 3endak anwe3 roz");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      code === "owner_chat_rate_limited" ? "Try again in 12 seconds" : "Try later.",
    );
    const firstKey = post.mock.calls[0][3];
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post.mock.calls[1][3]).not.toBe(firstKey);
  },
);
