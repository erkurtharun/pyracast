import { expect, test } from "./support/test";
import { readFile } from "node:fs/promises";
import { mockApi, region } from "./support/workspace";

for (const electrical of [true, false]) {
    test(`partial ${electrical ? "generation" : "irradiance"} explains hidden missing hours`, async ({
        page,
    }) => {
        await mockApi(page);
        await page.route(
            electrical
                ? "**/plants/21/forecast/**"
                : "**/regions/11/forecast/**",
            (route) =>
                route.fulfill({
                    json: {
                        date: "2026-11-14",
                        timezone: region.timezone,
                        source_kind: "forecast",
                        freshness: "fresh",
                        result: {
                            start: "2026-11-13T21:00:00Z",
                            end: "2026-11-14T21:00:00Z",
                            complete: false,
                            coverage_fraction: 20.5 / 24,
                            energy_mwh: null,
                            partial_energy_mwh: 5,
                            energy_kwh_m2: null,
                            partial_energy_kwh_m2: 1,
                            intervals: [
                                {
                                    start: "2026-11-14T00:30:00Z",
                                    end: "2026-11-14T04:00:00Z",
                                    mean_power_mw: 0,
                                    energy_mwh: 0,
                                    ghi_wm2: 0,
                                },
                                {
                                    start: "2026-11-14T04:00:00Z",
                                    end: "2026-11-14T15:00:00Z",
                                    mean_power_mw: 1,
                                    energy_mwh: 11,
                                    ghi_wm2: 100,
                                },
                                {
                                    start: "2026-11-14T15:00:00Z",
                                    end: "2026-11-14T21:00:00Z",
                                    mean_power_mw: 0,
                                    energy_mwh: 0,
                                    ghi_wm2: 0,
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
        if (!electrical)
            await page
                .getByRole("button", { name: "Irradiance", exact: true })
                .click();
        await expect(page.getByRole("status")).toContainText(
            "Some forecast data is missing.",
        );
        await page
            .getByRole("checkbox", {
                name: "Show full day",
            })
            .check();
        await page.getByText("View interval data (4 intervals)").click();
        const missing = page
            .getByRole("row")
            .filter({ hasText: "00:00–03:30" });
        await expect(
            missing.getByRole("cell", { name: "00:00–03:30", exact: true }),
        ).toBeVisible();
        await expect(missing.locator(".interval-value")).toHaveText(["—"]);
        await page.locator(".interval-notes summary").click();
        await expect(page.locator(".interval-notes")).toContainText(
            "missing source interval",
        );
        const downloadPromise = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .click();
        const csv = await readFile(
            await (await downloadPromise).path(),
            "utf8",
        );
        expect(csv).not.toContain("missing_source_interval");
        expect(csv).toContain("2026-11-14T00:30:00Z");
    });
}

test("irradiance distinguishes available source hours from ambiguous zero power", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/regions/11/forecast/**", (route) =>
        route.fulfill({
            json: {
                date: "2026-11-14",
                timezone: region.timezone,
                source_kind: "forecast",
                freshness: "fresh",
                result: {
                    start: "2026-11-13T21:00:00Z",
                    end: "2026-11-14T21:00:00Z",
                    complete: false,
                    coverage_seconds: 73800,
                    coverage_fraction: 10 / 24,
                    quality_flags: ["ambiguous_zero_power"],
                    intervals: [
                        {
                            start: "2026-11-14T00:30:00Z",
                            end: "2026-11-14T04:00:00Z",
                            ghi_wm2: null,
                            quality_flags: ["ambiguous_zero_power"],
                        },
                        {
                            start: "2026-11-14T04:00:00Z",
                            end: "2026-11-14T14:00:00Z",
                            ghi_wm2: 100,
                        },
                        {
                            start: "2026-11-14T14:00:00Z",
                            end: "2026-11-14T21:00:00Z",
                            ghi_wm2: null,
                            quality_flags: ["ambiguous_zero_power"],
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
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    await expect(page.getByRole("status")).toContainText(
        "Some forecast data is missing.",
    );
    await page
        .getByRole("checkbox", {
            name: "Show full day",
        })
        .check();
    await page.getByText("View interval data (4 intervals)").click();
    await page.locator(".interval-notes summary").click();
    const ambiguous = page
        .locator(".interval-notes li")
        .filter({ hasText: "ambiguous zero power" });
    await expect(ambiguous).toHaveCount(2);
    await expect(
        page
            .getByRole("table")
            .locator(".interval-value")
            .filter({ hasText: "—" }),
    ).toHaveCount(3);
});

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
            page.getByText("Mixed forecast updates", { exact: true }),
        ).toBeVisible();
        await page.getByText("View interval data (2 intervals)").click();
        await expect(page.getByRole("cell", { name: /^07:00–/ })).toBeVisible();
        await expect(page.getByRole("cell", { name: /^10:30–/ })).toBeVisible();
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

test("suspected derating shows a second scenario with its evidence", async ({
    page,
}) => {
    await mockApi(page);
    const interval = {
        start: "2026-11-11T04:00:00Z",
        end: "2026-11-11T04:30:00Z",
        mean_power_mw: 10,
        energy_mwh: 5,
        source_snapshot_ids: [6],
    };
    await page.route("**/plants/21/forecast/**", (route) =>
        route.fulfill({
            json: {
                date: "2026-11-11",
                timezone: region.timezone,
                source_kind: "forecast",
                freshness: "fresh",
                result: {
                    complete: true,
                    energy_mwh: 5,
                    intervals: [interval],
                },
                outage_scenario: null,
                derating_scenario: {
                    status: "suspected",
                    performance_ratio: 0.875,
                    window_start: "2026-11-04",
                    window_end: "2026-11-13",
                    days: [
                        { day: "2026-11-08", performance_index: 0.9, hours: 9 },
                        { day: "2026-11-09", performance_index: 0.8, hours: 9 },
                        {
                            day: "2026-11-11",
                            performance_index: 1.03,
                            hours: 8,
                        },
                        {
                            day: "2026-11-12",
                            performance_index: 0.875,
                            hours: 5,
                        },
                        {
                            day: "2026-11-13",
                            performance_index: 0.86,
                            hours: 3,
                        },
                    ],
                    result: {
                        complete: true,
                        energy_mwh: 4.375,
                        intervals: [
                            {
                                ...interval,
                                mean_power_mw: 8.75,
                                energy_mwh: 4.375,
                            },
                        ],
                    },
                },
            },
        }),
    );
    await page.goto("/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await expect(
        page.getByText(
            "Median shortfall: 12.5% over 5 comparable days. Outage unconfirmed.",
            { exact: false },
        ),
    ).toBeVisible();
    await expect(page.getByRole("status")).not.toContainText("2026-11-04");
    await expect(page.getByRole("status")).toContainText("Outage unconfirmed.");
    await page.getByText("Source & quality", { exact: true }).click();
    await expect(page.locator(".provenance")).toContainText(
        "5 qualifying days: 2026-11-08 to 2026-11-13. Median daily production/reference ratio over matched hours.",
    );
    await expect(
        page
            .locator(".stat")
            .getByText("If derating persists", { exact: true }),
    ).toBeVisible();
    await page.getByText("View interval data (1 intervals)").click();
    await expect(
        page.getByRole("columnheader", { name: "If derating persists (MWh)" }),
    ).toBeVisible();
    await expect(
        page
            .getByRole("table")
            .locator(".interval-value")
            .filter({ hasText: "4.375" }),
    ).toBeVisible();
    const downloadPromise = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download both scenarios CSV" })
        .click();
    const download = await downloadPromise;
    const csv = await readFile(await download.path(), "utf8");
    expect(csv).toContain("derating_scenario.performance_ratio");
});
