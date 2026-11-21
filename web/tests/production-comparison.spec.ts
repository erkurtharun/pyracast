import { expect, test } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { answer, mockApi, type Mock } from "./support/workspace";
import type { components } from "../src/lib/schema";
import { errorBody } from "./support/errors";

type PerformanceDay = Mock<components["schemas"]["PerformanceDay"]>;

const day = (index: number) =>
    `2026-${index < 20 ? "10" : "11"}-${String(index < 20 ? index + 12 : index - 19).padStart(2, "0")}`;

test("available historical model days appear without empty grid rows", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-11-11T12:00:00Z"));
    await page.setViewportSize({ width: 320, height: 844 });
    await mockApi(page);
    let query = "";
    await page.route("**/plants/21/performance/**", async (route) => {
        query = new URL(route.request().url()).search;
        await route.fulfill({
            json: answer("PerformanceOutput", {
                comparison: "historical_model",
                start_date: "2026-10-12",
                end_date: "2026-11-10",
                matched_days: 8,
                requested_days: 30,
                relative_error: 0.2,
                skill_score: 0.125,
                skill_days: 3,
                missing_days: 21,
                excluded_days: 1,
                scored_intervals: 64,
                scored_hours: 64,
                metrics: {
                    daily_mae_mwh: 2,
                    daily_bias_mwh: 2,
                    source_positive_mae_mw: 0.2,
                },
                baseline_metrics: { daily_mae_mwh: 2 },
                days: Array.from(
                    { length: 30 },
                    (_, index): PerformanceDay => ({
                        date: day(index),
                        status: index < 22 ? "missing" : "matched",
                        reason:
                            index === 0
                                ? "invalid_forecast_data"
                                : index < 22
                                  ? "estimated_actuals_not_available"
                                  : null,
                        measured_energy_mwh: index < 22 ? null : 10,
                        predicted_energy_mwh: index < 22 ? null : 12,
                        error_mwh: index < 22 ? null : 2,
                        provisional_production: index >= 22,
                        historical_configuration_assumed: index >= 22,
                    }),
                ),
            }),
        });
    });
    await page.goto("/app/");
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
    await expect.poll(() => query).toContain("comparison=archived_forecast");
    await comparison.getByRole("button", { name: "Model check" }).click();
    await expect.poll(() => query).toContain("comparison=historical_model");
    expect(query).toContain("start_date=2026-10-12&end_date=2026-11-10");
    await expect(comparison.getByText("Advanced", { exact: true })).toHaveCount(
        0,
    );
    await expect(comparison.getByRole("row")).toHaveCount(9);
    await expect(
        comparison.locator(".stat").filter({ hasText: "Relative error" }),
    ).toContainText("20 %");
    await expect(
        comparison
            .locator(".stat")
            .filter({ hasText: "Skill vs uncalibrated model" }),
    ).toContainText("12.5 %");
    await expect(
        comparison.getByText(
            "Skill uses 3 days outside the calibration's training period.",
        ),
    ).toBeVisible();
    await expect(
        comparison.locator(".stat").filter({ hasText: "Missing days" }),
    ).toContainText("21");
    await expect(
        comparison.locator(".stat").filter({ hasText: "Excluded days" }),
    ).toContainText("1");
    await comparison
        .getByText("Additional metrics & interpretation", { exact: true })
        .click();
    await expect(
        comparison.getByText("Scored: 64 intervals / 64 hours."),
    ).toBeVisible();
    await expect(
        comparison.getByRole("columnheader", { name: "Model (MWh)" }),
    ).toBeVisible();
    await expect(
        comparison.getByRole("cell", {
            name: "Provisional production",
            exact: true,
        }),
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
        `Saved reference data is invalid: ${day(0)}`,
        `Historical weather estimates are not available: ${Array.from(
            { length: 21 },
            (_, index) => day(index + 1),
        ).join(", ")}`,
    ]);
    await comparison.getByRole("button", { name: "Forecast accuracy" }).click();
    await expect.poll(() => query).toContain("comparison=archived_forecast");
    expect(query).not.toContain("issuance_cutoff");
    expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(320);
    const scan = await accessibilityViolations(page);
    expect(scan).toEqual([]);
    await comparison.getByText("Advanced", { exact: true }).click();
    await comparison.getByLabel("Previous-day cutoff").fill("09:30");
    await comparison.getByLabel("Start date").fill("2026-10-22");
    await comparison.getByRole("button", { name: "Load performance" }).click();
    await expect
        .poll(() => new URLSearchParams(query).get("issuance_cutoff"))
        .toBe("09:30");
    await comparison.getByRole("button", { name: "Model check" }).click();
    await expect.poll(() => query).toContain("comparison=historical_model");
    expect(query).toContain("start_date=2026-10-22");
    expect(query).not.toContain("issuance_cutoff");
    expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(320);
});

test("a completely unavailable comparison stays compact", async ({ page }) => {
    await mockApi(page);
    await page.route("**/plants/21/performance/**", async (route) => {
        await route.fulfill({
            json: answer("PerformanceOutput", {
                comparison: "archived_forecast",
                start_date: "2026-10-12",
                end_date: "2026-11-10",
                matched_days: 0,
                requested_days: 30,
                relative_error: null,
                skill_score: null,
                skill_days: 0,
                metrics: null,
                baseline_metrics: null,
                days: Array.from(
                    { length: 30 },
                    (_, index): PerformanceDay => ({
                        date: String(index),
                        status: "missing",
                        reason: "forecast_not_available",
                        measured_energy_mwh: null,
                        predicted_energy_mwh: null,
                        error_mwh: null,
                    }),
                ),
            }),
        });
    });
    await page.goto("/app/");
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
        `No forecast was archived for this day: ${Array.from(
            { length: 30 },
            (_, index) => String(index),
        ).join(", ")}`,
    ]);
});

test("a failed refresh on return keeps the comparison until a retry loads it", async ({
    page,
}) => {
    await page.clock.install({ time: new Date("2026-11-11T12:00:00Z") });
    await mockApi(page);
    let unavailable = false;
    await page.route("**/plants/21/performance/**", (route) =>
        unavailable
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "Comparison service is busy."),
              })
            : route.fulfill({
                  json: {
                      comparison: "archived_forecast",
                      start_date: "2026-11-09",
                      end_date: "2026-11-10",
                      matched_days: 1,
                      requested_days: 2,
                      missing_days: 1,
                      metrics: {
                          daily_mae_mwh: 2,
                          daily_bias_mwh: 2,
                          source_positive_mae_mw: 0.2,
                      },
                      baseline_metrics: { daily_mae_mwh: 3 },
                      days: [
                          {
                              date: "2026-11-09",
                              status: "missing",
                              reason: "forecast_not_available",
                              measured_energy_mwh: null,
                              predicted_energy_mwh: null,
                              error_mwh: null,
                          },
                          {
                              date: "2026-11-10",
                              status: "matched",
                              reason: null,
                              measured_energy_mwh: 10,
                              predicted_energy_mwh: 12,
                              error_mwh: 2,
                          },
                      ],
                  },
              }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    const missing = page.locator(".stat").filter({ hasText: "Missing days" });
    await expect(missing).toContainText("1");
    await page.clock.runFor(5_000);
    unavailable = true;
    const failed = page.waitForResponse(
        (response) =>
            response.url().includes("/performance/") &&
            response.status() === 503,
    );
    await page.evaluate(() =>
        document.dispatchEvent(
            new Event("visibilitychange", { bubbles: true }),
        ),
    );
    await failed;
    const notice = page
        .getByRole("alert")
        .filter({ hasText: "Could not refresh" });
    await expect(notice).toContainText("Comparison service is busy.");
    await expect(missing).toContainText("1");
    unavailable = false;
    await notice
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(notice).toHaveCount(0);
    await expect(missing).toContainText("1");
});
