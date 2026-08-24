import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, Business, ConnectionProfile, DataSource } from "./api";
import { DataSourcesPage } from "./data-sources";

const business: Business = {
  id: "business-1",
  name: "Waked Market",
  description: "A neighborhood minimarket.",
  category: "GROCERY_SUPERMARKET",
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
};

const profile: ConnectionProfile = {
  key: "fake_store_postgresql",
  display_name: "PostgreSQL Demo Store",
  description: "Read-only Lebanese minimarket demonstration source.",
  adapter_type: "postgresql_readonly",
  mapping: {
    key: "fake_store_minimarket",
    version: 1,
    display_name: "Lebanese Minimarket POS Mapping",
    completed_sale_statuses: ["COMPLETED", "RETURNED"],
    excluded_sale_statuses: ["PENDING", "CANCELLED"],
    return_treatment: "Completed refunds subtract quantity and revenue.",
    active_reservation_statuses: ["ACTIVE"],
    reservation_treatment: "Active, unexpired reservations reduce stock.",
    branch_meaning: "A customer-facing sales location.",
    warehouse_meaning: "A stock-holding location.",
    quantity_interpretation: "On hand minus valid reservations.",
    revenue_interpretation: "Gross lines minus completed refunds.",
    currency: "LBP",
    source_timezone: "Asia/Beirut",
  },
  capabilities: [
    "products",
    "inventory",
    "sales_summaries",
    "best_sellers",
    "restocking_recommendations",
  ],
};

function configuredSource(overrides: Partial<DataSource> = {}): DataSource {
  return {
    id: "source-1",
    display_name: "Hamra demo store",
    adapter_type: "postgresql_readonly",
    connection_profile_key: "fake_store_postgresql",
    mapping: profile.mapping,
    status: "CONFIGURED",
    last_validated_at: null,
    last_successful_health_check_at: null,
    failure_code: null,
    capabilities: profile.capabilities,
    created_at: "2026-08-24T08:00:00Z",
    updated_at: "2026-08-24T08:00:00Z",
    ...overrides,
  };
}

function mockLoad(sources: DataSource[] = []) {
  vi.spyOn(api, "dataSources").mockResolvedValueOnce(sources);
  vi.spyOn(api, "dataSourceProfiles").mockResolvedValueOnce([profile]);
}

describe("Data Sources management", () => {
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1280,
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders the responsive truthful empty state and approved source", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 375,
    });
    mockLoad();

    render(<DataSourcesPage business={business} />);

    expect(
      await screen.findByRole("heading", {
        name: "Connect trusted live business data",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("PostgreSQL Demo Store")).toBeInTheDocument();
    expect(screen.getByText("Read-only database role")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect demo source" })).toBeVisible();
    expect(screen.queryByLabelText(/password|host|port|database url|sql/i)).toBeNull();
  });

  it("configures only the selected safe profile and mapping", async () => {
    mockLoad();
    const created = configuredSource({ display_name: "Beirut Demo" });
    const create = vi.spyOn(api, "createDataSource").mockResolvedValueOnce(created);
    render(<DataSourcesPage business={business} />);

    fireEvent.click(await screen.findByRole("button", { name: "Connect demo source" }));
    const name = screen.getByLabelText("Display name");
    fireEvent.change(name, { target: { value: "Beirut Demo" } });
    expect(screen.getByLabelText("Connection profile")).toBeDisabled();
    expect(screen.getByText(/Lebanese Minimarket POS Mapping/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save configuration" }));

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith("business-1", {
        display_name: "Beirut Demo",
        connection_profile_key: "fake_store_postgresql",
        mapping_profile_key: "fake_store_minimarket",
        mapping_profile_version: 1,
      }),
    );
    expect(
      await screen.findByText("Data source configured. Validate it before activation."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Beirut Demo" })).toBeInTheDocument();
  });

  it("validates and activates a configured source", async () => {
    const configured = configuredSource();
    const validated = configuredSource({
      status: "VALIDATED",
      last_validated_at: "2026-08-24T10:30:00Z",
      last_successful_health_check_at: "2026-08-24T10:30:00Z",
    });
    const active = configuredSource({
      ...validated,
      status: "ACTIVE",
    });
    mockLoad([configured]);
    const validate = vi
      .spyOn(api, "validateDataSource")
      .mockResolvedValueOnce(validated);
    const activate = vi.spyOn(api, "activateDataSource").mockResolvedValueOnce(active);
    render(<DataSourcesPage business={business} />);

    fireEvent.click(await screen.findByRole("button", { name: "Validate" }));
    await waitFor(() =>
      expect(validate).toHaveBeenCalledWith("business-1", "source-1"),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Activate" }));

    await waitFor(() =>
      expect(activate).toHaveBeenCalledWith("business-1", "source-1"),
    );
    expect(await screen.findByText("Data source activated.")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    for (const label of [
      "Products",
      "Current inventory",
      "Sales summaries",
      "Best sellers",
      "Restocking recommendations",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("tests the connection and renders a safe unhealthy state", async () => {
    const active = configuredSource({
      status: "ACTIVE",
      last_validated_at: "2026-08-24T10:30:00Z",
      last_successful_health_check_at: "2026-08-24T10:30:00Z",
    });
    const unhealthy = configuredSource({
      ...active,
      status: "UNHEALTHY",
      failure_code: "operational_source_unavailable",
    });
    mockLoad([active]);
    const check = vi.spyOn(api, "checkDataSource").mockResolvedValueOnce(unhealthy);
    render(<DataSourcesPage business={business} />);

    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    await waitFor(() => expect(check).toHaveBeenCalledWith("business-1", "source-1"));
    expect(await screen.findByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByText("Connection needs attention")).toBeInTheDocument();
    expect(screen.getByText("Connection test failed safely.")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /postgresql:\/\/|password|select \*/i,
    );
  });

  it("requires confirmation before disabling and renders the disabled result", async () => {
    const active = configuredSource({
      status: "ACTIVE",
      last_validated_at: "2026-08-24T10:30:00Z",
      last_successful_health_check_at: "2026-08-24T10:30:00Z",
    });
    const disabled = configuredSource({
      ...active,
      status: "DISABLED",
      failure_code: null,
    });
    mockLoad([active]);
    const disable = vi.spyOn(api, "disableDataSource").mockResolvedValueOnce(disabled);
    render(<DataSourcesPage business={business} />);

    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(disable).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Disable source" }));

    await waitFor(() => expect(disable).toHaveBeenCalledWith("business-1", "source-1"));
    expect(await screen.findByText("Data source disabled.")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Validate again" })).toBeInTheDocument();
  });

  it("shows loading and safe load-error states", async () => {
    let rejectLoad: ((reason: unknown) => void) | undefined;
    vi.spyOn(api, "dataSources").mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectLoad = reject;
        }),
    );
    vi.spyOn(api, "dataSourceProfiles").mockResolvedValueOnce([profile]);
    render(<DataSourcesPage business={business} />);

    expect(screen.getByLabelText("Loading data sources")).toBeInTheDocument();
    rejectLoad?.(
      new ApiError(
        503,
        "operational_source_unavailable",
        "postgresql://readonly:secret@private/store",
      ),
    );

    expect(
      await screen.findByText(
        "The source could not be reached. Check the local service and try again.",
      ),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("postgresql://readonly:secret");
  });
});
