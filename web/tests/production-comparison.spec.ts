import { expect, test } from "./support/test";
import { mockApi } from "./support/workspace";

test("available historical model days appear without empty grid rows", async ({
    page,
}) => {
    await mockApi(page);
    let query = "";
    await page.route("**/plants/21/performance/**", async (route) => {
        query = new URL(route.request().url()).search;
        await route.fulfill({
            json: {
                comparison: "historical_model",
                start_date: "2026-10-12",
                end_date: "2026-11-10",
                matched_days: 8,
                requested_days: 30,
                metrics: {
                    daily_mae_mwh: 2,
                    daily_bias_mwh: 2,
                    source_positive_mae_mw: 0.2,
                },
                baseline_metrics: { daily_mae_mwh: 2 },
                days: Array.from({ length: 30 }, (_, index) => ({
                    date: `2026-${index < 20 ? "10" : "11"}-${String(index < 20 ? index + 12 : index - 19).padStart(2, "0")}`,
                    status: index < 22 ? "missing" : "matched",
                    reason:
                        index < 22 ? "estimated_actuals_not_available" : null,
                    measured_energy_mwh: index < 22 ? null : 10,
                    predicted_energy_mwh: index < 22 ? null : 12,
                    error_mwh: index < 22 ? null : 2,
                    provisional_production: index >= 22,
                    historical_configuration_assumed: index >= 22,
                })),
            },
        });
    });
    await page.goto("/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    const comparison = page.locator("section").filter({
        has: page.getByRole("heading", {
            name: "Production comparison",
            exact: true,
        }),
    });
    await expect(
        comparison.getByRole("combobox", { name: /^Comparison/ }),
    ).toHaveValue("historical_model");
    expect(query).toContain("comparison=historical_model");
    await expect(comparison.getByRole("row")).toHaveCount(9);
    await expect(
        comparison.getByRole("columnheader", { name: "Model (MWh)" }),
    ).toBeVisible();
    await expect(
        comparison.getByText("Provisional production", { exact: false }),
    ).toHaveCount(8);
    await expect(
        comparison.getByText("Includes provisional reported production", {
            exact: false,
        }),
    ).toBeVisible();
    await comparison
        .getByText("Unavailable dates (22)", { exact: true })
        .click();
    await expect(comparison.getByRole("listitem")).toHaveText([
        "Historical weather estimates are not available: 22 days",
    ]);
    await comparison
        .getByRole("combobox", { name: /^Comparison/ })
        .selectOption("archived_forecast");
    await comparison.getByRole("button", { name: "Load performance" }).click();
    await expect.poll(() => query).toContain("comparison=archived_forecast");
});

test("a completely unavailable comparison stays compact", async ({ page }) => {
    await mockApi(page);
    await page.route("**/plants/21/performance/**", async (route) => {
        await route.fulfill({
            json: {
                comparison: "archived_forecast",
                start_date: "2026-10-12",
                end_date: "2026-11-10",
                matched_days: 0,
                requested_days: 30,
                metrics: null,
                baseline_metrics: null,
                days: Array.from({ length: 30 }, (_, index) => ({
                    date: String(index),
                    status: "missing",
                    reason: "configuration_not_available",
                    measured_energy_mwh: null,
                    predicted_energy_mwh: null,
                    error_mwh: null,
                })),
            },
        });
    });
    await page.goto("/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    const comparison = page.locator("section").filter({
        has: page.getByRole("heading", {
            name: "Production comparison",
            exact: true,
        }),
    });
    await expect(
        comparison.getByText("No complete production and reference pairs", {
            exact: false,
        }),
    ).toBeVisible();
    await expect(comparison.getByRole("table")).toHaveCount(0);
    await expect(comparison.locator(".stat")).toHaveCount(0);
    await comparison
        .getByText("Unavailable dates (30)", { exact: true })
        .click();
    await expect(comparison.getByRole("listitem")).toHaveText([
        "Configuration was not available before this day: 30 days",
    ]);
});
