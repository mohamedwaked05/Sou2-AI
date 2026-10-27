import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError, DataSource, ProductMapping, QualityReport } from "./api";
import { CatalogueQuality } from "./catalogue-quality";

const mapping: ProductMapping = {
  contract_version: 1,
  capability: "products",
  object_id: "o1",
  key_columns: ["id"],
  name_columns: ["label"],
  sku_column: null,
  categories: [],
  identifiers: [],
};
const source: DataSource = {
  id: "source",
  display_name: "Quality source",
  adapter_type: "sqlserver_readonly",
  connection_profile_key: "reader",
  mapping: null,
  status: "UNHEALTHY",
  last_validated_at: null,
  last_successful_health_check_at: null,
  failure_code: "catalogue_value_invalid",
  capabilities: ["products"],
  created_at: "2026-10-18T00:00:00Z",
  updated_at: "2026-10-18T00:00:00Z",
};
const report: QualityReport = {
  id: "report",
  business_id: "business",
  source_id: "source",
  revision_id: "revision",
  mapping_version: 1,
  mapping,
  schema_fingerprint: "a".repeat(64),
  source_fingerprint: "b".repeat(64),
  validated_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + 900000).toISOString(),
  total_records: 1000,
  valid_records: 999,
  invalid_records: 1,
  invalid_percent: "0.1",
  max_invalid_percent: "1.0",
  reason_counts: { missing_name: 1 },
  structural_status: "PASS",
  impact: "SAFE_FOR_PARTIAL_CATALOGUE",
  findings: ["Full relationship scope verified."],
  eligible: true,
  recommended_action: "Review and explicitly approve exclusions.",
  integrity: "c".repeat(64),
  diagnostics: [
    {
      identifier: null,
      name: "<img src=x onerror=alert(1)>",
      rules: ["missing_name"],
      explanation: "No usable name.",
      relationship_concerns: [],
      safe_exclusion: true,
      severity: "warning",
      suggested_action: "Review source fields.",
      locator_note: "No synthetic key.",
    },
  ],
  diagnostics_truncated: false,
};

beforeEach(() => {
  vi.spyOn(api, "catalogueQuality").mockResolvedValue(report);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
function show(value = source, draft = mapping) {
  return render(
    <CatalogueQuality
      businessId="business"
      source={value}
      revisionId="revision"
      mapping={draft}
      onRefresh={vi.fn()}
    />,
  );
}
async function open() {
  fireEvent.click(await screen.findByText("Review exclusions (1)"));
  await waitFor(() => expect(screen.getByRole("checkbox")).toBeVisible());
}

it("shows exact counts and requires a separate explicit exclusion confirmation", async () => {
  const approve = vi.spyOn(api, "approveCatalogueQuality").mockResolvedValue({
    approval_type: "partial",
    excluded_records: 1,
    total_records: 1000,
    valid_records: 999,
  });
  const view = show();
  expect(
    await screen.findByText("Review required — partial approval eligible"),
  ).toBeVisible();
  expect(screen.getByText("0.100000%")).toBeVisible();
  await open();
  expect(screen.getByText("Identifier unavailable")).toBeVisible();
  expect(view.container.querySelector("img")).toBeNull();
  const button = screen.getByRole("button", {
    name: "Approve partial catalogue",
  });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button);
  await waitFor(() =>
    expect(approve).toHaveBeenCalledWith("business", "source", report),
  );
  expect(screen.getByRole("checkbox")).not.toBeChecked();
});

it.each(["UNSAFE", "UNKNOWN_IMPACT"] as const)(
  "blocks %s despite being below tolerance",
  async (impact) => {
    vi.mocked(api.catalogueQuality).mockResolvedValue({
      ...report,
      impact,
      eligible: false,
    });
    show();
    expect(await screen.findByText("Approval blocked")).toBeVisible();
    await open();
    expect(
      screen.getByRole("button", { name: "Approve partial catalogue" }),
    ).toBeDisabled();
    expect(screen.getByRole("checkbox")).toBeDisabled();
  },
);

it("clears confirmation and blocks a changed mapping draft", async () => {
  const view = show();
  await open();
  fireEvent.click(screen.getByRole("checkbox"));
  view.rerender(
    <CatalogueQuality
      businessId="business"
      source={source}
      revisionId="revision"
      mapping={{ ...mapping, name_columns: ["other"] }}
      onRefresh={vi.fn()}
    />,
  );
  expect(await screen.findByText(/belongs to a different mapping/)).toBeVisible();
  fireEvent.click(screen.getByText("Review exclusions (1)"));
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(screen.getByRole("checkbox")).toBeDisabled();
});

it("explains stale server approval and does not retain confirmation", async () => {
  vi.spyOn(api, "approveCatalogueQuality").mockRejectedValue(
    new ApiError(409, "catalogue_quality_report_stale"),
  );
  show();
  await open();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Approve partial catalogue" }));
  expect(await screen.findByText(/source or mapping changed/)).toBeVisible();
  expect(screen.getByRole("checkbox")).not.toBeChecked();
});

it("distinguishes active partial coverage and exposes revocation", async () => {
  const revoke = vi.spyOn(api, "revokeCatalogueQuality").mockResolvedValue(undefined);
  show({
    ...source,
    status: "ACTIVE",
    catalogue_coverage: {
      approval_type: "partial",
      excluded_records: 1,
      total_records: 1000,
      valid_records: 999,
      disclosure: "1 invalid source record is excluded.",
    },
  });
  expect(await screen.findByText("Active — partial catalogue")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Revoke partial approval" }));
  await waitFor(() => expect(revoke).toHaveBeenCalledWith("business", "source"));
});

it("revalidates the current draft without silently approving", async () => {
  vi.mocked(api.catalogueQuality).mockResolvedValue(null);
  const validate = vi.spyOn(api, "validateCatalogueQuality").mockResolvedValue(report);
  const approve = vi.spyOn(api, "approveCatalogueQuality");
  show();
  fireEvent.click(
    await screen.findByRole("button", { name: "Validate catalogue quality" }),
  );
  expect(
    await screen.findByText("Review required — partial approval eligible"),
  ).toBeVisible();
  expect(validate).toHaveBeenCalledWith("business", "source", "revision", mapping);
  expect(approve).not.toHaveBeenCalled();
});

it("renews an unchanged active mapping only after fresh validation and owner confirmation", async () => {
  vi.mocked(api.catalogueQuality).mockResolvedValue({
    ...report,
    expires_at: new Date(Date.now() - 1000).toISOString(),
  });
  const freshReport = { ...report, id: "fresh-report" };
  const validate = vi
    .spyOn(api, "validateCatalogueQuality")
    .mockResolvedValue(freshReport);
  const approve = vi.spyOn(api, "approveCatalogueQuality").mockResolvedValue({
    approval_type: "partial",
    excluded_records: 1,
    total_records: 1000,
    valid_records: 999,
  });
  show({
    ...source,
    status: "ACTIVE",
    mapping_version: 1,
    last_validated_at: new Date(Date.now() - 10000).toISOString(),
    quality_approval_status: "catalogue_partial_approval_expired",
    catalogue_approval_expires_at: new Date(Date.now() - 1000).toISOString(),
  });
  await open();
  expect(
    screen.getByRole("button", { name: "Renew catalogue approval" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Revalidate catalogue" }));
  await waitFor(() => expect(validate).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByRole("checkbox")).toBeEnabled());
  expect(approve).not.toHaveBeenCalled();
  await open();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Renew catalogue approval" }));
  await waitFor(() =>
    expect(approve).toHaveBeenCalledWith("business", "source", freshReport),
  );
  expect(
    await screen.findByText(
      /source remains active; other capability approvals are unchanged/,
    ),
  ).toBeVisible();
});

it("blocks a different revision from being approved while the source is active", async () => {
  show({
    ...source,
    status: "ACTIVE",
    mapping_version: 2,
    quality_approval_status: "partial",
    catalogue_approval_expires_at: new Date(Date.now() + 10000).toISOString(),
  });
  await open();
  expect(screen.getByRole("checkbox")).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Approve partial catalogue" }),
  ).toBeDisabled();
});

it("requires revalidation instead of replaying the report from an active approval", async () => {
  show({
    ...source,
    status: "ACTIVE",
    mapping_version: 1,
    quality_approval_status: "partial",
    catalogue_approval_expires_at: new Date(Date.now() + 10000).toISOString(),
    last_validated_at: new Date(
      new Date(report.validated_at).getTime() + 1,
    ).toISOString(),
  });
  await open();
  expect(screen.getByRole("checkbox")).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Renew catalogue approval" }),
  ).toBeDisabled();
});
