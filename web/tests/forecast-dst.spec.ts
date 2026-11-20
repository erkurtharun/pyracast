import { expect, test, type Page } from "./support/test";
import { mockApi, paged, plant, region } from "./support/workspace";

const zone = "Europe/Berlin";

async function openDay(page: Page, date: string, starts: string[]) {
    await page.clock.setFixedTime(new Date(`${date}T00:00:00Z`));
    await mockApi(page);
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({
            json: paged([{ ...region, timezone: zone, plant }]),
        }),
    );
    await page.route("**/plants/21/forecast/**", (route) =>
        route.fulfill({
            json: {
                date,
                timezone: zone,
                freshness: "fresh",
                source_kind: "forecast",
                configuration_id: 31,
                result: {
                    complete: true,
                    energy_mwh: starts.length,
                    coverage_fraction: 1,
                    quality_flags: [],
                    intervals: starts.map((start) => ({
                        start,
                        end: new Date(
                            Date.parse(start) + 3_600_000,
                        ).toISOString(),
                        mean_power_mw: 1,
                        energy_mwh: 1,
                        quality_flags: [],
                    })),
                },
            },
        }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByText(`View interval data (${starts.length} intervals)`)
        .click();
    return page.getByRole("table");
}

test("interval labels name the offset across the missing spring hour", async ({
    page,
}) => {
    const table = await openDay(page, "2026-03-29", [
        "2026-03-29T00:00:00.000Z",
        "2026-03-29T01:00:00.000Z",
    ]);
    await expect(table).toContainText("01:00 GMT+1–03:00 GMT+2");
    await expect(table).toContainText("03:00 GMT+2–04:00 GMT+2");
    await expect(table).not.toContainText("02:00");
});

test("interval labels tell the repeated autumn hour apart by its offset", async ({
    page,
}) => {
    const table = await openDay(page, "2026-10-25", [
        "2026-10-25T00:00:00.000Z",
        "2026-10-25T01:00:00.000Z",
    ]);
    await expect(table).toContainText("02:00 GMT+2–02:00 GMT+1");
    await expect(table).toContainText("02:00 GMT+1–03:00 GMT+1");
});
