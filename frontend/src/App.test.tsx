import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

import { App } from "./App";
import { api } from "./api";

it("renders the sign-in screen when no session can be restored", async () => {
  vi.spyOn(api, "refresh").mockRejectedValueOnce(new Error("no session"));
  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  );
  expect(await screen.findByText("Welcome back")).toBeInTheDocument();
});
