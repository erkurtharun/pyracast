import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mockApi, paged, region } from "./support/workspace";

async function setup(page: Page) {
    await page.clock.setFixedTime(new Date("2026-01-08T12:00:00Z"));
    await mockApi(page);
    await page.route("**/collection-schedule/", (route) =>
        route.fulfill({
            json: {
                enabled: true,
                times: ["09:00", "10:00", "11:00"],
                timezone: region.timezone,
                effective_from: "2026-01-01T00:00:00Z",
                daily_request_limit: 10,
                used_today: 2,
                remaining_today: 8,
                max_forecast_times: 9,
                history_local_time: "03:00",
                history_retry_until_local: "09:00",
                history_retry_minutes: 30,
                quota_utc_day: "2026-01-08",
                requests_by_outcome: { succeeded: 2, failed: 0, uncertain: 0 },
                recent_jobs: [],
            },
        }),
    );
    await page.route("**/production-source/", (route) =>
        route.fulfill({
            json: { connected: false, credentials_available: true },
        }),
    );
    await page.route("**/reported-production/**", (route) =>
        route.fulfill({
            json: {
                timezone: region.timezone,
                date: "2026-01-07",
                source_kind: "mixed",
                complete: false,
                calibration_eligible: true,
                calibration_blocker: null,
                snapshot: {
                    id: 1,
                    collected_at: "2026-01-08T03:00:00Z",
                    available_at: "2026-01-08T03:00:00Z",
                    content_sha256: "synthetic",
                    normalization_policy: "hour_start",
                    complete: false,
                },
                readings: [1, null, 2].map((energy, index) => ({
                    period_start: `2026-01-07T${String(index + 8).padStart(2, "0")}:00:00+03:00`,
                    period_end: `2026-01-07T${String(index + 9).padStart(2, "0")}:00:00+03:00`,
                    provider_timestamp:
                        energy === null ? null : `hour-${index}`,
                    energy_mwh: energy,
                    source_kind:
                        energy === null
                            ? null
                            : index === 0
                              ? "settlement_injection"
                              : "realtime_generation",
                    snapshot_id: energy === null ? null : index === 0 ? 2 : 1,
                })),
            },
        }),
    );
    const calibration = {
        id: 40,
        window_start: "2026-01-01",
        window_end: "2026-01-07",
        status: "ready",
        assessment: "validated",
        factor: 1.1,
        result: { provisional_production: true },
    };
    await page.route("**/calibrations/", (route) =>
        route.fulfill({ json: paged([calibration]) }),
    );
    await page.route("**/calibrations/40/", (route) =>
        route.fulfill({ json: calibration }),
    );
    await page.route("**/outages/", (route) =>
        route.fulfill({ json: paged([]) }),
    );
    await page.route("**/trash/**", (route) => {
        const isPlant = new URL(route.request().url()).pathname.endsWith(
            "/plants/",
        );
        return route.fulfill({
            json: paged([
                {
                    id: 91,
                    name: isPlant ? "Archived plant" : "Archived region",
                    deleted_at: "2026-01-07T10:00:00Z",
                    purge_after: "2026-02-06T10:00:00Z",
                    can_restore: true,
                    ...(isPlant
                        ? {
                              region_id: region.id,
                              region_name: region.name,
                              parent_deleted: false,
                          }
                        : {}),
                },
            ]),
        });
    });
    await page.route("**/forecast/**", (route) => {
        const target =
            new URL(route.request().url()).searchParams.get("date") ??
            "2026-01-08";
        return route.fulfill({
            json: {
                date: target,
                timezone: region.timezone,
                source_kind: "forecast",
                freshness: "fresh",
                collected_at: "2026-01-08T10:00:00Z",
                configuration_id: 31,
                result: {
                    complete: true,
                    energy_kwh_m2: 3,
                    energy_mwh: 10,
                    coverage_fraction: 1,
                    partial_energy_kwh_m2: 3,
                    partial_energy_mwh: 10,
                    quality_flags: [
                        "unvalidated_reference_transfer",
                        "provisional_production_calibration",
                    ],
                    factor: 1,
                    model_version: "reference-power-transfer-v1",
                    configuration: { revision: 1 },
                    provenance: {
                        snapshot_id: 1,
                        collected_at: "2026-01-08T10:00:00Z",
                    },
                    intervals: [
                        {
                            start: `${target}T06:00:00Z`,
                            end: `${target}T06:30:00Z`,
                            ghi_wm2: 200,
                            mean_power_mw: 1,
                            energy_mwh: 0.5,
                        },
                        {
                            start: `${target}T06:30:00Z`,
                            end: `${target}T07:00:00Z`,
                            ghi_wm2: 400,
                            mean_power_mw: 2,
                            energy_mwh: 1,
                        },
                    ],
                },
            },
        });
    });
    await page.route("**/production/?**", (route) => {
        const date =
            new URL(route.request().url()).searchParams.get("start_date") ??
            "2026-01-07";
        return route.fulfill({
            json: {
                timezone: region.timezone,
                start_date: date,
                end_date: date,
                available_start_date: "2026-01-01",
                available_end_date: "2026-01-07",
                days: [
                    {
                        date,
                        available_count: 1,
                        recorded_count: 0,
                        missing_count: 1,
                        sunrise: `${date}T05:00:00Z`,
                        sunset: `${date}T14:00:00Z`,
                        daylight_status: "normal",
                        daylight_windows: [
                            {
                                starts_at: `${date}T05:00:00Z`,
                                ends_at: `${date}T14:00:00Z`,
                            },
                        ],
                    },
                ],
                intervals: [
                    {
                        starts_at: `${date}T06:00:00Z`,
                        ends_at: `${date}T07:00:00Z`,
                        can_enter: true,
                        reference_available: true,
                        observation: null,
                    },
                ],
                observations: [],
            },
        });
    });
}

async function fits(page: Page, name: string, audit: boolean) {
    await page.locator("details").evaluateAll((rows) =>
        rows.forEach((row) => {
            if (row instanceof HTMLDetailsElement) row.open = true;
        }),
    );
    await expect
        .poll(
            () =>
                page.evaluate(
                    () => document.documentElement.scrollWidth - innerWidth,
                ),
            { message: `${name}: document overflow` },
        )
        .toBeLessThanOrEqual(1);
    const outside = await page
        .locator(
            "main button:visible, main input:visible, main select:visible, main textarea:visible",
        )
        .evaluateAll((rows) =>
            rows
                .filter((row) => !row.closest(".table-scroll"))
                .filter((row) => {
                    const rect = row.getBoundingClientRect();
                    return rect.left < -1 || rect.right > innerWidth + 1;
                })
                .map(
                    (row) =>
                        row.getAttribute("aria-label") ??
                        row.textContent?.trim(),
                ),
        );
    expect(outside, `${name}: controls outside viewport`).toEqual([]);
    if (audit) {
        const result = await new AxeBuilder({ page }).analyze();
        expect(result.violations, `${name}: accessibility`).toEqual([]);
    }
}

for (const width of [320, 390, 768, 1440]) {
    test(`all workspace pages fit at ${width}px`, async ({ page }) => {
        test.setTimeout(90_000);
        await page.setViewportSize({ width, height: 900 });
        await setup(page);
        const audit = width === 390;
        await page.goto("/");
        await page
            .getByRole("button", { name: /Antalya solar park/ })
            .waitFor();
        await fits(page, "Overview", audit);
        await page.getByRole("button", { name: "+ Request a region" }).click();
        await fits(page, "New region", audit);
        await page.getByRole("button", { name: /Antalya solar park/ }).click();
        await page
            .getByRole("img", { name: /Irradiance by interval/ })
            .waitFor();
        await fits(page, "Regional forecast", audit);
        for (const tab of ["Data collection", "Email reminders"]) {
            await page.getByRole("button", { name: tab, exact: true }).click();
            await page
                .getByRole("heading", { name: tab, exact: true })
                .waitFor();
            await fits(page, tab, audit);
        }
        await page
            .getByRole("button", { name: "Solar plants", exact: true })
            .click();
        await page.getByRole("button", { name: /East array/ }).waitFor();
        await fits(page, "Plants", audit);
        await page.getByRole("button", { name: /East array/ }).click();
        await page.getByRole("img", { name: /Power by interval/ }).waitFor();
        await expect(
            page.getByText("Provisional production calibration", {
                exact: true,
            }),
        ).toBeVisible();
        await fits(page, "Plant forecast", audit);
        await page.getByRole("button", { name: "Edit configuration" }).click();
        await fits(page, "Plant configuration", audit);
        await page.getByRole("button", { name: "Close configuration" }).click();
        for (const tab of [
            "Calibration",
            "Automatic production",
            "Outages",
            "Production import",
        ]) {
            await page.getByRole("button", { name: tab, exact: true }).click();
            if (tab === "Calibration") {
                await page.getByLabel("Day", { exact: true }).waitFor();
                await expect(
                    page.getByText("Provisional production", { exact: true }),
                ).toHaveCount(1);
                await page
                    .getByRole("button", { name: "View details" })
                    .click();
                await expect(
                    page.getByText("Provisional production", { exact: true }),
                ).toHaveCount(2);
                await expect(
                    page.getByText("Assessment: validated. Factor: 1.1."),
                ).toBeVisible();
            }
            if (tab === "Automatic production") {
                const table = page.getByRole("table", {
                    name: "EPİAŞ reported hourly production",
                });
                await expect(
                    table.getByText("Missing", { exact: true }),
                ).toBeVisible();
                await expect(
                    table.getByText("UEVM", { exact: true }),
                ).toBeVisible();
                await expect(
                    table.getByText("Provisional", { exact: true }),
                ).toBeVisible();
                const overflow = await table.evaluate(
                    (node) =>
                        node.getBoundingClientRect().width -
                        node.parentElement!.clientWidth,
                );
                expect(overflow).toBeLessThanOrEqual(1);
            }
            if (tab === "Outages")
                await page
                    .getByRole("button", { name: "Report outage" })
                    .click();
            await fits(page, tab, audit);
        }
        await page.getByRole("button", { name: /^Back to region:/ }).click();
        await expect(
            page.getByRole("button", {
                name: "Regional forecast",
                exact: true,
            }),
        ).toBeVisible();
        for (const tab of ["Account settings", "Recycle bin"]) {
            await page.getByRole("button", { name: tab, exact: true }).click();
            await page
                .getByRole("heading", { name: tab, exact: true })
                .waitFor();
            await fits(page, tab, audit);
        }
        for (const name of ["Archived region", "Archived plant"]) {
            const restore = page.getByRole("button", {
                name: `Restore ${name}`,
                exact: true,
            });
            await expect(restore).toBeEnabled();
            const box = await restore.boundingBox();
            expect(box).not.toBeNull();
            expect(box!.x).toBeGreaterThanOrEqual(0);
            expect(box!.x + box!.width).toBeLessThanOrEqual(width);
        }
        await expect(
            page.getByRole("button", { name: "Sign out" }),
        ).toBeVisible();
    });
}

test("mobile sign in keeps visible controls and keyboard focus", async ({
    page,
}) => {
    await page.setViewportSize({ width: 320, height: 640 });
    await mockApi(page, { anonymous: true });
    await page.goto("/");
    await page.getByLabel("Username", { exact: true }).waitFor();
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("Username", { exact: true })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("Password", { exact: true })).toBeFocused();
    await fits(page, "Sign in", true);
});
