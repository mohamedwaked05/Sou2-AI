import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { ChatContent } from "./chat-content";

afterEach(cleanup);

it("renders five returned products as separate readable entries with factual caveats", () => {
  const items = Array.from(
    { length: 5 },
    (_, i) => `${i + 1}. Item ${i + 1} — ID: sku-${i}`,
  );
  render(
    <ChatContent
      content={[
        "Matching catalogue products:",
        ...items,
        "Stock and prices are unknown.",
      ].join("\n")}
    />,
  );
  expect(screen.getAllByRole("listitem")).toHaveLength(5);
  expect(screen.getByText("Item 1 — ID: sku-0")).toBeVisible();
  expect(screen.getByText("Stock and prices are unknown.")).toBeVisible();
});

it("preserves historical unstructured text without inventing product facts", () => {
  const text =
    "Matching catalogue products: ITEM 1 (ID: A); ITEM 2 (ID: B). Stock and prices are unknown.";
  render(<ChatContent content={text} />);
  expect(screen.getByText(text)).toBeVisible();
  expect(screen.queryByRole("list")).toBeNull();
});

it("keeps unsafe source labels as text", () => {
  const { container } = render(
    <ChatContent
      content={
        "Products:\n1. <img src=x onerror=alert(1)>\n2. [label](javascript:alert(1))"
      }
    />,
  );
  expect(container.querySelector("img, a, script")).toBeNull();
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
});

it("preserves pagination warnings and separate lists", () => {
  render(
    <ChatContent
      content={
        "Matches:\n1. A\n2. B\nResults are truncated; narrow the search.\n5. Another list"
      }
    />,
  );
  expect(screen.getAllByRole("list")).toHaveLength(2);
  expect(screen.getByText(/Results are truncated/)).toBeVisible();
  expect(screen.getAllByRole("list")[1]).toHaveAttribute("start", "5");
});
