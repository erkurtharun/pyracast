import { expect, test } from "./support/test";
import { mockApi, region } from "./support/workspace";
import type { Page } from "@playwright/test";

// The page's own navigation to the date also requests a .../forecast/?date= address.
function forecastRead(page: Page) {
    return page.waitForResponse((response) =>
        response.url().includes("/plants/21/forecast/?date=2026-11-14"),
    );
}

// Advances exactly one poll period and waits for the answer of that single poll.
async function pollOnce(page: Page) {
    const polled = forecastRead(page);
    await page.clock.runFor(30_000);
    await polled;
}

const liveAt = "2026-11-14T03:00:00Z";
const savedAt = "2026-11-14T09:00:00Z";
const satelliteAt = "2026-11-15T02:00:00Z";
const plantTime = (value: string) =>
    new Intl.DateTimeFormat("en", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: region.timezone,
    }).format(new Date(value));

test("the forecast survives midnight and the data choice names satellite data once it arrives", async ({
    page,
}) => {
    const beforeMidnight = new Date("2026-11-14T20:59:40Z");
    await page.clock.install({ time: beforeMidnight });
    await mockApi(page);
    let historicalEstimatesAvailable = false;
    let completedDay = false;
    await page.route("**/plants/21/forecast/**", async (route) => {
        const url = new URL(route.request().url());
        expect(url.searchParams.get("data_source")).toBe("auto");
        const date = url.searchParams.get("date");
        const actual = date === "2026-11-14" && historicalEstimatesAvailable;
        const fallback = date === "2026-11-14" && completedDay && !actual;
        await route.fulfill({
            json: {
                date,
                timezone: region.timezone,
                freshness: "fresh",
                source_kind: actual ? "estimated_actuals" : "forecast",
                result: {
                    complete: true,
                    energy_mwh: actual ? 230 : 237.773,
                    coverage_fraction: 1,
                    quality_flags: fallback ? ["forecast_fallback"] : [],
                    provenance: fallback
                        ? {
                              fallback_reason: "estimated_actuals_unavailable",
                              collected_at: savedAt,
                          }
                        : { collected_at: actual ? satelliteAt : liveAt },
                    intervals: [
                        {
                            start: "2026-11-14T09:00:00Z",
                            end: "2026-11-14T10:00:00Z",
                            mean_power_mw: actual ? 230 : 237.773,
                            energy_mwh: actual ? 230 : 237.773,
                        },
                    ],
                },
                production_diagnostic: {
                    evidence_state: "stale",
                    days: [],
                },
            },
        });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    const requested = forecastRead(page);
    await page.getByLabel("Forecast date").fill("2026-11-14");
    await requested;
    const generation = page.locator(".stat", { hasText: "Daily total" });
    const choice = page.getByLabel("Data", { exact: true });
    const panel = page.locator("section.panel");
    await expect(generation).toContainText("237.77 MWh");
    await expect(choice).toHaveValue("forecast");
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(liveAt)}`,
    );
    await page.clock.pauseAt(new Date("2026-11-14T20:59:50Z"));
    completedDay = true;
    await pollOnce(page);
    // The query layer hands an answer to the screen on a zero-delay timer, which a paused clock holds;
    // flowing time releases it but cannot reach the next 30 s poll before the assertion times out.
    await page.clock.resume();
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(savedAt)}`,
    );
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-14");
    await expect(choice).toHaveValue("forecast");
    await expect(generation).toContainText("237.77 MWh");
    await expect(
        panel.getByRole("heading", { name: "Generation forecast" }),
    ).toBeVisible();
    await expect(panel.getByText(/^Satellite data: /)).toHaveCount(0);
    await page.getByText("Show table", { exact: true }).click();
    await expect(
        page.getByRole("table", { name: "Hourly values" }),
    ).toBeVisible();

    historicalEstimatesAvailable = true;
    await pollOnce(page);
    await expect(choice).toHaveValue("estimated_actuals");
    await expect(
        panel.getByRole("heading", {
            name: "Production calculated from satellite data",
        }),
    ).toBeVisible();
    await expect(panel.getByText(/^Satellite data: /)).toHaveText(
        `Satellite data: ${plantTime(satelliteAt)}`,
    );
    await expect(panel.getByText(/^Forecast data: /)).toHaveCount(0);
    await expect(generation).toContainText("230");
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-14");
    await expect(
        page.getByRole("table", { name: "Hourly values" }),
    ).toBeVisible();
});
