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
        await page.goto("/app/");
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .click();
        if (!electrical)
            await page
                .getByRole("button", {
                    name: "Irradiance forecast",
                    exact: true,
                })
                .click();
        await expect(page.getByRole("status")).toHaveText(
            "Data is missing for some hours.",
        );
        await page
            .getByRole("checkbox", {
                name: "Show full day",
            })
            .check();
        await page.getByText("Show table", { exact: true }).click();
        const missing = page
            .getByRole("row")
            .filter({ hasText: "00:00–03:30" });
        await expect(
            missing.getByRole("cell", { name: "00:00–03:30", exact: true }),
        ).toBeVisible();
        await expect(missing.locator(".interval-value")).toHaveText(["—"]);
        await expect(page.locator(".interval-notes")).toHaveCount(0);
        const downloadPromise = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .click();
        const csv = await readFile(
            await (await downloadPromise).path(),
            "utf8",
        );
        expect(csv).not.toContain("missing_source_interval");
        expect(csv).toContain("2026-11-14T03:30:00+03:00");
    });
}

test("irradiance shows hours without a usable value as missing without interval notes", async ({
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
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await expect(page.getByRole("status")).toHaveText(
        "Data is missing for some hours.",
    );
    await page
        .getByRole("checkbox", {
            name: "Show full day",
        })
        .check();
    await page.getByText("Show table", { exact: true }).click();
    await expect(page.locator(".interval-notes")).toHaveCount(0);
    await expect(page.getByText(/ambiguous zero power/)).toHaveCount(0);
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
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(
        page.getByRole("heading", { name: "Generation forecast" }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await observed;
    try {
        await expect(
            page.getByRole("heading", {
                name: "Irradiance forecast",
                exact: true,
            }),
        ).toHaveCount(0);
        await expect(
            page.getByText("Loading irradiance forecast…", { exact: true }),
        ).toBeVisible();
    } finally {
        release();
    }
    await expect(
        page.getByRole("heading", {
            name: "Irradiance forecast",
            exact: true,
        }),
    ).toBeVisible();
    await expect(page.getByText("kWh/m²", { exact: true })).toBeVisible();
});

for (const width of [320, 390]) {
    test(`a day from several forecast updates shows one data line at ${width}px`, async ({
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
        await page.goto("/app/");
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .click();
        await page
            .getByRole("button", { name: "Irradiance forecast", exact: true })
            .click();
        await expect(
            page.getByText("Forecast data: Nov 11, 2026, 8:00 AM", {
                exact: true,
            }),
        ).toBeVisible();
        await expect(page.locator("section.panel .tag")).toHaveCount(0);
        await page.getByText("Show table", { exact: true }).click();
        await expect(page.getByRole("cell", { name: /^07:00–/ })).toBeVisible();
        await expect(page.getByRole("cell", { name: /^10:30–/ })).toBeVisible();
        await expect(page.getByText(/^Forecast update /)).toHaveCount(0);
        const downloadPromise = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .click();
        const download = await downloadPromise;
        const csv = await readFile(await download.path(), "utf8");
        expect(csv).not.toContain("snapshot");
        expect(csv).not.toContain("provenance");
    });
}

test("outage comparison CSV writes both scenarios per interval without provenance", async ({
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
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    const downloadPromise = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download CSV", exact: true })
        .click();
    const download = await downloadPromise;
    const csv = await readFile(await download.path(), "utf8");
    const [header, row] = csv.replace(/^\uFEFF/, "").split("\r\n");
    expect(header).toBe(
        '"Plant","Data","Start","End","Without outage (MWh)","With outage (MWh)","Average power without outage (MW)","Average power with outage (MW)"',
    );
    expect(row).toContain(
        '"Forecast","2026-11-11T07:00:00+03:00","2026-11-11T07:30:00+03:00",0.5,0.5,1,1',
    );
});

for (const state of [
    "shortfall",
    "zero",
    "recovered",
    "stale",
    "insufficient",
] as const) {
    test(`production diagnostic ${state} keeps one forecast and its interval CSV`, async ({
        page,
    }) => {
        await page.setViewportSize({ width: 320, height: 844 });
        await mockApi(page);
        const latestRatio =
            state === "zero" ? 0 : state === "recovered" ? 1.03 : 0.86;
        await page.route("**/plants/21/forecast/**", (route) =>
            route.fulfill({
                json: {
                    date: "2026-11-14",
                    timezone: region.timezone,
                    source_kind: "forecast",
                    freshness: "fresh",
                    result: {
                        complete: true,
                        energy_mwh: 5,
                        intervals: [
                            {
                                start: "2026-11-14T09:00:00Z",
                                end: "2026-11-14T09:30:00Z",
                                mean_power_mw: 10,
                                energy_mwh: 5,
                                source_snapshot_ids: [6],
                            },
                        ],
                    },
                    outage_scenario: null,
                    production_diagnostic: {
                        status:
                            state === "insufficient"
                                ? "insufficient"
                                : "suspected",
                        performance_ratio: 0.875,
                        threshold: 0.92,
                        baseline_factor: 1,
                        reference_kind: "uncalibrated_reference",
                        window_start: "2026-11-04",
                        window_end: "2026-11-13",
                        latest_matched_day: "2026-11-13",
                        latest_day_status:
                            state === "recovered"
                                ? "within_threshold"
                                : state === "insufficient"
                                  ? "insufficient"
                                  : "shortfall",
                        latest_performance_index: latestRatio,
                        evaluated_at: "2026-11-14T01:00:00Z",
                        calibration_id: 4,
                        evidence_state: state === "stale" ? "stale" : "current",
                        days: [
                            {
                                day: "2026-11-08",
                                performance_index: 0.875,
                                hours: 5,
                                status: "shortfall",
                                observed_zero: false,
                            },
                            {
                                day: "2026-11-13",
                                performance_index: latestRatio,
                                hours: 5,
                                status:
                                    state === "recovered"
                                        ? "within_threshold"
                                        : "shortfall",
                                observed_zero: state === "zero",
                            },
                        ],
                    },
                },
            }),
        );
        await page.goto("/app/");
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .click();
        if (state === "stale" || state === "insufficient")
            await expect(page.getByRole("status")).toHaveCount(0);
        else if (state === "recovered")
            await expect(page.getByRole("status")).toHaveText(
                "Production was below expected on several recent days.",
            );
        else
            await expect(page.getByRole("status")).toHaveText(
                `Production was ${state === "zero" ? 100 : 14}% below expected during the hours compared on 2026-11-13.`,
            );
        await expect(
            page.getByText("If derating persists", { exact: true }),
        ).toHaveCount(0);
        await expect(
            page.getByRole("img", { name: "Average power (MW)", exact: true }),
        ).toBeVisible();
        await page.getByText("Show table", { exact: true }).click();
        await expect(page.getByRole("columnheader")).toHaveCount(2);
        await expect(
            page.getByRole("table").locator(".interval-value"),
        ).toHaveText(["5"]);
        await expect(page.locator(".provenance")).toHaveCount(0);
        expect(
            await page.evaluate(() => document.documentElement.scrollWidth),
        ).toBeLessThanOrEqual(320);
        const downloadPromise = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .click();
        const csv = await readFile(
            await (await downloadPromise).path(),
            "utf8",
        );
        expect(csv).toContain('"Average power (MW)"');
        expect(csv).not.toContain("production_diagnostic");
        expect(csv).not.toContain("With outage");
        expect(csv).not.toContain("derating_scenario");
    });
}
