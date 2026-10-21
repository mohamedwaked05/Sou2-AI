import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api, DataSource, OperationalWorkspace } from "./api";
import { OperationalOnboarding } from "./operational-onboarding";

const source = { id: "source", status: "CONFIGURED" } as DataSource;
const workspace: OperationalWorkspace = {
  context: { catalogue_revision: 7 },
  discovery: { engine: "postgresql", objects: [] },
  product_mapping: {} as OperationalWorkspace["product_mapping"],
  mapping_schema: {},
  limitations: ["Names do not establish semantics."],
  history: [],
};
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("separates proposal validation, draft preview and explicit owner approval", async () => {
  vi.spyOn(api, "operationalWorkspace").mockResolvedValue(workspace);
  const submit = vi
    .spyOn(api, "reviewOperations")
    .mockResolvedValue({ approved: false });
  render(
    <OperationalOnboarding businessId="tenant" source={source} onRefresh={vi.fn()} />,
  );
  fireEvent.click(screen.getByText("Prepare operational proposal"));
  const validate = await screen.findByText("Validate operational structure");
  const approve = screen.getByText("Approve operational version");
  expect(approve).toBeDisabled();
  expect(submit).not.toHaveBeenCalled();
  fireEvent.click(validate);
  await waitFor(() => expect(screen.getAllByRole("checkbox")[0]).not.toBeDisabled());
  fireEvent.change(screen.getByLabelText("Bounded preview request"), {
    target: { value: '{"capability":"stock"}' },
  });
  fireEvent.click(screen.getByText("Preview source results"));
  await waitFor(() =>
    expect(submit).toHaveBeenCalledWith(
      "tenant",
      "source",
      "preview",
      expect.objectContaining({ request: { capability: "stock" } }),
    ),
  );
  expect(approve).toBeDisabled();
  fireEvent.click(screen.getAllByRole("checkbox")[0]);
  fireEvent.click(screen.getAllByRole("checkbox")[1]);
  expect(approve).not.toBeDisabled();
  fireEvent.change(screen.getByLabelText("Proposed operational mapping"), {
    target: { value: "{}" },
  });
  expect(approve).toBeDisabled();
  expect(screen.getAllByRole("checkbox")[0]).not.toBeChecked();
});

it("blocks active-source draft execution and safely renders evidence", async () => {
  vi.spyOn(api, "operationalWorkspace").mockResolvedValue({
    ...workspace,
    limitations: ["<img src=x onerror=alert(1)>"],
  });
  const { container } = render(
    <OperationalOnboarding
      businessId="tenant"
      source={{ ...source, status: "ACTIVE" }}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByText("Prepare operational proposal"));
  await screen.findByText(/Disable this source/);
  expect(screen.queryByText("Approve operational version")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
});

async function prepareApproval() {
  fireEvent.click(screen.getByText("Prepare operational proposal"));
  fireEvent.click(await screen.findByText("Validate operational structure"));
  await waitFor(() => expect(screen.getAllByRole("checkbox")[0]).not.toBeDisabled());
  fireEvent.click(screen.getAllByRole("checkbox")[0]);
  fireEvent.click(screen.getAllByRole("checkbox")[1]);
  return screen.getByText("Approve operational version");
}

it("retains a successful approval and prevents duplicate submission after refresh fails", async () => {
  vi.spyOn(api, "operationalWorkspace").mockResolvedValue(workspace);
  const submit = vi.spyOn(api, "reviewOperations").mockResolvedValue({ version: 2 });
  const refresh = vi.fn().mockRejectedValue(new Error("refresh unavailable"));
  render(
    <OperationalOnboarding businessId="tenant" source={source} onRefresh={refresh} />,
  );
  const approve = await prepareApproval();
  fireEvent.click(approve);
  await screen.findByText(
    /Approval was recorded, but refreshed history is unavailable/,
  );
  expect(approve).toBeDisabled();
  expect(screen.getAllByRole("checkbox")[0]).not.toBeChecked();
  fireEvent.click(approve);
  expect(submit.mock.calls.filter((call) => call[2] === "approve")).toHaveLength(1);
  expect(screen.getByText(/"version": 2/)).toBeInTheDocument();
});

it("requires fresh history after a lost approval response", async () => {
  vi.spyOn(api, "operationalWorkspace").mockResolvedValue(workspace);
  const submit = vi
    .spyOn(api, "reviewOperations")
    .mockImplementation(async (_business, _source, action) => {
      if (action === "approve") throw new Error("response lost");
      return { approved: false };
    });
  render(
    <OperationalOnboarding businessId="tenant" source={source} onRefresh={vi.fn()} />,
  );
  const approve = await prepareApproval();
  fireEvent.click(approve);
  await screen.findByRole("status");
  await waitFor(() =>
    expect(screen.getByText("Prepare operational proposal")).not.toBeDisabled(),
  );
  expect(approve).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Proposed operational mapping"), {
    target: { value: "{}" },
  });
  fireEvent.click(screen.getByText("Validate operational structure"));
  await waitFor(() => expect(screen.getAllByRole("checkbox")[0]).not.toBeDisabled());
  fireEvent.click(screen.getAllByRole("checkbox")[0]);
  fireEvent.click(screen.getAllByRole("checkbox")[1]);
  expect(approve).toBeDisabled();
  expect(submit.mock.calls.filter((call) => call[2] === "approve")).toHaveLength(1);
  fireEvent.click(screen.getByText("Prepare operational proposal"));
  await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
});
