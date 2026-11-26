import { expect, test } from "./support/test";
import type { Locator, Page } from "@playwright/test";
import { accessibilityViolations } from "./support/accessibility";
import {
    accountWith,
    answer,
    mockApi,
    sessionIdentity,
    type Mock,
} from "./support/workspace";
import type { components } from "../src/lib/schema";
import { errorBody } from "./support/errors";

type PerformanceDay = Mock<components["schemas"]["PerformanceDay"]>;

const day = (index: number) =>
    `2026-${index < 20 ? "10" : "11"}-${String(index < 20 ? index + 12 : index - 19).padStart(2, "0")}`;
const shownDay = (index: number) =>
    `${index < 20 ? index + 12 : index - 19} ${index < 20 ? "Oct" : "Nov"} 2026`;

async function openComparison(
    page: Page,
    names = { production: "Production", comparison: "Comparison" },
) {
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: names.production, exact: true })
        .click();
    await page
        .getByRole("button", { name: names.comparison, exact: true })
        .click();
    return page.locator("section").filter({
        has: page.getByRole("heading", {
            name: names.comparison,
            exact: true,
        }),
    });
}

test("available satellite data days appear without empty grid rows", async ({
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
                data_source: "satellite",
                start_date: "2026-10-12",
                end_date: "2026-11-10",
                matched_days: 8,
                requested_days: 30,
                relative_error: 0.2,
                daily_mae_mwh: 2,
                daily_bias_mwh: 2,
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
                    }),
                ),
            }),
        });
    });
    const comparison = await openComparison(page);
    await expect.poll(() => query).toContain("data_source=forecast");
    await comparison
        .getByRole("button", { name: "Satellite data", exact: true })
        .click();
    await expect.poll(() => query).toContain("data_source=satellite");
    expect(query).toContain("start_date=2026-10-12&end_date=2026-11-10");
    await expect(comparison.getByText("Advanced", { exact: true })).toHaveCount(
        0,
    );
    await expect(comparison.getByRole("row")).toHaveCount(9);
    await expect(
        comparison
            .locator(".stat")
            .filter({ hasText: "Absolute error as % of production" }),
    ).toContainText("20%");
    await expect(comparison.locator(".stat")).toHaveCount(4);
    await expect(comparison.getByText(/Days not compared/)).toHaveText([
        "Days not compared (22)",
    ]);
    await expect(
        comparison.locator(".stat").filter({
            hasText: "Average daily difference",
        }),
    ).toHaveText("Average daily difference+2.00 MWhSatellite data too high");
    await expect(
        comparison.getByLabel("Start date"),
    ).toHaveAccessibleDescription("Up to 90 days.");
    for (const name of [
        "Satellite data (MWh)",
        "Satellite data − actual (MWh)",
    ])
        await expect(
            comparison.getByRole("columnheader", { name, exact: true }),
        ).toBeVisible();
    await expect(
        comparison.getByRole("table", {
            name: "Satellite data · 12 Oct 2026 – 10 Nov 2026",
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
        `The data to compare with cannot be used: ${shownDay(0)}`,
        `No satellite data for this day: ${Array.from(
            { length: 21 },
            (_, index) => shownDay(index + 1),
        ).join(", ")}`,
    ]);
    await comparison
        .getByRole("button", { name: "Forecast", exact: true })
        .click();
    await expect.poll(() => query).toContain("data_source=forecast");
    expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(320);
    const scan = await accessibilityViolations(page);
    expect(scan).toEqual([]);
    await comparison.getByLabel("Start date").fill("2026-10-22");
    await comparison.getByRole("button", { name: "Show", exact: true }).click();
    await expect
        .poll(() => new URLSearchParams(query).get("start_date"))
        .toBe("2026-10-22");
    await comparison
        .getByRole("button", { name: "Satellite data", exact: true })
        .click();
    await expect.poll(() => query).toContain("data_source=satellite");
    expect(query).toContain("start_date=2026-10-22");
    expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(320);
});

test("the two dates and the button sit on one line with the range's limit under them", async ({
    page,
}) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await mockApi(page);
    const comparison = await openComparison(page);
    const start = comparison.getByLabel("Start date");
    const end = comparison.getByLabel("End date");
    const limit = comparison.getByText("Up to 90 days.", { exact: true });
    await expect(start).toHaveAccessibleDescription("Up to 90 days.");
    await expect(end).toHaveAccessibleDescription("Up to 90 days.");
    await expect(limit).toHaveCount(1);

    const box = async (element: Locator) => (await element.boundingBox())!;
    const bottom = async (element: Locator) => {
        const { y, height } = await box(element);
        return y + height;
    };
    const show = comparison.getByRole("button", { name: "Show", exact: true });
    const line = await bottom(start);
    expect(await bottom(end)).toBeCloseTo(line, 0);
    expect(await bottom(show)).toBeCloseTo(line, 0);
    expect((await box(limit)).y).toBeGreaterThanOrEqual(line);
});

test("the comparison offers the forecast screen's two data names in Turkish", async ({
    page,
}) => {
    const account = await mockApi(page);
    account.current = accountWith({ language_preference: "tr" });
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { ...sessionIdentity(account.current), language: "tr" },
        }),
    );
    const comparison = await openComparison(page, {
        production: "Üretim",
        comparison: "Karşılaştırma",
    });
    await expect(
        comparison
            .getByRole("navigation", { name: "Karşılaştırma" })
            .getByRole("button"),
    ).toHaveText(["Tahmin", "Uydu verisi"]);
});

test("a completely unavailable comparison stays compact", async ({ page }) => {
    await mockApi(page);
    await page.route("**/plants/21/performance/**", async (route) => {
        await route.fulfill({
            json: answer("PerformanceOutput", {
                data_source: "forecast",
                start_date: "2026-10-12",
                end_date: "2026-11-10",
                matched_days: 0,
                requested_days: 30,
                relative_error: null,
                daily_mae_mwh: null,
                daily_bias_mwh: null,
                days: Array.from(
                    { length: 30 },
                    (_, index): PerformanceDay => ({
                        date: day(index),
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
    const comparison = await openComparison(page);
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
            (_, index) => shownDay(index),
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
                  json: answer("PerformanceOutput", {
                      data_source: "forecast",
                      start_date: "2026-11-09",
                      end_date: "2026-11-10",
                      matched_days: 1,
                      requested_days: 2,
                      daily_mae_mwh: 2,
                      daily_bias_mwh: 2,
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
                  }),
              }),
    );
    await openComparison(page);
    const missing = page.getByText("Days not compared (1)", { exact: true });
    const counts = page.getByText(/Days not compared/);
    await expect(missing).toBeVisible();
    await expect(counts).toHaveText(["Days not compared (1)"]);
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
    await expect(counts).toHaveText(["Days not compared (1)"]);
    unavailable = false;
    await notice
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(notice).toHaveCount(0);
    await expect(missing).toBeVisible();
    await expect(counts).toHaveText(["Days not compared (1)"]);
});

test("a forecast comparison signs the daily difference, counts no day not compared, and marks only a day on real-time production", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-11-11T12:00:00Z"));
    await mockApi(page);
    await page.route("**/plants/21/performance/**", (route) =>
        route.fulfill({
            json: answer("PerformanceOutput", {
                data_source: "forecast",
                start_date: "2026-11-09",
                end_date: "2026-11-10",
                matched_days: 2,
                requested_days: 2,
                relative_error: 0.1,
                daily_mae_mwh: 1,
                daily_bias_mwh: -1,
                days: ["2026-11-09", "2026-11-10"].map(
                    (date, index): PerformanceDay => ({
                        date,
                        status: "matched",
                        reason: null,
                        measured_energy_mwh: 10,
                        predicted_energy_mwh: 9,
                        error_mwh: -1,
                        provisional_production: index === 1,
                    }),
                ),
            }),
        }),
    );
    const comparison = await openComparison(page);
    await expect(
        comparison.locator(".stat").filter({
            hasText: "Average daily difference",
        }),
    ).toHaveText("Average daily difference−1.00 MWhForecast too low");
    await expect(
        comparison.getByText("Days not compared (0)", { exact: true }),
    ).toBeVisible();
    await expect(
        comparison.locator("summary", { hasText: "Days not compared" }),
    ).toHaveCount(0);
    await expect(
        comparison.getByRole("table", {
            name: "Forecast · 9 Nov 2026 – 10 Nov 2026",
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
    ).toHaveCount(1);
    await expect(
        comparison.getByRole("cell", {
            name: "Real-time (EPİAŞ)",
            exact: true,
        }),
    ).toHaveCount(1);
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
                data_source: "forecast",
                start_date: "2026-10-12",
                end_date: "2026-11-10",
                matched_days: 0,
                requested_days: 30,
                relative_error: null,
                daily_mae_mwh: null,
                daily_bias_mwh: null,
                days: [],
            }),
        }),
    );
    const comparison = await openComparison(page);
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
