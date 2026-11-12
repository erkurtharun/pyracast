import { expect, test } from "./support/test";
import { readFile } from "node:fs/promises";
import { mockApi, region } from "./support/workspace";

test("switching forecast views waits for data with the matching units", async ({
    page,
}) => {
    await mockApi(page);
    let release!: () => void;
    let started!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    const observed = new Promise<void>((resolve) => {
        started = resolve;
    });
    await page.route("**/plants/21/forecast/**", (route) =>
        route.fulfill({
            json: {
                timezone: region.timezone,
                freshness: "fresh",
                source_kind: "forecast",
                result: { complete: true, energy_mwh: 10, intervals: [] },
            },
        }),
    );
    await page.route("**/regions/11/forecast/**", async (route) => {
        started();
        await gate;
        await route.fulfill({
            json: {
                timezone: region.timezone,
                freshness: "fresh",
                source_kind: "forecast",
                result: { complete: true, energy_kwh_m2: 4, intervals: [] },
            },
        });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(
        page.getByRole("heading", { name: "Electrical generation" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    await observed;
    try {
        await expect(
            page.getByRole("heading", { name: "Irradiance", exact: true }),
        ).toHaveCount(0);
        await expect(
            page.getByText("Loading irradiance forecast…", { exact: true }),
        ).toBeVisible();
    } finally {
        release();
    }
    await expect(
        page.getByRole("heading", { name: "Irradiance", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("kWh/m²", { exact: true })).toBeVisible();
});

for (const width of [320, 390]) {
    test(`mixed forecast coverage remains visible at ${width}px`, async ({
        page,
    }) => {
        await page.setViewportSize({ width, height: 800 });
        await mockApi(page);
        await page.route("**/regions/11/forecast/**", (route) =>
            route.fulfill({
                json: {
                    region_id: 11,
                    date: "2026-11-11",
                    timezone: region.timezone,
                    source_kind: "forecast",
                    freshness: "fresh",
                    collected_at: "2026-11-11T05:00:00Z",
                    result: {
                        complete: true,
                        coverage_fraction: 1,
                        quality_flags: ["mixed_forecast_vintages"],
                        provenance: {
                            snapshot_id: 8,
                            source_snapshots: [
                                {
                                    snapshot_id: 6,
                                    collected_at: "2026-11-11T02:00:00Z",
                                },
                                {
                                    snapshot_id: 8,
                                    collected_at: "2026-11-11T05:00:00Z",
                                },
                            ],
                        },
                        intervals: [
                            {
                                start: "2026-11-11T04:00:00Z",
                                end: "2026-11-11T04:30:00Z",
                                ghi_wm2: 100,
                                source_snapshot_ids: [6],
                            },
                            {
                                start: "2026-11-11T07:30:00Z",
                                end: "2026-11-11T08:00:00Z",
                                ghi_wm2: 500,
                                source_snapshot_ids: [8],
                            },
                        ],
                    },
                },
            }),
        );
        await page.goto("/");
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .click();
        await page
            .getByRole("button", { name: "Irradiance", exact: true })
            .click();
        await expect(
            page.getByText("Includes intervals from earlier forecast updates."),
        ).toBeVisible();
        await page.getByText("View interval data (2 intervals)").click();
        await expect(page.getByRole("cell", { name: "07:00" })).toBeVisible();
        await expect(page.getByRole("cell", { name: "10:30" })).toBeVisible();
        await page.getByText("Source & quality", { exact: true }).click();
        await expect(
            page.getByText(
                "Forecast update 6: collected 11/11/2026, 5:00:00 AM",
            ),
        ).toBeVisible();
        await expect(
            page.getByText(
                "Forecast update 8: collected 11/11/2026, 8:00:00 AM",
            ),
        ).toBeVisible();
        const downloadPromise = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .click();
        const download = await downloadPromise;
        const csv = await readFile(await download.path(), "utf8");
        expect(csv).toContain("source_snapshot_ids");
        expect(csv).toContain("snapshot_id");
        expect(csv).toContain("[6]");
        expect(csv).toContain("[8]");
    });
}

test("outage comparison CSV retains normal forecast source IDs", async ({
    page,
}) => {
    await mockApi(page);
    const interval = {
        start: "2026-11-11T04:00:00Z",
        end: "2026-11-11T04:30:00Z",
        mean_power_mw: 1,
        energy_mwh: 0.5,
        source_snapshot_ids: [6],
    };
    await page.route("**/plants/21/forecast/**", (route) =>
        route.fulfill({
            json: {
                date: "2026-11-11",
                timezone: region.timezone,
                source_kind: "forecast",
                freshness: "fresh",
                result: { complete: true, intervals: [interval] },
                outage_scenario: {
                    status: "estimated",
                    result: { complete: true, intervals: [interval] },
                },
            },
        }),
    );
    await page.goto("/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    const downloadPromise = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download both scenarios CSV" })
        .click();
    const download = await downloadPromise;
    const csv = await readFile(await download.path(), "utf8");
    expect(csv).toContain("source_snapshot_ids");
    expect(csv).toContain("[6]");
});
