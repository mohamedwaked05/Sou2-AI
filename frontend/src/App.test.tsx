import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "./App";
import { api, ApiError } from "./api";

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
});

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
  vi.spyOn(api, "messages").mockResolvedValue({ items: [], next_cursor: null });
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
});
