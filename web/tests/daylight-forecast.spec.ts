import { expect, test } from "./support/test";
import type { Page } from "@playwright/test";
import { mockApi, paged, region, tenantRow } from "./support/workspace";

const stat = (page: Page, label: RegExp) =>
    page.locator(".stat").filter({ hasText: label });

test("irradiance with an unavailable derived value gives no daily total", async ({
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
    await expect(stat(page, /^Daily total/)).toContainText("— kWh/m²");
    await expect(
        page.getByText("Data is missing for some hours.", { exact: true }),
    ).toBeVisible();
    await expect(stat(page, /^Coverage/)).toHaveCount(0);
    await expect(page.locator(".stat")).toHaveCount(1);
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
    await expect(stat(page, /^Daily total/)).toContainText("— kWh/m²");
    await expect(
        page.getByText("Data is missing for some hours.", { exact: true }),
    ).toBeVisible();
});

test("an outage with no usable intervals does not present unknown energy as zero", async ({
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
    await expect(stat(page, /^Without outage/)).toContainText("30 MWh");
    await expect(stat(page, /^With outage/)).toContainText("— MWh");
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
        await expect(stat(page, /^Daily total/)).toContainText(
            electrical ? "0 MWh" : "0 kWh/m²",
        );
        await expect(page.getByRole("status")).toHaveCount(0);
    });
}

async function openForecast(
    page: Page,
    electrical: boolean,
    result: Record<string, unknown>,
    scenario: Record<string, unknown> = {},
    { member = false }: { member?: boolean } = {},
) {
    await mockApi(page);
    if (member)
        await page.route("**/api/tenants/", (route) =>
            route.fulfill({
                json: paged([
                    tenantRow({
                        id: 1,
                        name: "Sunfield Energy",
                        role: "viewer",
                    }),
                ]),
            }),
        );
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
        await expect(stat(page, /^Daily total/)).toContainText(
            electrical ? "30 MWh" : "1 kWh/m²",
        );
        await expect(page.getByRole("status")).toHaveCount(0);
        await expect(
            page.getByText(
                "Energy includes whole source intervals overlapping daylight.",
            ),
        ).toHaveCount(0);
        if (electrical)
            expect(
                await page.evaluate(() => document.documentElement.scrollWidth),
            ).toBeLessThanOrEqual(390);
    });

    test(`${electrical ? "generation" : "irradiance"} gives no total for missing daylight hours`, async ({
        page,
    }) => {
        await openForecast(page, electrical, forecastResult(electrical, false));
        await expect(page.getByRole("status")).toHaveText(
            "Data is missing for some hours.",
        );
        await expect(stat(page, /^Daily total/)).toContainText(
            electrical ? "— MWh" : "— kWh/m²",
        );
        await expect(stat(page, /^Partial estimate/)).toHaveCount(0);
    });
}

test("a missing boundary interval gives no total despite full daylight coverage", async ({
    page,
}) => {
    const result = forecastResult(true, false);
    Object.assign(result.daylight, {
        coverage_fraction: 1,
        valid_coverage_seconds: 34200,
    });
    await openForecast(page, true, result);
    await expect(stat(page, /^Daily total/)).toContainText("— MWh");
    await expect(page.getByRole("status")).toHaveText(
        "Data is missing for some hours.",
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
    await expect(stat(page, /^Daily total/)).toContainText("—");
    await expect(page.getByRole("status")).toHaveCount(0);
});

for (const name of ["outage_scenario"]) {
    test(`${name} uses its daylight energy beside the figure without outage`, async ({
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
        await expect(stat(page, /^Without outage/)).toContainText("30 MWh");
        await expect(stat(page, /^With outage/)).toContainText("15 MWh");
    });
}

for (const member of [false, true]) {
    test(`an outage of unknown impact gives no total beside the complete figure without outage and ${member ? "tells a member nothing to enter" : "tells the administrator what to enter"}`, async ({
        page,
    }) => {
        const affected = forecastResult(true, false);
        Object.assign(affected.daylight, { partial_energy_mwh: 12 });
        await openForecast(
            page,
            true,
            forecastResult(true),
            { outage_scenario: { status: "unknown_impact", result: affected } },
            { member },
        );
        await expect(stat(page, /^Without outage/)).toContainText("30 MWh");
        await expect(stat(page, /^With outage/)).toContainText("— MWh");
        await expect(page.getByRole("status")).toHaveText(
            member
                ? "Outage impact is unknown."
                : "Outage impact is unknown. Enter the capacity still in service in the outage record.",
        );
    });
}
