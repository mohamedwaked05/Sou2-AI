import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError, DataSource, MappingReview, ProductMapping } from "./api";
import { MappingOnboarding } from "./mapping-onboarding";

const mapping: ProductMapping = {
  contract_version: 1,
  capability: "products",
  object_id: "o1",
  key_columns: ["ref"],
  name_columns: ["caption"],
  sku_column: null,
  categories: [],
  identifiers: [],
};
const source: DataSource = {
  id: "source-1",
  display_name: "Unfamiliar catalogue",
  adapter_type: "sqlserver_readonly",
  connection_profile_key: "catalogue_reader",
  mapping: null,
  status: "CONFIGURED",
  last_validated_at: null,
  last_successful_health_check_at: null,
  failure_code: null,
  capabilities: [],
  created_at: "2026-10-07T00:00:00Z",
  updated_at: "2026-10-07T00:00:00Z",
};
const review: MappingReview = {
  id: "revision-1",
  version: 1,
  status: "review",
  schema_fingerprint: "a".repeat(64),
  discovery: {
    engine: "sqlserver",
    objects: [
      {
        object_id: "o1",
        schema_name: "x",
        name: "<img src=x onerror=alert(1)>",
        columns: [
          { name: "ref", kind: "text", nullable: false },
          { name: "caption", kind: "text", nullable: true },
        ],
        unique_keys: [["ref"]],
      },
    ],
  },
  proposal: {
    mapping,
    rationale: "Keys are source-backed; labels require review.",
    uncertainties: ["Confirm that captions represent products."],
  },
  approved_mapping: null,
  failure_code: null,
  validation_notes: [],
};

beforeEach(() => {
  sessionStorage.clear();
  vi.spyOn(api, "mappingReviews").mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("keeps independent capability review available without inheriting catalogue approval", async () => {
  const workspace = vi.spyOn(api, "operationalWorkspace");
  render(
    <MappingOnboarding
      businessId="business-1"
      source={{
        ...source,
        catalogue_coverage: {
          approval_type: "partial",
          excluded_records: 10,
          total_records: 9353,
          valid_records: 9343,
        },
      }}
      onRefresh={vi.fn()}
    />,
  );
  expect(await screen.findByText(/Catalogue exclusions do not/))
    .toBeVisible();
  expect(screen.queryByRole("region", { name: "Operational mapping review" }))
    .toBeNull();
  expect(workspace).not.toHaveBeenCalled();
});

it("requires explicit semantic review and renders untrusted metadata as text", async () => {
  vi.mocked(api.mappingReviews).mockResolvedValue([review]);
  const approve = vi
    .spyOn(api, "approveMapping")
    .mockResolvedValue({ ...review, status: "approved", approved_mapping: mapping });
  const refresh = vi.fn().mockResolvedValue(undefined);
  const { container } = render(
    <MappingOnboarding businessId="business-1" source={source} onRefresh={refresh} />,
  );
  const button = await screen.findByRole("button", {
    name: "Validate and approve mapping",
  });
  expect(button).toBeDisabled();
  expect((await screen.findAllByText(/<img src=x/)).length).toBeGreaterThan(0);
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText(/Stock is unknown/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button);
  await waitFor(() =>
    expect(approve).toHaveBeenCalledWith(
      "business-1",
      "source-1",
      "revision-1",
      mapping,
    ),
  );
  expect(refresh).toHaveBeenCalledOnce();
});

it("explains incomplete category keys and preserves approved history for review", async () => {
  const failed: MappingReview = {
    ...review,
    id: "revision-2",
    version: 2,
    status: "failed",
    proposal: null,
    failure_code: "mapping_category_join_not_unique",
  };
  const approved: MappingReview = {
    ...review,
    status: "approved",
    approved_mapping: mapping,
  };
  vi.mocked(api.mappingReviews).mockResolvedValue([failed, approved]);
  render(
    <MappingOnboarding businessId="business-1" source={source} onRefresh={vi.fn()} />,
  );
  expect(await screen.findByText(/does not cover a complete unique key/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Validate and approve mapping" }),
  ).toBeNull();
  fireEvent.change(screen.getByLabelText("Mapping history"), {
    target: { value: "revision-1" },
  });
  expect(screen.getByText(/Owner-approved version/)).toBeVisible();
  expect(screen.getByText(/Identity: ref/)).toBeVisible();
});

it("edits display fields without JSON and requires renewed semantic confirmation", async () => {
  vi.mocked(api.mappingReviews).mockResolvedValue([review]);
  const approve = vi
    .spyOn(api, "approveMapping")
    .mockResolvedValue({ ...review, status: "approved", approved_mapping: mapping });
  render(
    <MappingOnboarding businessId="business-1" source={source} onRefresh={vi.fn()} />,
  );
  const sku = await screen.findByLabelText("SKU column (optional)");
  fireEvent.click(screen.getByRole("checkbox"));
  const button = screen.getByRole("button", { name: "Validate and approve mapping" });
  expect(button).toBeEnabled();
  fireEvent.change(sku, { target: { value: "ref" } });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button);
  await waitFor(() =>
    expect(approve).toHaveBeenCalledWith("business-1", "source-1", "revision-1", {
      ...mapping,
      sku_column: "ref",
    }),
  );
});

it("reuses the proposal key after a lost response", async () => {
  const proposal = vi
    .spyOn(api, "proposeMapping")
    .mockRejectedValueOnce(
      new ApiError(503, "temporary_failure", "Temporary request failure"),
    )
    .mockResolvedValue(review);
  render(
    <MappingOnboarding businessId="business-1" source={source} onRefresh={vi.fn()} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Request mapping proposal" }));
  await screen.findByText("Temporary request failure");
  fireEvent.click(screen.getByRole("button", { name: "Request mapping proposal" }));
  await screen.findByRole("button", { name: "Validate and approve mapping" });
  expect(proposal).toHaveBeenCalledTimes(2);
  expect(proposal.mock.calls[0][2]).toBe(proposal.mock.calls[1][2]);
});

it("offers variants and selects only an explicit source-backed identifier", async () => {
  const search = vi.spyOn(api, "searchCatalogue").mockResolvedValue({
    mapping_version: 3,
    status: "ambiguous",
    truncated: false,
    capabilities: ["products"],
    items: [
      {
        external_product_id: "0001",
        names: ["Cola mini"],
        sku: null,
        categories: [],
        stock: null,
      },
      {
        external_product_id: "0002",
        names: ["Cola diet"],
        sku: null,
        categories: [],
        stock: null,
      },
    ],
  });
  render(
    <MappingOnboarding
      businessId="business-1"
      source={{ ...source, status: "ACTIVE", capabilities: ["products"] }}
      onRefresh={vi.fn()}
    />,
  );
  fireEvent.change(screen.getByLabelText("Search source catalogue"), {
    target: { value: "Cola" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search products" }));
  await screen.findByText("Choose the intended variant.");
  expect(search).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Cola diet" }));
  await waitFor(() =>
    expect(search).toHaveBeenLastCalledWith("business-1", "source-1", {
      external_product_id: "0002",
      mapping_version: 3,
      limit: 50,
    }),
  );
});
