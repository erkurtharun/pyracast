import { expect, test } from "./support/test";
import { mockApi, region } from "./support/workspace";
import type { Page } from "@playwright/test";

// Advances exactly one poll period and waits for the answer of that single poll.
async function pollOnce(page: Page) {
    const polled = page.waitForResponse((response) =>
        response.url().includes("/forecast/?date=2026-11-14"),
    );
    await page.clock.runFor(30_000);
    await polled;
}

test("automatic forecast survives midnight until historical estimates arrive", async ({
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
                        ? { fallback_reason: "estimated_actuals_unavailable" }
                        : {},
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
    const requested = page.waitForResponse((response) =>
        response.url().includes("/forecast/?date=2026-11-14"),
    );
    await page.getByLabel("Forecast date").fill("2026-11-14");
    await requested;
    const generation = page.locator(".stat", { hasText: "Estimated energy" });
    const pending = page.getByText(
        "Saved forecast · Historical estimates pending",
        { exact: true },
    );
    const actualLabel = page.getByText(
        "Satellite/model estimates, not meter readings.",
        { exact: true },
    );
    await expect(generation).toContainText("237.773");
    await expect(pending).toHaveCount(0);
    await page.clock.pauseAt(new Date("2026-11-14T20:59:50Z"));
    completedDay = true;
    await pollOnce(page);
    // The query layer hands an answer to the screen on a zero-delay timer, which a paused clock holds;
    // flowing time releases it but cannot reach the next 30 s poll before the assertion times out.
    await page.clock.resume();
    await expect(pending).toBeVisible();
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-14");
    await expect(page.getByLabel("Data source")).toHaveValue("auto");
    await expect(generation).toContainText("237.773");
    await expect(actualLabel).toHaveCount(0);
    await expect(
        page.getByText("Production comparison is out of date.", {
            exact: true,
        }),
    ).toBeVisible();
    await page.getByText("View interval data (1 interval)").click();
    await expect(
        page.getByRole("table", {
            name: "Forecast intervals",
        }),
    ).toBeVisible();

    historicalEstimatesAvailable = true;
    await pollOnce(page);
    await expect(actualLabel).toBeVisible();
    await expect(pending).toHaveCount(0);
    await expect(generation).toContainText("230");
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-14");
    await expect(
        page.getByRole("table", {
            name: "Estimated actual intervals",
        }),
    ).toBeVisible();
});
