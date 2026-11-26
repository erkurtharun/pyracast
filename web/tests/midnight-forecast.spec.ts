import { expect, test } from "./support/test";
import { mockApi, region } from "./support/workspace";
import type { Page } from "@playwright/test";

function forecastRead(page: Page) {
    return page.waitForResponse((response) => {
        const url = new URL(response.url());
        return (
            url.pathname ===
                `/api/tenants/1/regions/${region.id}/plants/21/forecast/` &&
            url.searchParams.get("date") === "2026-11-14"
        );
    });
}

// Advances exactly one poll period and waits for the answer of that single poll.
async function pollOnce(page: Page) {
    const polled = forecastRead(page);
    await page.clock.runFor(30_000);
    await polled;
}

const savedAt = "2026-11-14T03:00:00Z";
const laterAt = "2026-11-14T09:00:00Z";
const plantTime = (value: string) =>
    new Intl.DateTimeFormat("en", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: region.timezone,
    }).format(new Date(value));

test("after midnight the day keeps a forecast saved before its sunrise and names how it was chosen", async ({
    page,
}) => {
    const beforeMidnight = new Date("2026-11-14T20:59:40Z");
    await page.clock.install({ time: beforeMidnight });
    await mockApi(page);
    let dayEnded = false;
    let productionArrived = false;
    await page.route("**/plants/21/forecast/**", async (route) => {
        const url = new URL(route.request().url());
        expect(url.searchParams.get("data_source")).toBe("forecast");
        const date = url.searchParams.get("date");
        const past = date === "2026-11-14" && dayEnded;
        await route.fulfill({
            json: {
                date,
                timezone: region.timezone,
                freshness: past ? "archived" : "fresh",
                source_kind: "forecast",
                selection: past
                    ? {
                          mode: "auto",
                          rule: productionArrived
                              ? "closest_to_production"
                              : "latest",
                          forecast_id: 1,
                          options: [{ forecast_id: 1, fetched_at: savedAt }],
                      }
                    : {
                          mode: "auto",
                          rule: "latest",
                          forecast_id: 2,
                          options: [
                              { forecast_id: 2, fetched_at: laterAt },
                              { forecast_id: 1, fetched_at: savedAt },
                          ],
                      },
                result: {
                    complete: true,
                    energy_mwh: past ? 230 : 237.773,
                    coverage_fraction: 1,
                    quality_flags: [],
                    provenance: { collected_at: past ? savedAt : laterAt },
                    intervals: [
                        {
                            start: "2026-11-14T09:00:00Z",
                            end: "2026-11-14T10:00:00Z",
                            mean_power_mw: past ? 230 : 237.773,
                            energy_mwh: past ? 230 : 237.773,
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
    const forecast = page.getByRole("radio", { name: "Forecast" });
    const controls = page.locator(".forecast-controls");
    const times = page.getByLabel("Forecast time");
    const panel = page.locator("section.panel");
    await expect(generation).toContainText("237.77 MWh");
    await expect(forecast).toBeChecked();
    await expect(controls.getByText("Auto: latest forecast")).toBeVisible();
    await expect(times.locator("option")).toHaveText([
        "Auto",
        plantTime(laterAt),
        plantTime(savedAt),
    ]);
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(laterAt)}`,
    );
    await page.clock.pauseAt(new Date("2026-11-14T20:59:50Z"));
    dayEnded = true;
    await pollOnce(page);
    // The query layer hands an answer to the screen on a zero-delay timer, which a paused clock holds;
    // flowing time releases it but cannot reach the next 30 s poll before the assertion times out.
    await page.clock.resume();
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(savedAt)}`,
    );
    await expect(times.locator("option")).toHaveText([
        "Auto",
        plantTime(savedAt),
    ]);
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-14");
    await expect(forecast).toBeChecked();
    await expect(generation).toContainText("230");
    await expect(
        panel.getByRole("heading", { name: "Generation forecast" }),
    ).toBeVisible();
    await expect(panel.getByText(/^Satellite data: /)).toHaveCount(0);
    await page.getByText("Show table", { exact: true }).click();
    await expect(
        page.getByRole("table", { name: "Hourly values" }),
    ).toBeVisible();

    productionArrived = true;
    await pollOnce(page);
    await expect(
        controls.getByText("Auto: closest to production"),
    ).toBeVisible();
    await expect(forecast).toBeChecked();
    await expect(panel.getByText(/^Satellite data: /)).toHaveCount(0);
    await expect(
        page.getByRole("table", { name: "Hourly values" }),
    ).toBeVisible();
});
