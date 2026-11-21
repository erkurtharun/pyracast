import { expect, test, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import type { components } from "../src/lib/schema";
import { errorBody } from "./support/errors";
import { mockApi, paged, region, user, tenantRow } from "./support/workspace";

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
                last_success_at: "2026-01-08T03:00:00Z",
                next_collection_at: null,
                state: "collecting",
                inactive_reason: null,
                requests_today: 0,
                max_forecast_times: 9,
                history_local_time: "03:00",
                history_retry_until_local: "09:00",
                history_retry_minutes: 30,
                recent_jobs: [],
            },
        }),
    );
    await page.route("**/production-source/", (route) =>
        route.fulfill({
            json: {
                connected: false,
                credentials_available: true,
                archived_days: 1,
            },
        }),
    );
    const calibration = {
        id: 40,
        window_start: "2026-01-01",
        window_end: "2026-01-07",
        status: "ready",
        selection_state: "candidate",
        activation_blocker: null,
        assessment: "evaluated",
        factor: 1.1,
        result: { provisional_production: true },
    };
    await page.route("**/calibrations/", (route) =>
        route.fulfill({ json: paged([calibration]) }),
    );
    await page.route("**/calibrations/40/", (route) =>
        route.fulfill({ json: calibration }),
    );
    await page.route("**/api/**/outages/", (route) =>
        route.fulfill({ json: paged([]) }),
    );
    await page.route("**/trash/regions/", (route) =>
        route.fulfill({
            json: paged(
                ["Archived region", "Archived plant"].map((name, index) => ({
                    id: 91 + index,
                    name,
                    plant_name: name,
                    latitude: 36 + index,
                    longitude: 30,
                    deleted_at: "2026-01-07T10:00:00Z",
                    purge_after: "2026-02-06T10:00:00Z",
                    restore_blocker: null,
                })),
            ),
        }),
    );
    await page.route("**/api/**/forecast/**", (route) => {
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
    await page.route("**/api/**/production/?**", (route) => {
        const query = new URL(route.request().url()).searchParams;
        expect(query.get("native_intervals")).toBe("true");
        const date = query.get("start_date") ?? "2026-01-07";
        return route.fulfill({
            json: {
                plant_id: 21,
                configuration_id: 31,
                timezone: region.timezone,
                start_date: date,
                end_date: date,
                available_start_date: "2026-01-01",
                available_end_date: "2026-01-07",
                days: [
                    {
                        date,
                        available_count: 23,
                        recorded_count: 1,
                        missing_count: 1,
                        total_mwh: null,
                        complete: false,
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
                resolution_minutes: 60,
                native_intervals: true,
                slot_scope: "full_day",
                daylight_policy: "reference_irradiance",
                intervals: Array.from({ length: 24 }, (_, hour) => {
                    const midnight = Date.parse(`${date}T00:00:00+03:00`);
                    const starts_at = new Date(
                        midnight + hour * 3_600_000,
                    ).toISOString();
                    const ends_at = new Date(
                        midnight + (hour + 1) * 3_600_000,
                    ).toISOString();
                    const provider:
                        | components["schemas"]["ProductionProviderOutput"]
                        | null =
                        hour === 11
                            ? null
                            : {
                                  starts_at,
                                  ends_at,
                                  energy_mwh: hour < 7 || hour > 18 ? 0 : 1,
                                  snapshot_id: hour === 12 ? 2 : 1,
                                  collected_at: "2026-01-08T03:00:00Z",
                                  available_at: "2026-01-08T03:00:00Z",
                                  content_sha256: "synthetic",
                                  source_kind:
                                      hour === 12
                                          ? "realtime_generation"
                                          : "settlement_injection",
                                  provisional: hour === 12,
                              };
                    const observation:
                        components["schemas"]["ObservationOutput"] | null =
                        hour === 10
                            ? {
                                  id: 1,
                                  configuration_id: 31,
                                  starts_at,
                                  ends_at,
                                  energy_mwh: 1.2,
                                  quality: "valid",
                                  source: "meter",
                                  recorded_by_id: user.id,
                                  created_at: "2026-01-08T05:00:00Z",
                                  supersedes_id: null,
                                  is_reverted: false,
                              }
                            : null;
                    return {
                        starts_at,
                        ends_at,
                        can_edit: true,
                        can_propose: false,
                        can_propose_revert: false,
                        proposal_unavailable_reason: null,
                        can_revert: observation !== null,
                        observation,
                        previous_configuration_observations: [],
                        revision_id: observation?.id ?? null,
                        provider_originals: provider ? [provider] : [],
                        effective:
                            observation || provider
                                ? {
                                      energy_mwh:
                                          observation?.energy_mwh ??
                                          provider!.energy_mwh,
                                      quality: observation?.quality ?? "valid",
                                      source: observation?.source ?? "epias",
                                      source_kind: observation
                                          ? "manual"
                                          : provider!.source_kind,
                                      provisional: observation
                                          ? false
                                          : provider!.provisional,
                                      aggregated: false,
                                  }
                                : null,
                        edit_unavailable_reason: null,
                    };
                }),
            } satisfies components["schemas"]["ProductionHistoryOutput"],
        });
    });
    await page.route("**/production/preview/", (route) => {
        const observation = route.request().postDataJSON().observations[0];
        return route.fulfill({
            json: {
                changes: [
                    { ...observation, action: "set", expected_revision_id: 1 },
                ],
                preview: [
                    {
                        starts_at: observation.starts_at,
                        ends_at: observation.ends_at,
                        current_energy_mwh: 1.2,
                        new_energy_mwh: 2.4,
                        corrected: true,
                    },
                ],
                warnings: [],
            },
        });
    });
}

async function fits(page: Page, name: string, audit: boolean) {
    const openStates = await page.locator("details").evaluateAll((rows) =>
        rows.map((row) => {
            if (!(row instanceof HTMLDetailsElement)) return false;
            const wasOpen = row.open;
            row.open = true;
            return wasOpen;
        }),
    );
    try {
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
                    .filter((row) => !row.closest(".table-scroll, .tabs"))
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
            const result = await accessibilityViolations(page);
            expect(result, `${name}: accessibility`).toEqual([]);
        }
    } finally {
        await page.locator("details").evaluateAll((rows, states) => {
            rows.forEach((row, index) => {
                if (row instanceof HTMLDetailsElement)
                    row.open = states[index] ?? false;
            });
        }, openStates);
    }
}

for (const width of [320, 390, 768, 1440]) {
    test(`all workspace pages fit at ${width}px`, async ({
        page,
    }, testInfo) => {
        test.setTimeout(90_000);
        await page.setViewportSize({ width, height: 900 });
        await setup(page);
        const audit = width === 390;
        await page.goto("/app/");
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .waitFor();
        await fits(page, "Overview", audit);
        await page.screenshot({
            path: testInfo.outputPath("portfolio.png"),
            fullPage: true,
        });
        await page.getByRole("button", { name: "+ Add a plant" }).click();
        await fits(page, "New region", audit);
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .click();
        await page
            .getByRole("button", { name: "Irradiance", exact: true })
            .click();
        await page
            .getByRole("img", { name: /Irradiance by interval/ })
            .waitFor();
        await fits(page, "Regional forecast", audit);
        await page
            .getByRole("button", { name: "Settings", exact: true })
            .click();
        for (const tab of ["Data collection", "Email reminders"]) {
            await page.getByRole("button", { name: tab, exact: true }).click();
            await page
                .getByRole("heading", { name: tab, exact: true })
                .waitFor();
            await fits(page, tab, audit);
        }
        await page
            .getByRole("button", { name: "Forecast", exact: true })
            .click();
        await page
            .getByRole("button", { name: "Generation", exact: true })
            .click();
        await page.getByRole("img", { name: /Power by interval/ }).waitFor();
        await expect(
            page.getByText("Provisional production calibration", {
                exact: true,
            }),
        ).toBeVisible();
        await fits(page, "Plant forecast", audit);
        await page.screenshot({
            path: testInfo.outputPath("forecast.png"),
            fullPage: true,
        });
        const settingsTab = page.getByRole("button", {
            name: "Settings",
            exact: true,
        });
        await settingsTab.focus();
        await expect(settingsTab).toBeFocused();
        const settingsBounds = await settingsTab.boundingBox();
        expect(settingsBounds!.x).toBeGreaterThanOrEqual(0);
        expect(settingsBounds!.x + settingsBounds!.width).toBeLessThanOrEqual(
            width,
        );
        await page
            .getByRole("button", { name: "Settings", exact: true })
            .click();
        await fits(page, "Plant configuration", audit);
        await page.screenshot({
            path: testInfo.outputPath("plant-settings.png"),
            fullPage: true,
        });
        await page
            .getByRole("button", { name: "Forecast", exact: true })
            .click();
        for (const tab of ["Calibration", "Production", "Outages"]) {
            await page.getByRole("button", { name: tab, exact: true }).click();
            if (tab === "Calibration") {
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
                    page.getByText("Assessment: Evaluated. Factor: 1.1."),
                ).toBeVisible();
            }
            if (tab === "Production") {
                const table = page.getByRole("table", {
                    name: "Production intervals",
                });
                const fullDay = page.getByRole("checkbox", {
                    name: "Show full day",
                });
                await expect(fullDay).not.toBeChecked();
                await expect(table.locator("tbody tr")).toHaveCount(12);
                await fullDay.check();
                await expect(table.locator("tbody tr")).toHaveCount(24);
                if (width === 320) {
                    const dayBounds = await page
                        .getByLabel("Day", { exact: true })
                        .boundingBox();
                    expect(dayBounds!.width).toBeGreaterThanOrEqual(160);
                    for (const name of [
                        "Previous day",
                        "Next day",
                        "Refresh",
                    ]) {
                        const bounds = await page
                            .getByRole("button", { name, exact: true })
                            .boundingBox();
                        expect(bounds!.width).toBeGreaterThanOrEqual(44);
                        expect(bounds!.height).toBeGreaterThanOrEqual(44);
                    }
                }
                await expect(
                    table.locator(".production-value").first(),
                ).toHaveText(/^0/);
                await expect(
                    page.getByText("1 missing interval", { exact: true }),
                ).toBeVisible();
                await expect(
                    table.getByText("Provisional", { exact: true }),
                ).toBeVisible();
                await expect(
                    table.getByText("Corrected", { exact: true }),
                ).toBeVisible();
                await expect(
                    table.getByText("Provider: 1 MWh", { exact: true }),
                ).toBeVisible();
                const overflow = await table.evaluate(
                    (node) =>
                        node.getBoundingClientRect().width -
                        node.parentElement!.clientWidth,
                );
                expect(overflow).toBeLessThanOrEqual(1);
                await page.screenshot({
                    path: testInfo.outputPath("production.png"),
                    fullPage: false,
                });
                await table
                    .getByRole("button", { name: /^Edit / })
                    .first()
                    .click();
                const editor = page.locator(".production-row-editor");
                await editor.getByRole("textbox").fill("0.5");
                await fits(page, "Production editor", audit);
                await page.screenshot({
                    path: testInfo.outputPath("production-editor.png"),
                    fullPage: false,
                });
                await editor
                    .getByRole("button", { name: "Cancel", exact: true })
                    .click();
                await page
                    .getByRole("button", {
                        name: "Import CSV",
                        exact: true,
                    })
                    .click();
                await expect(
                    page.getByLabel("Production CSV", { exact: true }),
                ).toBeVisible();
                await page
                    .getByLabel("Production CSV", { exact: true })
                    .setInputFiles({
                        name: "production.csv",
                        mimeType: "text/csv",
                        buffer: Buffer.from(
                            "starts_at,ends_at,energy_kwh,quality,source\n2026-01-07T10:00:00+03:00,2026-01-07T11:00:00+03:00,2400,valid,meter",
                        ),
                    });
                await page
                    .getByRole("button", {
                        name: "Preview import",
                        exact: true,
                    })
                    .click();
                const preview = page.locator(".production-preview-table");
                await expect(
                    preview.getByRole("cell", { name: "1.2", exact: true }),
                ).toBeVisible();
                await expect(
                    preview.getByRole("cell", { name: "2.4", exact: true }),
                ).toBeVisible();
                expect(
                    await preview.evaluate(
                        (element) => element.scrollWidth - element.clientWidth,
                    ),
                ).toBeLessThanOrEqual(1);
                await preview.evaluate((element) => {
                    const sidebar = document.querySelector(".sidebar");
                    const offset =
                        sidebar &&
                        getComputedStyle(sidebar).position === "sticky"
                            ? sidebar.getBoundingClientRect().height
                            : 0;
                    window.scrollBy(
                        0,
                        element.getBoundingClientRect().top - offset - 16,
                    );
                });
                await expect(
                    preview.getByRole("cell", { name: "1.2", exact: true }),
                ).toBeInViewport();
                await expect(
                    preview.getByRole("cell", { name: "2.4", exact: true }),
                ).toBeInViewport();
                await page.screenshot({
                    path: testInfo.outputPath("production-import.png"),
                    fullPage: false,
                });
            }
            if (tab === "Outages")
                await page
                    .getByRole("button", { name: "Report outage" })
                    .click();
            await fits(page, tab, audit);
            if (tab === "Calibration" || tab === "Outages")
                await page.screenshot({
                    path: testInfo.outputPath(`${tab.toLowerCase()}.png`),
                    fullPage: true,
                });
        }
        await page.getByRole("button", { name: "All plants" }).click();
        await expect(
            page.getByRole("heading", { name: "Plants" }),
        ).toBeVisible();
        for (const [button, heading] of [
            ["Account", "Account settings"],
            ["Recycle bin", "Recycle bin"],
        ] as const) {
            if (button === "Recycle bin")
                await page
                    .getByRole("navigation", { name: "Main navigation" })
                    .getByRole("button", { name: "Plants", exact: true })
                    .click();
            await page
                .getByRole("button", { name: button, exact: true })
                .click();
            await page
                .getByRole("heading", { name: heading, exact: true })
                .waitFor();
            await fits(page, heading, audit);
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
    await page.goto("/app/");
    await page.getByLabel("Email", { exact: true }).waitFor();
    await page
        .getByRole("navigation", { name: "Account access" })
        .getByRole("button", { name: "Create account" })
        .focus();
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("Email", { exact: true })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("Password", { exact: true })).toBeFocused();
    await fits(page, "Sign in", true);
});

for (const width of [320, 390, 1440]) {
    for (const language of ["en", "tr"] as const) {
        test(`compact workspace navigation fits ${language} at ${width}px`, async ({
            page,
        }, testInfo) => {
            await page.setViewportSize({ width, height: 844 });
            await mockApi(page);
            await page.route("**/api/session/", (route) =>
                route.fulfill({
                    json: { user, language, csrf_token: "signed-token" },
                }),
            );
            await page.route("**/api/tenants/", (route) =>
                route.fulfill({
                    json: paged([
                        tenantRow({
                            id: 1,
                            name: "Sunfield Energy",
                            role: "admin",
                        }),
                        {
                            id: 2,
                            name: "A workspace with a long company name",
                            role: "viewer",
                        },
                    ]),
                }),
            );
            await page.route("**/api/**/members/", (route) =>
                route.fulfill({
                    json: {
                        members: [
                            {
                                id: 2,
                                email: "a.member.with.a.long.address@example.test",
                                role: "viewer",
                            },
                        ],
                    },
                }),
            );
            await page.route("**/invitations/", (route) =>
                route.request().method() === "POST"
                    ? route.fulfill({
                          status: 400,
                          json: errorBody("invalid_request", "Check it.", {
                              fields: { email: "Use a company address." },
                          }),
                      })
                    : route.fulfill({ json: { invitations: [] } }),
            );
            await page.route("**/api/**/trash/**", (route) =>
                route.fulfill({ json: paged([]) }),
            );
            await page.goto("/app/");
            const labels =
                language === "tr"
                    ? {
                          navigation: "Ana menü",
                          workspace: "Çalışma alanı",
                          plants: "Santraller",
                          members: "Üyeler",
                          account: "Hesap",
                          accountHeading: "Hesap ayarları",
                          trash: "Geri dönüşüm kutusu",
                          email: "E-posta adresi",
                          invite: "Davet gönder",
                      }
                    : {
                          navigation: "Main navigation",
                          workspace: "Workspace",
                          plants: "Plants",
                          members: "Members",
                          account: "Account",
                          accountHeading: "Account settings",
                          trash: "Recycle bin",
                          email: "Email address",
                          invite: "Send invitation",
                      };
            const navigation = page.getByRole("navigation", {
                name: labels.navigation,
                exact: true,
            });
            await expect(navigation.getByRole("button")).toHaveCount(4);
            await expect(
                navigation.getByRole("button", { name: labels.trash }),
            ).toHaveCount(0);
            await expect(
                page.getByRole("heading", { level: 1, name: labels.plants }),
            ).toBeVisible();
            await expect(page.locator("main .stats")).toHaveCount(0);
            await expect(
                page.getByRole("combobox", { name: labels.workspace }),
            ).toBeVisible();
            const controls = await navigation
                .getByRole("button")
                .evaluateAll((buttons) =>
                    buttons.map((button) => {
                        const rect = button.getBoundingClientRect();
                        return { top: rect.top, height: rect.height };
                    }),
                );
            expect(controls.every((control) => control.height >= 44)).toBe(
                true,
            );
            if (width < 900)
                expect(
                    new Set(controls.map((control) => control.top)).size,
                ).toBe(1);
            await fits(page, "Portfolio", width === 390);
            await page.screenshot({
                path: testInfo.outputPath("portfolio.png"),
                fullPage: true,
            });
            await page
                .getByRole("button", { name: labels.trash, exact: true })
                .click();
            await expect(
                page.getByRole("heading", { name: labels.trash }),
            ).toBeVisible();
            await expect(
                navigation.getByRole("button", {
                    name: labels.plants,
                    exact: true,
                }),
            ).toHaveAttribute("aria-current", "page");
            await page
                .locator("main")
                .getByRole("button", { name: labels.plants, exact: true })
                .click();
            await navigation
                .getByRole("button", {
                    name: labels.members,
                    exact: true,
                })
                .click();
            await expect(
                page.getByRole("heading", {
                    name: labels.members,
                    exact: true,
                    level: 1,
                }),
            ).toBeVisible();
            await fits(page, "Members", width === 390);
            await page.screenshot({
                path: testInfo.outputPath("members.png"),
                fullPage: true,
            });
            await page
                .getByLabel(labels.email, { exact: true })
                .fill("a.member.with.a.long.address@example.test");
            await page
                .getByRole("button", { name: labels.invite, exact: true })
                .click();
            await expect(page.locator(".form-errors li")).toHaveText(
                `${labels.email}: Use a company address.`,
            );
            await fits(page, "Members with a field error", width === 390);
            await page.screenshot({
                path: testInfo.outputPath("members-field-error.png"),
                fullPage: true,
            });
            await navigation
                .getByRole("button", {
                    name: labels.account,
                    exact: true,
                })
                .click();
            await expect(
                page.getByRole("heading", {
                    name: labels.accountHeading,
                    exact: true,
                }),
            ).toBeVisible();
            await expect(page.locator(".account-disclosure[open]")).toHaveCount(
                0,
            );
            await expect(page.locator(".identity-details[open]")).toHaveCount(
                0,
            );
            await page.screenshot({
                path: testInfo.outputPath("account.png"),
                fullPage: true,
            });
            await page.locator("details").evaluateAll((rows) => {
                rows.forEach((row) => {
                    if (row instanceof HTMLDetailsElement) row.open = true;
                });
            });
            await fits(page, "Account expanded", width === 390);
            await page.screenshot({
                path: testInfo.outputPath("account-expanded.png"),
                fullPage: true,
            });
        });
    }
}

test("mobile viewer retains the read-only indicator and no workspace management actions", async ({
    page,
}) => {
    await page.setViewportSize({ width: 320, height: 740 });
    await mockApi(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "viewer" }),
            ]),
        }),
    );
    await page.goto("/app/");
    await expect(
        page.getByText("Member · changes require approval", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Members", exact: true }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Recycle bin", exact: true }),
    ).toHaveCount(0);
    await fits(page, "Read-only portfolio", false);
});

test("mobile platform administrator can reach administration and customer navigation", async ({
    page,
}) => {
    await page.setViewportSize({ width: 320, height: 740 });
    await mockApi(page, { admin: true });
    await page.goto("/app/w/");
    await expect(page).toHaveURL(/\/app\/w\/1\/$/);
    const navigation = page.getByRole("navigation", {
        name: "Main navigation",
    });
    await expect(
        navigation.getByRole("link", { name: "Administration" }),
    ).toHaveAttribute("href", "/admin/");
    await expect(navigation.locator("button, a")).toHaveCount(5);
    const controls = await navigation
        .locator("button, a")
        .evaluateAll((elements) =>
            elements.map((element) => {
                const rect = element.getBoundingClientRect();
                return {
                    left: rect.left,
                    right: rect.right,
                    height: rect.height,
                };
            }),
        );
    expect(
        controls.every(
            (control) =>
                control.left >= 0 &&
                control.right <= 320 &&
                control.height >= 44,
        ),
    ).toBe(true);
    await fits(page, "Administrator customer workspace", false);
});
