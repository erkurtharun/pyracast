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
                        provisional_snapshot_ids: index >= 22 ? [index] : [],
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
            name: "Comparison",
            exact: true,
        }),
    });
    await expect.poll(() => query).toContain("comparison=archived_forecast");
    await comparison
        .getByRole("button", {
            name: "Weather-based expected production",
        })
        .click();
    await expect.poll(() => query).toContain("comparison=historical_model");
    expect(query).toContain("start_date=2026-10-12&end_date=2026-11-10");
    await expect(comparison.getByText("Advanced", { exact: true })).toHaveCount(
        0,
    );
    await expect(comparison.getByRole("row")).toHaveCount(9);
    await expect(
        comparison
            .locator(".stat")
            .filter({ hasText: "Absolute error as % of production" }),
    ).toContainText("20 %");
    await expect(
        comparison.locator(".stat").filter({
            hasText: "Improvement over forecasts without calibration",
        }),
    ).toContainText("12.5 %");
    await expect(
        comparison.getByText(
            "The improvement uses 3 days outside the calibration days.",
        ),
    ).toBeVisible();
    await expect(comparison.getByText(/Days not compared/)).toHaveText([
        "Days not compared (22)",
    ]);
    await expect(
        comparison.locator(".stat").filter({ hasText: "Excluded days" }),
    ).toHaveCount(0);
    await expect(
        comparison.locator(".stat").filter({
            hasText: "Average daily difference (expected − actual)",
        }),
    ).toContainText("2");
    await comparison.getByText("Additional metrics", { exact: true }).click();
    await expect(
        comparison.getByText("Compared: 64 intervals · 64 hours"),
    ).toBeVisible();
    await expect(
        comparison.getByRole("columnheader", { name: "Expected (MWh)" }),
    ).toBeVisible();
    await expect(
        comparison.getByRole("columnheader", {
            name: "Expected − actual (MWh)",
        }),
    ).toBeVisible();
    await expect(
        comparison.getByRole("table", {
            name: "Weather-based expected production · 2026-10-12 – 2026-11-10",
        }),
    ).toBeVisible();
    await expect(
        comparison.getByRole("cell", {
            name: "Real-time (EPİAŞ)",
            exact: true,
        }),
    ).toHaveCount(8);
    await expect(
        comparison.getByText("Includes EPİAŞ real-time production.", {
            exact: true,
        }),
    ).toBeVisible();
    await comparison
        .getByText("Days not compared (22)", { exact: true })
        .click();
    await expect(comparison.getByRole("listitem")).toHaveText([
        `The data to compare with cannot be used: ${day(0)}`,
        `No satellite data for this day: ${Array.from(
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
    await comparison
        .getByLabel(
            "Use the last forecast available by this time on the previous day",
        )
        .fill("09:30");
    await comparison.getByLabel("Start date").fill("2026-10-22");
    await comparison.getByRole("button", { name: "Show", exact: true }).click();
    await expect
        .poll(() => new URLSearchParams(query).get("issuance_cutoff"))
        .toBe("09:30");
    await comparison
        .getByRole("button", {
            name: "Weather-based expected production",
        })
        .click();
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
            name: "Comparison",
            exact: true,
        }),
    });
    await expect(
        comparison.getByText(
            "No day in this date range can be compared. Choose other dates or check the actual production.",
            { exact: true },
        ),
    ).toBeVisible();
    await expect(comparison.getByRole("table")).toHaveCount(0);
    await expect(comparison.locator(".stat")).toHaveCount(0);
    await comparison
        .getByText("Days not compared (30)", { exact: true })
        .click();
    await expect(comparison.getByRole("listitem")).toHaveText([
        `No forecast to compare for this day: ${Array.from(
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
                              provisional_snapshot_ids: [],
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
    const missing = page.getByText("Days not compared (1)", { exact: true });
    await expect(missing).toBeVisible();
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
    await expect(missing).toBeVisible();
    unavailable = false;
    await notice
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(notice).toHaveCount(0);
    await expect(missing).toBeVisible();
});

test("a forecast accuracy comparison signs the daily difference, and an entered day whose source names real-time production is not marked real-time", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-11-11T12:00:00Z"));
    await mockApi(page);
    await page.route("**/plants/21/performance/**", (route) =>
        route.fulfill({
            json: answer("PerformanceOutput", {
                comparison: "archived_forecast",
                start_date: "2026-11-09",
                end_date: "2026-11-10",
                matched_days: 2,
                requested_days: 2,
                relative_error: 0.1,
                skill_score: null,
                skill_days: 0,
                metrics: {
                    daily_mae_mwh: 1,
                    daily_bias_mwh: -1,
                    source_positive_mae_mw: 0.1,
                },
                baseline_metrics: null,
                days: ["2026-11-09", "2026-11-10"].map(
                    (date, index): PerformanceDay => ({
                        date,
                        status: "matched",
                        reason: null,
                        measured_energy_mwh: 10,
                        predicted_energy_mwh: 9,
                        error_mwh: -1,
                        provisional_production: index === 1,
                        manual_observation_ids: index === 1 ? [5] : [],
                        settlement_snapshot_ids: index === 1 ? [] : [3],
                        provisional_snapshot_ids: [],
                    }),
                ),
            }),
        }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    const comparison = page.locator("section").filter({
        has: page.getByRole("heading", { name: "Comparison", exact: true }),
    });
    await expect(
        comparison.locator(".stat").filter({
            hasText: "Average daily difference (forecast − actual)",
        }),
    ).toContainText("-1");
    await expect(
        comparison.getByRole("table", {
            name: "Forecast accuracy · 2026-11-09 – 2026-11-10",
        }),
    ).toBeVisible();
    for (const name of [
        "Actual (MWh)",
        "Forecast (MWh)",
        "Forecast − actual (MWh)",
    ])
        await expect(
            comparison.getByRole("columnheader", { name, exact: true }),
        ).toBeVisible();
    await expect(
        comparison.getByRole("cell", { name: "Compared", exact: true }),
    ).toHaveCount(2);
    await expect(
        comparison.getByText(/Includes EPİAŞ real-time production|Real-time/),
    ).toHaveCount(0);
    await expect(
        comparison.getByRole("img", { name: "Absolute error by day" }),
    ).toBeVisible();
});

test("a range with requested days and none compared counts the days not compared", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/plants/21/performance/**", (route) =>
        route.fulfill({
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
                days: [],
            }),
        }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    const comparison = page.locator("section").filter({
        has: page.getByRole("heading", { name: "Comparison", exact: true }),
    });
    await expect(
        comparison.getByText(
            "No day in this date range can be compared. Choose other dates or check the actual production.",
            { exact: true },
        ),
    ).toBeVisible();
    await expect(
        comparison.getByText("Days not compared (30)", { exact: true }),
    ).toBeVisible();
});
