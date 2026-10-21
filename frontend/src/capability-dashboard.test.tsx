import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  api,
  ApiError,
  CapabilityFamily,
  CapabilityQualityReport,
  CapabilityWorkspace,
  DataSource,
} from "./api";
import { CapabilityDashboard } from "./capability-dashboard";

const source: DataSource = {
  id: "source",
  display_name: "Test source",
  adapter_type: "sqlserver_readonly",
  connection_profile_key: "reader",
  mapping: null,
  status: "UNHEALTHY",
  failure_code: "catalogue_value_invalid",
  capabilities: [],
  created_at: "2026-10-18T00:00:00Z",
  updated_at: "2026-10-18T00:00:00Z",
  last_validated_at: null,
  last_successful_health_check_at: null,
};
const discovery: CapabilityWorkspace["discovery"] = {
  engine: "postgresql",
  objects: [
    {
      object_id: "o1",
      schema_name: "public",
      name: "balances",
      columns: [{ name: "id", kind: "integer", nullable: false }],
      unique_keys: [["id"]],
    },
  ],
};
function report(invalid = 0): CapabilityQualityReport {
  return {
    id: "report",
    capability: "inventory",
    integrity: "a".repeat(64),
    mapping: { stock: { object_id: "o1" } },
    discovery: discovery!,
    meaning:
      "Owner reviewed authoritative balance by original product and warehouse.",
    validated_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 600000).toISOString(),
    total_records: 1000,
    valid_records: 1000 - invalid,
    invalid_records: invalid,
    invalid_percent: String(invalid / 10),
    max_invalid_percent: "1",
    denominator: "All in-scope balances",
    structural_status: "PASS",
    impact: invalid ? "BLOCKED_BY_DATA_QUALITY" : "SAFE_FOR_CAPABILITY",
    eligible: invalid === 0,
    reason_counts: { invalid_number: invalid },
    record_counts: { stock: 1000 },
    findings: ["Every balance is required; no operational facts are excluded."],
    diagnostics: invalid
      ? [
          {
            object_id: "o1",
            identifier: [null],
            rules: ["invalid_number"],
            explanation: "Balance quantity is missing.",
            safe_exclusion: false,
            locator_note:
              "Original identifier unavailable; no replacement key.",
          },
        ]
      : [],
    diagnostics_truncated: false,
  };
}
function workspace(invalid = 0): CapabilityWorkspace {
  return {
    discovery,
    connection_status: "CONNECTED",
    context: {},
    identity_mapping: null,
    access_inventory: {
      objects: [],
      limitation: "Metadata is access limited.",
    },
    limitations: [],
    capabilities: (
      ["inventory", "pricing", "sales", "financial"] as CapabilityFamily[]
    ).map((capability) => ({
      capability,
      status: capability === "inventory" ? "VALIDATED" : "MAPPING_REQUIRED",
      connection_status: "CONNECTED",
      mapping_status: capability === "inventory" ? "PASS" : "MAPPING_REQUIRED",
      quality_status:
        capability === "inventory" ? report(invalid).impact : "UNKNOWN",
      approval_status: "UNAPPROVED",
      runtime_status: "UNAVAILABLE",
      prerequisites: ["Review authoritative meaning, source access and scope."],
      candidates: [],
      report: capability === "inventory" ? report(invalid) : null,
      approval_version: null,
      expires_at: null,
      tools: [],
    })),
  };
}
beforeEach(() => {
  vi.spyOn(api, "capabilityWorkspace").mockResolvedValue(workspace());
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("separates connection health and five independently reviewed capabilities", async () => {
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  expect(await screen.findByText("connected")).toBeVisible();
  for (const name of [
    "Catalogue",
    "Inventory",
    "Pricing",
    "Sales",
    "Financial reporting",
  ])
    expect(screen.getByRole("heading", { name })).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Approve and enable inventory" }),
  ).toBeNull();
});

it("shows a business review guide before collapsed technical fields", async () => {
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Review pricing" }),
  );
  const guide = screen.getByRole("region", { name: "Pricing next steps" });
  expect(guide).toHaveTextContent("What approval enables");
  expect(guide).toHaveTextContent("reviewed source price");
  expect(guide).toHaveTextContent("Retail meaning requires confirmation");
  expect(guide).toHaveTextContent(
    "operator to review the exact readable columns",
  );
  expect(guide).toHaveTextContent(
    "No capability quality report has been completed",
  );
  expect(
    screen.getByText("Advanced: map source fields").closest("details"),
  ).not.toHaveAttribute("open");
  expect(
    screen.queryByRole("button", { name: "Approve and enable pricing" }),
  ).toBeNull();
});

it("distinguishes expired eligible evidence from a report ready for approval", async () => {
  const value = workspace();
  value.capabilities[0].report!.expires_at = new Date(
    Date.now() - 60000,
  ).toISOString();
  vi.mocked(api.capabilityWorkspace).mockResolvedValue(value);
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  expect(await screen.findByText("Revalidation required")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Review inventory" }));
  expect(
    screen.queryByRole("button", { name: "Approve and enable inventory" }),
  ).toBeNull();
});

it("shows actual source names and submitted meaning without claiming activation", async () => {
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Review inventory" }),
  );
  const guide = screen.getByRole("region", { name: "Inventory next steps" });
  expect(guide).toHaveTextContent("public.balances");
  expect(guide).toHaveTextContent(report().meaning);
  expect(guide).toHaveTextContent("this interpretation is not active");
  expect(guide).toHaveTextContent("explicitly approve this capability");
});

it("keeps source recovery actionable when discovery is unavailable", async () => {
  const value = workspace();
  value.discovery = null;
  value.connection_status = "SOURCE_UNAVAILABLE";
  for (const state of value.capabilities) {
    state.status = "SOURCE_UNAVAILABLE";
    state.connection_status = "SOURCE_UNAVAILABLE";
  }
  vi.mocked(api.capabilityWorkspace).mockResolvedValue(value);
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Review inventory" }),
  );
  const guide = screen.getByRole("region", { name: "Inventory next steps" });
  expect(guide).toHaveTextContent(
    "Previous reports do not verify its current data",
  );
  expect(guide).toHaveTextContent("Restore the source connection");
  expect(screen.queryByRole("region", { name: "Inventory review" })).toBeNull();
  expect(
    screen.queryByRole("button", { name: "Approve and enable inventory" }),
  ).toBeNull();
});

it("keeps a ready-for-review report distinct from active availability", async () => {
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  expect(await screen.findByText("Ready for approval review")).toBeVisible();
  expect(screen.queryByText("Ready", { exact: true })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Review inventory" }));
  expect(
    screen.getByRole("region", { name: "Inventory next steps" }),
  ).toHaveTextContent("freshness and source data are checked again");
});

it("requires fresh explicit meaning and scope confirmation before enabling only inventory", async () => {
  const approve = vi.spyOn(api, "approveCapability").mockResolvedValue({
    approval_id: "approved",
    version: 1,
    status: "ACTIVE",
  });
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Review inventory" }),
  );
  const button = screen.getByRole("button", {
    name: "Approve and enable inventory",
  });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button);
  await waitFor(() => expect(approve).toHaveBeenCalledOnce());
  expect(approve.mock.calls[0].slice(0, 3)).toEqual([
    "business",
    "source",
    "inventory",
  ]);
  expect(approve.mock.calls[0][3].invalid_records).toBe(0);
});

it("blocks operational approval even below tolerance and shows bounded invalid facts", async () => {
  vi.mocked(api.capabilityWorkspace).mockResolvedValue(workspace(1));
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Review inventory" }),
  );
  expect(screen.getByText("Balance quantity is missing.")).toBeVisible();
  expect(screen.getByText(/Original identifier unavailable/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Approve and enable inventory" }),
  ).toBeNull();
});

it("editing meaning or mapping invalidates the previous confirmation", async () => {
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Review inventory" }),
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(
    screen.getByLabelText("What does this capability measure?"),
    { target: { value: "A changed balance meaning requires new validation." } },
  );
  expect(
    screen.queryByRole("button", { name: "Approve and enable inventory" }),
  ).toBeNull();
  expect(
    screen.getByRole("button", { name: "Validate inventory" }),
  ).toBeEnabled();
});

it("explains stale approvals and clears confirmation", async () => {
  vi.spyOn(api, "approveCapability").mockRejectedValue(
    new ApiError(409, "capability_report_stale", "Stale"),
  );
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Review inventory" }),
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(
    screen.getByRole("button", { name: "Approve and enable inventory" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    /Revalidate before approving/,
  );
  expect(screen.getByRole("checkbox")).not.toBeChecked();
});

it("revokes one capability through its own endpoint", async () => {
  const value = workspace();
  value.capabilities[0].approval_status = "APPROVED";
  value.capabilities[0].status = "ACTIVE";
  vi.mocked(api.capabilityWorkspace).mockResolvedValue(value);
  const revoke = vi.spyOn(api, "revokeCapability").mockResolvedValue(undefined);
  render(
    <CapabilityDashboard
      businessId="business"
      source={source}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Review inventory" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Revoke inventory approval" }),
  );
  await waitFor(() =>
    expect(revoke).toHaveBeenCalledWith("business", "source", "inventory"),
  );
});
