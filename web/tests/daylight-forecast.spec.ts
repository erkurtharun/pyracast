import { expect, test } from "./support/test";
import type { Page } from "@playwright/test";
import { mockApi, region } from "./support/workspace";

test("irradiance input coverage stays complete when a derived value is unavailable", async ({
    page,
}) => {
    const result = forecastResult(false, false);
    Object.assign(result.daylight, {
        input_coverage_seconds: 34200,
        input_coverage_fraction: 1,
        valid_coverage_seconds: 33640,
        coverage_fraction: 0.9836,
        complete: false,
        energy_kwh_m2: null,
        partial_energy_kwh_m2: 0.95,
    });
    await openForecast(page, false, result);
    await expect(
        page.locator(".stat").filter({ hasText: /^Coverage/ }),
    ).toContainText("100 %");
    await expect(
        page.locator(".stat").filter({ hasText: /^Partial estimate/ }),
    ).toContainText("0.95 kWh/m²");
    await expect(
        page.getByText("Some irradiance values could not be calculated.", {
            exact: true,
        }),
    ).toBeVisible();
    await expect(
        page.locator(".stat").filter({ hasText: /^Estimated energy/ }),
    ).toHaveCount(0);
    await page.getByText("Source & quality", { exact: true }).click();
    await expect(
        page.getByText("Source data coverage: 100%.", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("Usable irradiance coverage: 98.36%.", { exact: true }),
    ).toBeVisible();
});

function forecastResult(electrical: boolean, complete = true) {
    const energy = electrical ? "energy_mwh" : "energy_kwh_m2";
    const partial = electrical ? "partial_energy_mwh" : "partial_energy_kwh_m2";
    return {
        start: "2026-11-13T21:00:00Z",
        end: "2026-11-14T21:00:00Z",
        complete: false,
        coverage_fraction: electrical
            ? (complete ? 20.5 : 20) / 24
            : (complete ? 10 : 9.5) / 24,
        coverage_seconds: 73800,
        quality_flags: electrical ? [] : ["ambiguous_zero_power"],
        [energy]: null,
        [partial]: complete ? (electrical ? 30 : 1) : electrical ? 28.5 : 0.95,
        daylight: {
            policy: "astral-3.2-sea-level-v1",
            energy_scope: "whole_native_intervals",
            status: "normal",
            sunrise: "2026-11-14T04:15:00Z",
            sunset: "2026-11-14T13:45:00Z",
            windows: [
                { start: "2026-11-14T04:15:00Z", end: "2026-11-14T13:45:00Z" },
            ],
            duration_seconds: 34200,
            coverage_seconds: 34200,
            valid_coverage_seconds: complete ? 34200 : 32400,
            coverage_fraction: complete ? 1 : 18 / 19,
            input_coverage_fraction: complete ? 1 : 18 / 19,
            complete,
            [energy]: complete ? (electrical ? 30 : 1) : null,
            [partial]: complete
                ? electrical
                    ? 30
                    : 1
                : electrical
                  ? 28.5
                  : 0.95,
        },
        intervals: Array.from({ length: 48 }, (_, index) => {
            const start =
                Date.parse("2026-11-13T21:00:00Z") + index * 1_800_000;
            const missing = index < 7 || (!complete && index === 15);
            const generating = index >= 14 && index < 34;
            return {
                start: new Date(start).toISOString(),
                end: new Date(start + 1_800_000).toISOString(),
                mean_power_mw: missing ? null : generating ? 3 : 0,
                energy_mwh: missing ? null : generating ? 1.5 : 0,
                ghi_wm2: missing || !generating ? null : 100,
            };
        }).filter((_, index) => electrical || index >= 7),
    };
}

test("legacy irradiance payloads keep their reported coverage when input coverage is absent", async ({
    page,
}) => {
    const result = forecastResult(false, false);
    Object.assign(result.daylight, { input_coverage_fraction: undefined });
    await openForecast(page, false, result);
    await expect(
        page.locator(".stat").filter({ hasText: /^Coverage/ }),
    ).toContainText("94.737 %");
    await expect(
        page.locator(".stat").filter({ hasText: /^Partial estimate/ }),
    ).toContainText("0.95 kWh/m²");
});

test("complete irradiance input without any usable irradiance does not present unknown energy as zero", async ({
    page,
}) => {
    const result = forecastResult(false, false);
    Object.assign(result, { coverage_fraction: 0, partial_energy_kwh_m2: 0 });
    Object.assign(result.daylight, {
        input_coverage_fraction: 1,
        coverage_fraction: 0,
        valid_coverage_seconds: 0,
        partial_energy_kwh_m2: 0,
    });
    result.intervals = result.intervals.map((row) => ({
        ...row,
        ghi_wm2: null,
    }));
    await openForecast(page, false, result);
    await expect(
        page.locator(".stat").filter({ hasText: /^Coverage/ }),
    ).toContainText("100 %");
    await expect(
        page.locator(".stat").filter({ hasText: /^Partial estimate/ }),
    ).toContainText("— kWh/m²");
    await expect(
        page.getByText("Some irradiance values could not be calculated.", {
            exact: true,
        }),
    ).toBeVisible();
    await page.getByText("Source & quality", { exact: true }).click();
    await expect(
        page.getByText("Full-day partial estimate: — kWh/m².", { exact: true }),
    ).toBeVisible();
});

test("an outage with no usable intervals does not present unknown partial energy as zero", async ({
    page,
}) => {
    const affected = forecastResult(true, false);
    Object.assign(affected.daylight, {
        coverage_fraction: 0,
        valid_coverage_seconds: 0,
        partial_energy_mwh: 0,
    });
    affected.intervals = affected.intervals.map((row) => ({
        ...row,
        mean_power_mw: null,
        energy_mwh: null,
    }));
    await openForecast(page, true, forecastResult(true), {
        outage_scenario: { status: "unknown_impact", result: affected },
    });
    await expect(
        page.locator(".stat").filter({ hasText: /^Normal capacity/ }),
    ).toContainText("30 MWh");
    await expect(
        page.locator(".stat").filter({ hasText: /^With outages · partial/ }),
    ).toContainText("— MWh");
});

for (const electrical of [true, false]) {
    test(`${electrical ? "generation" : "irradiance"} preserves a complete zero energy estimate`, async ({
        page,
    }) => {
        const result = forecastResult(electrical);
        Object.assign(result, {
            complete: true,
            coverage_fraction: 1,
            energy_mwh: 0,
            energy_kwh_m2: 0,
        });
        Object.assign(result.daylight, { energy_mwh: 0, energy_kwh_m2: 0 });
        await openForecast(page, electrical, result);
        await expect(
            page.locator(".stat").filter({ hasText: /^Estimated energy/ }),
        ).toContainText(electrical ? "0 MWh" : "0 kWh/m²");
        await expect(
            page.locator(".stat").filter({ hasText: /^Partial estimate/ }),
        ).toHaveCount(0);
        await page.getByText("Source & quality", { exact: true }).click();
        await expect(
            page.getByText(
                `Full-day energy: 0 ${electrical ? "MWh" : "kWh/m²"}.`,
                { exact: true },
            ),
        ).toBeVisible();
    });
}

async function openForecast(
    page: Page,
    electrical: boolean,
    result: Record<string, unknown>,
    scenario: Record<string, unknown> = {},
) {
    await mockApi(page);
    await page.route(
        electrical ? "**/plants/21/forecast/**" : "**/regions/11/forecast/**",
        (route) =>
            route.fulfill({
                json: {
                    date: "2026-11-14",
                    timezone: region.timezone,
                    source_kind: "forecast",
                    freshness: "fresh",
                    result,
                    ...scenario,
                },
            }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    if (!electrical)
        await page
            .getByRole("button", { name: "Irradiance", exact: true })
            .click();
}

for (const electrical of [true, false]) {
    test(`${electrical ? "generation" : "irradiance"} shows complete daylight energy despite missing night`, async ({
        page,
    }) => {
        if (electrical) await page.setViewportSize({ width: 390, height: 844 });
        await openForecast(page, electrical, forecastResult(electrical));
        await expect(
            page.locator(".stat").filter({ hasText: /^Estimated energy/ }),
        ).toContainText(electrical ? "30 MWh" : "1 kWh/m²");
        await expect(
            page.locator(".stat").filter({ hasText: /^Coverage/ }),
        ).toContainText("100 %");
        await expect(
            page.getByText("Daylight coverage complete.", { exact: false }),
        ).toHaveCount(0);
        await expect(page.getByRole("status")).toHaveCount(0);
        await page.getByText("Source & quality", { exact: true }).click();
        await expect(
            page.getByText(
                `Full-day partial estimate: ${electrical ? "30 MWh" : "1 kWh/m²"}.`,
                { exact: true },
            ),
        ).toBeVisible();
        await expect(
            page.getByText(
                `Full-day coverage: ${electrical ? "85.417" : "41.667"}%.`,
                { exact: false },
            ),
        ).toBeVisible();
        await expect(
            page.getByText(
                "Energy includes whole source intervals overlapping daylight.",
                { exact: false },
            ),
        ).toBeVisible();
        if (electrical)
            expect(
                await page.evaluate(() => document.documentElement.scrollWidth),
            ).toBeLessThanOrEqual(390);
    });

    test(`${electrical ? "generation" : "irradiance"} preserves missing daylight instead of showing a complete total`, async ({
        page,
    }) => {
        await openForecast(page, electrical, forecastResult(electrical, false));
        await expect(page.getByRole("status")).toContainText(
            "Some forecast data is missing.",
        );
        await expect(
            page.locator(".stat").filter({ hasText: /^Estimated energy/ }),
        ).toHaveCount(0);
        await expect(
            page.locator(".stat").filter({ hasText: /^Partial estimate/ }),
        ).toContainText(electrical ? "28.5 MWh" : "0.95 kWh/m²");
    });
}

test("a missing boundary interval preserves a partial energy estimate despite full daylight coverage", async ({
    page,
}) => {
    const result = forecastResult(true, false);
    Object.assign(result.daylight, {
        coverage_fraction: 1,
        valid_coverage_seconds: 34200,
    });
    await openForecast(page, true, result);
    await expect(
        page.locator(".stat").filter({ hasText: /^Coverage/ }),
    ).toContainText("100 %");
    await expect(
        page.locator(".stat").filter({ hasText: /^Estimated energy/ }),
    ).toHaveCount(0);
    await expect(
        page.locator(".stat").filter({ hasText: /^Partial estimate/ }),
    ).toContainText("28.5 MWh");
    await expect(page.getByRole("status")).toContainText(
        "Some interval values are unavailable.",
    );
});

test("polar night shows no daylight without a complete zero total", async ({
    page,
}) => {
    const result = forecastResult(true);
    Object.assign(result.daylight, {
        status: "polar_night",
        duration_seconds: 0,
        coverage_seconds: 0,
        valid_coverage_seconds: 0,
        coverage_fraction: null,
        complete: false,
        energy_mwh: null,
        partial_energy_mwh: 0,
    });
    await openForecast(page, true, result);
    await expect(
        page.getByText("No daylight on this date.", { exact: true }),
    ).toBeVisible();
    await expect(
        page.locator(".stat").filter({ hasText: /^Estimated energy/ }),
    ).toContainText("—");
    await expect(
        page.locator(".stat").filter({ hasText: /^Coverage/ }),
    ).toContainText("—");
    await expect(
        page.getByText("Daylight partial estimate", { exact: true }),
    ).toHaveCount(0);
    await expect(page.getByRole("status")).toHaveCount(0);
});

for (const name of ["outage_scenario"]) {
    test(`${name} uses its daylight energy alongside normal capacity`, async ({
        page,
    }) => {
        const affected = forecastResult(true);
        Object.assign(affected.daylight, { energy_mwh: 15 });
        const scenario = {
            [name]: {
                status: "estimated",
                performance_ratio: 0.8,
                window_start: "2026-11-08",
                window_end: "2026-11-13",
                result: affected,
            },
        };
        await openForecast(page, true, forecastResult(true), scenario);
        await expect(
            page.locator(".stat").filter({ hasText: /^Normal capacity/ }),
        ).toContainText("30 MWh");
        await expect(
            page.locator(".stat").filter({
                hasText: /^With outages/,
            }),
        ).toContainText("15 MWh");
        await expect(
            page.getByText("Partial coverage", { exact: false }),
        ).toHaveCount(0);
    });
}

test("an incomplete outage scenario keeps its partial energy beside the complete normal estimate", async ({
    page,
}) => {
    const affected = forecastResult(true, false);
    Object.assign(affected.daylight, { partial_energy_mwh: 12 });
    await openForecast(page, true, forecastResult(true), {
        outage_scenario: { status: "unknown_impact", result: affected },
    });
    await expect(
        page.locator(".stat").filter({ hasText: /^Normal capacity/ }),
    ).toHaveCount(1);
    await expect(
        page.locator(".stat").filter({ hasText: /^Normal capacity/ }),
    ).toContainText("30 MWh");
    await expect(
        page.locator(".stat").filter({ hasText: /^With outages · partial/ }),
    ).toHaveCount(1);
    await expect(
        page.locator(".stat").filter({ hasText: /^With outages · partial/ }),
    ).toContainText("12 MWh");
    await expect(
        page.getByText("Outage impact is unknown for some intervals.", {
            exact: true,
        }),
    ).toBeVisible();
});
