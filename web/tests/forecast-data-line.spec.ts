import { expect, test, type Page } from "./support/test";
import { mockApi, region } from "./support/workspace";

const collected = "2026-11-14T06:00:00Z";
const shown = new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: region.timezone,
}).format(new Date(collected));

async function openForecast(
    page: Page,
    state: {
        freshness: "fresh" | "stale";
        stale_reason?: "collection_overdue" | "connection_inactive" | null;
        source_kind?: "forecast" | "estimated_actuals";
    },
) {
    await mockApi(page);
    await page.route("**/plants/21/forecast/**", (route) =>
        route.fulfill({
            json: {
                date: "2026-11-14",
                timezone: region.timezone,
                source_kind: state.source_kind ?? "forecast",
                freshness: state.freshness,
                stale_reason: state.stale_reason ?? null,
                result: {
                    complete: true,
                    energy_mwh: 8,
                    intervals: [],
                    provenance: { collected_at: collected },
                },
            },
        }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    return page.locator("section.panel .section-heading .fine");
}

test("a current forecast shows only its data line under the heading", async ({
    page,
}) => {
    const lines = await openForecast(page, { freshness: "fresh" });
    await expect(lines).toHaveText(["2026-11-14", `Forecast data: ${shown}`]);
    await expect(page.locator("section.panel .tag")).toHaveCount(0);
});

test("a forecast whose newer data did not come says so above its data line", async ({
    page,
}) => {
    const lines = await openForecast(page, {
        freshness: "stale",
        stale_reason: "collection_overdue",
    });
    await expect(lines).toHaveText([
        "2026-11-14",
        "Newer forecast data was not available.",
        `Forecast data: ${shown}`,
    ]);
    await expect(page.locator("section.panel .tag")).toHaveCount(0);
});

test("a plant whose forecast data stopped arriving names whom to write to", async ({
    page,
}) => {
    const lines = await openForecast(page, {
        freshness: "stale",
        stale_reason: "connection_inactive",
    });
    await expect(lines).toHaveText([
        "2026-11-14",
        "No new forecast data is arriving for this plant. Write to Pyracast: info@pyracast.app",
        `Forecast data: ${shown}`,
    ]);
});

test("a day from satellite data names it in the heading and the data line", async ({
    page,
}) => {
    const lines = await openForecast(page, {
        freshness: "fresh",
        source_kind: "estimated_actuals",
    });
    await expect(
        page.locator("section.panel").getByRole("heading", {
            name: "Satellite data",
        }),
    ).toBeVisible();
    await expect(lines).toHaveText(["2026-11-14", `Satellite data: ${shown}`]);
});
