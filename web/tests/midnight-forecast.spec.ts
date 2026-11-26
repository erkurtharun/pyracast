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

async function openToday(page: Page) {
    await page.clock.install({ time: new Date("2026-11-14T20:59:40Z") });
    await mockApi(page);
    const day = { ended: false };
    await page.route("**/plants/21/forecast/**", async (route) => {
        const url = new URL(route.request().url());
        expect(url.searchParams.get("data_source")).toBe("forecast");
        const date = url.searchParams.get("date");
        const chosen = Number(url.searchParams.get("forecast_id")) || null;
        const past = date === "2026-11-14" && day.ended;
        if (past && chosen === 2)
            return route.fulfill({
                status: 400,
                json: {
                    code: "invalid_request",
                    message: "Check the highlighted values and try again.",
                    fields: {
                        forecast_id: "Choose one of this day's forecast times.",
                    },
                },
            });
        const energy = past ? 230 : 237.773;
        await route.fulfill({
            json: {
                date,
                timezone: region.timezone,
                freshness: past ? "archived" : "fresh",
                source_kind: "forecast",
                selection: {
                    mode: chosen ? "manual" : "auto",
                    rule: chosen ? null : "latest",
                    forecast_id: past ? 1 : 2,
                    options: past
                        ? [{ forecast_id: 1, fetched_at: savedAt }]
                        : [
                              { forecast_id: 2, fetched_at: laterAt },
                              { forecast_id: 1, fetched_at: savedAt },
                          ],
                },
                result: {
                    complete: true,
                    energy_mwh: energy,
                    coverage_fraction: 1,
                    quality_flags: [],
                    provenance: { collected_at: past ? savedAt : laterAt },
                    intervals: [
                        {
                            start: "2026-11-14T09:00:00Z",
                            end: "2026-11-14T10:00:00Z",
                            mean_power_mw: energy,
                            energy_mwh: energy,
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
    const panel = page.locator("section.panel");
    return {
        day,
        panel,
        generation: page.locator(".stat", { hasText: "Daily total" }),
        forecast: page.getByRole("radio", { name: "Forecast" }),
        controls: page.locator(".forecast-controls"),
        times: page.getByLabel("Forecast time"),
        dataLine: panel.getByText(/^Forecast data: /),
    };
}

// The query layer hands an answer to the screen on a zero-delay timer, which a paused clock holds;
// flowing time releases it but cannot reach the next 30 s poll before the assertion times out.
async function endTheDay(page: Page, day: { ended: boolean }) {
    await page.clock.pauseAt(new Date("2026-11-14T20:59:50Z"));
    day.ended = true;
    await pollOnce(page);
    await page.clock.resume();
}

test("after midnight the day keeps a forecast saved before its sunrise", async ({
    page,
}) => {
    const { day, panel, generation, forecast, controls, times, dataLine } =
        await openToday(page);
    await expect(generation).toContainText("237.77 MWh");
    await expect(forecast).toBeChecked();
    await expect(times.locator("option")).toHaveText([
        "Automatic",
        plantTime(laterAt),
        plantTime(savedAt),
    ]);
    await expect(controls.locator(".tag")).toHaveCount(0);
    await expect(dataLine).toHaveText(`Forecast data: ${plantTime(laterAt)}`);

    await endTheDay(page, day);
    await expect(dataLine).toHaveText(`Forecast data: ${plantTime(savedAt)}`);
    await expect(times.locator("option")).toHaveText([
        "Automatic",
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
});

test("a time chosen today that is refused after midnight shows the automatic forecast", async ({
    page,
}) => {
    const { day, panel, generation, controls, times, dataLine } =
        await openToday(page);
    const chosen = forecastRead(page);
    await times.selectOption("2");
    await chosen;
    await expect(controls.getByText("Manual", { exact: true })).toBeVisible();
    await expect(generation).toContainText("237.77 MWh");

    await endTheDay(page, day);
    await expect(generation).toContainText("230");
    await expect(dataLine).toHaveText(`Forecast data: ${plantTime(savedAt)}`);
    await expect(times).toHaveValue("");
    await expect(times.locator("option:checked")).toHaveText("Automatic");
    await expect(controls.locator(".tag")).toHaveCount(0);
    await expect(panel).not.toContainText("237.77");
    await page.getByText("Show table", { exact: true }).click();
    await expect(
        page.getByRole("table", { name: "Hourly values" }),
    ).not.toContainText("237.77");
});
