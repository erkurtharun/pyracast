import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { parseProduction } from "../src/lib/production";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";
const region = {
    id: 11,
    name: "Antalya solar park",
    latitude: 36.9,
    longitude: 30.7,
    timezone: "Europe/Istanbul",
    surface: "horizontal",
    revision: 1,
    status: "approved",
};
const plant = {
    id: 21,
    name: "East array",
    region_id: 11,
    created_at: "2026-01-01T00:00:00Z",
    current_configuration: {
        id: 31,
        revision: 1,
        binding_id: 1,
        created_at: "2026-01-01T00:00:00Z",
        dc_capacity_mw: 5,
        inverter_capacity_mw: 4,
        export_limit_mw: null,
        measurement_basis: "net_export",
    },
};
const user = { id: 1, username: "solar.user", email: "solar@example.test" };
const paged = (results: unknown[]) => ({
    results,
    count: results.length,
    next: null,
    previous: null,
});
async function mockApi(
    page: Page,
    { anonymous = false, empty = false, failed = false } = {},
) {
    let signedIn = !anonymous;
    await page.route("**/api/**", async (route) => {
        const path = new URL(route.request().url()).pathname,
            method = route.request().method();
        let data: unknown;
        if (path === "/api/session/login/") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "anonymous-token",
            );
            signedIn = true;
            data = { user, csrf_token: "signed-token" };
        } else if (path === "/api/session/logout/") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            signedIn = false;
            data = { user: null, csrf_token: "anonymous-token" };
        } else if (path === "/api/session/") {
            data = {
                user: signedIn ? user : null,
                csrf_token: signedIn ? "signed-token" : "anonymous-token",
            };
        } else if (path === "/api/tenants/")
            data = paged([{ id: 1, name: "Sunfield Energy" }]);
        else if (path === "/api/tenants/1/regions/") {
            if (failed) {
                await route.fulfill({
                    status: 503,
                    json: {
                        detail: "Forecast service is temporarily unavailable.",
                    },
                });
                return;
            }
            data =
                method === "POST"
                    ? {
                          ...region,
                          ...route.request().postDataJSON(),
                          status: "pending",
                      }
                    : paged(empty ? [] : [region]);
        } else if (path.endsWith("/plants/"))
            data = method === "POST" ? plant : paged([plant]);
        else if (path.endsWith("/observations/")) {
            expect(route.request().postDataJSON()).toMatchObject({
                configuration_id: 31,
                unit: "kWh",
            });
            data = { created: 1, existing: 0, count: 1, ids: [1] };
        } else if (path.endsWith("/production/"))
            data = {
                timezone: region.timezone,
                available_start_date: null,
                available_end_date: null,
                start_date: "2026-01-01",
                end_date: "2026-01-07",
                days: [],
                intervals: [],
                observations: [],
            };
        else if (path.endsWith("/calibrations/"))
            data =
                method === "POST"
                    ? {
                          id: 41,
                          status: "pending",
                          assessment: "pending",
                          factor: null,
                          result: {},
                          dataset_sha256: "",
                      }
                    : paged([]);
        else if (path.endsWith("/reminders/"))
            data = {
                enabled: false,
                email: "",
                times: ["08:00"],
                effective_email: user.email,
                timezone: region.timezone,
                quota: { daily_reminder_limit: 10, queries_per_email: 0 },
                deliveries: [],
            };
        else if (path.endsWith("/performance/"))
            data = {
                start_date: "2026-01-01",
                end_date: "2026-01-01",
                matched_days: 0,
                requested_days: 1,
                metrics: null,
                baseline_metrics: null,
                days: [],
            };
        else if (path.endsWith("/forecast/"))
            data = {
                region_id: 11,
                date: "2026-01-01",
                timezone: region.timezone,
                status: "approved",
                freshness: "missing",
                result: null,
                collected_at: null,
                issued_at: null,
                ...(path.includes("/plants/") && {
                    configuration_id: 31,
                    unavailable_reason: "forecast_unavailable",
                }),
            };
        else throw new Error(`Unmocked API: ${method} ${path}`);
        await route.fulfill({ json: data });
    });
}
test("mock contract: signs in with CSRF, selects region, and signs out", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.goto("/");
    await page.getByLabel("Username", { exact: true }).fill(user.username);
    await page.getByLabel("Password", { exact: true }).fill("fixture-password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("heading", { name: "A clearer outlook." }),
    ).toBeVisible();
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await expect(
        page.getByRole("heading", { name: "No forecast for this day yet" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});

test("initial session has one CSRF handshake during Strict Mode and focus events", async ({
    page,
}) => {
    let requests = 0;
    let release!: () => void;
    let started!: () => void;
    const barrier = new Promise<void>((resolve) => {
        release = resolve;
    });
    const observed = new Promise<void>((resolve) => {
        started = resolve;
    });
    await page.route("**/api/session/", async (route) => {
        requests++;
        started();
        await barrier;
        await route.fulfill({ json: { user: null, csrf_token: "one-token" } });
    });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await observed;
    await page.evaluate(() => {
        window.dispatchEvent(new Event("focus"));
        document.dispatchEvent(new Event("visibilitychange"));
    });
    release();
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
    expect(requests).toBe(1);
});
test("mock contract: requests a region and surfaces pending approval", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await page.goto("/");
    await page.getByRole("button", { name: "Request a region" }).click();
    await page
        .getByLabel("Region name", { exact: true })
        .fill("New solar park");
    await page.getByLabel("Latitude").fill("36.5");
    await page.getByLabel("Longitude").fill("30.2");
    await page
        .getByRole("button", { name: "Request region", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "New solar park" }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Add a plant" }),
    ).toBeDisabled();
});
test("plant creation preserves capacities and opens tomorrow across DST", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-03-28T22:30:00Z"));
    await mockApi(page);
    await page.route("**/api/tenants/1/regions/", async (route) => {
        await route.fulfill({
            json: paged([{ ...region, timezone: "Europe/Berlin" }]),
        });
    });
    await page.route("**/api/tenants/1/regions/11/plants/", async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        expect(route.request().postDataJSON()).toEqual({
            name: "New array",
            dc_capacity_mw: 130,
            inverter_capacity_mw: 100,
            export_limit_mw: null,
            measurement_basis: "net_export",
            mounting_type: "unknown",
            surface_tilt: null,
            surface_azimuth: null,
        });
        await route.fulfill({
            json: {
                ...plant,
                name: "New array",
                current_configuration: {
                    ...plant.current_configuration,
                    dc_capacity_mw: 130,
                    inverter_capacity_mw: 100,
                },
            },
        });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: "Add a plant" }).click();
    await page.getByLabel("Plant name").fill("New array");
    await page.getByLabel("DC nameplate capacity (MW)").fill("130");
    await page.getByLabel("Inverter AC capacity (MW, optional)").fill("100");
    await page.getByRole("button", { name: "Create plant" }).click();
    await expect(
        page.getByRole("heading", { name: "New array" }),
    ).toBeVisible();
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-03-29");
    await expect(
        page.getByRole("heading", { name: "No forecast for this day yet" }),
    ).toBeVisible();
});

test("plant configuration saves fixed angles and clears them when tracking", async ({
    page,
}) => {
    await mockApi(page);
    let updated = {
        ...plant,
        current_configuration: {
            ...plant.current_configuration,
            mounting_type: "fixed",
            surface_tilt: 33,
            surface_azimuth: 180,
        },
    };
    const bodies: Record<string, unknown>[] = [];
    await page.route("**/plants/21/", async (route) => {
        if (route.request().method() !== "PUT") return route.fallback();
        const body = route.request().postDataJSON();
        bodies.push(body);
        updated = {
            ...updated,
            name: body.name,
            current_configuration: {
                ...updated.current_configuration,
                ...body,
                id: 31 + bodies.length,
            },
        };
        await route.fulfill({ json: updated });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Production import" }).click();
    await page.getByLabel("Production CSV").setInputFiles({
        name: "production.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(
            "starts_at,ends_at,energy,quality,source\n2026-01-01T09:00:00+03:00,2026-01-01T09:30:00+03:00,250,valid,meter\n",
        ),
    });
    await expect(page.getByText(/1 valid observations ready/)).toBeVisible();
    await page.getByRole("button", { name: "Edit configuration" }).click();
    await page.getByLabel("Mounting type").selectOption("fixed");
    await page.getByLabel("Surface tilt (degrees, optional)").fill("0");
    await page.getByLabel("Surface azimuth (degrees, optional)").fill("360");
    await page.getByRole("button", { name: "Save configuration" }).click();
    expect(bodies).toHaveLength(0);
    await page.getByLabel("Surface azimuth (degrees, optional)").fill("180");
    await page.getByRole("button", { name: "Save configuration" }).click();
    await expect.poll(() => bodies.length).toBe(1);
    await expect(page.getByText(/1 valid observations ready/)).toHaveCount(0);
    expect(bodies[0]).toMatchObject({
        mounting_type: "fixed",
        surface_tilt: 0,
        surface_azimuth: 180,
        dc_capacity_mw: 5,
        inverter_capacity_mw: 4,
        export_limit_mw: null,
    });
    await page.getByRole("button", { name: "Edit configuration" }).click();
    await expect(
        page.getByLabel("Surface tilt (degrees, optional)"),
    ).toHaveValue("0");
    await page.getByLabel("Mounting type").selectOption("tracking");
    await page.getByRole("button", { name: "Save configuration" }).click();
    await expect.poll(() => bodies.length).toBe(2);
    expect(bodies[1]).toMatchObject({
        mounting_type: "tracking",
        surface_tilt: null,
        surface_azimuth: null,
    });
});

test("missing plant configuration explains the selected day", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/plants/21/forecast/**", async (route) => {
        await route.fulfill({
            json: {
                timezone: region.timezone,
                freshness: "missing",
                result: null,
                configuration_id: null,
                unavailable_reason: "configuration_unavailable",
            },
        });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(
        page.getByRole("heading", { name: "No configuration for this day" }),
    ).toBeVisible();
    await expect(
        page.getByText(
            "No configuration existed before the selected day began. Choose a later date.",
        ),
    ).toBeVisible();
    await expect(page.getByText("configuration_unavailable")).toHaveCount(0);
});

test("mock contract: imports validated production with explicit energy units", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Production import" }).click();
    await page.getByLabel("Production CSV").setInputFiles({
        name: "production.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(
            "starts_at,ends_at,energy,quality,source\n2026-01-01T09:00:00+03:00,2026-01-01T09:30:00+03:00,250,valid,meter",
        ),
    });
    await expect(page.getByText(/1 valid observations ready/)).toBeVisible();
    await page.getByRole("button", { name: "Import 1 observations" }).click();
    await expect(
        page.getByText(
            "Import complete: 1 created, 0 already present, 1 total.",
        ),
    ).toBeVisible();
});

for (const staleRead of ["resolve", "reject"] as const) {
    for (const selection of ["new file", "clear"] as const) {
        test(`CSV ignores an old ${staleRead} after ${selection}`, async ({
            page,
        }) => {
            await page.addInitScript(
                ({ staleRead }) => {
                    const read = File.prototype.text;
                    File.prototype.text = function () {
                        if (this.name !== "old.csv") return read.call(this);
                        return new Promise<string>((resolve, reject) => {
                            Object.assign(window, {
                                releaseOldCsv: async () => {
                                    if (staleRead === "reject")
                                        reject(
                                            new Error("Old file read failed."),
                                        );
                                    else resolve(await read.call(this));
                                },
                            });
                        });
                    };
                },
                { staleRead },
            );
            await mockApi(page);
            await page.goto("/");
            await page
                .getByRole("button", { name: /Antalya solar park/ })
                .click();
            await page
                .getByRole("button", { name: "Solar plants", exact: true })
                .click();
            await page.getByRole("button", { name: /East array/ }).click();
            await page
                .getByRole("button", { name: "Production import" })
                .click();
            const csv = (energy: number, source: string) =>
                Buffer.from(
                    `starts_at,ends_at,energy,quality,source\n2026-01-01T09:00:00+03:00,2026-01-01T09:30:00+03:00,${energy},valid,${source}`,
                );
            const input = page.getByLabel("Production CSV");
            await input.setInputFiles({
                name: "old.csv",
                mimeType: "text/csv",
                buffer: csv(250, "old-meter"),
            });
            if (selection === "clear") await input.setInputFiles([]);
            else {
                await input.setInputFiles({
                    name: "new.csv",
                    mimeType: "text/csv",
                    buffer: csv(777, "new-meter"),
                });
                await expect(
                    page.getByText(/new.csv: 1 valid observations ready/),
                ).toBeVisible();
            }
            await page.evaluate(async () => {
                await (
                    window as unknown as { releaseOldCsv: () => Promise<void> }
                ).releaseOldCsv();
                await new Promise<void>((resolve) =>
                    requestAnimationFrame(() => resolve()),
                );
            });
            await expect(
                page.getByText("Old file read failed.", { exact: true }),
            ).toHaveCount(0);
            if (selection === "clear") {
                await expect(
                    page.getByText(/valid observations ready/),
                ).toHaveCount(0);
                await expect(
                    page.getByRole("button", {
                        name: "Import observations",
                        exact: true,
                    }),
                ).toBeVisible();
                return;
            }
            const imported = page.waitForRequest(
                (request) =>
                    request.method() === "POST" &&
                    request.url().includes("/observations/"),
            );
            await page
                .getByRole("button", { name: "Import 1 observations" })
                .click();
            expect((await imported).postDataJSON()).toMatchObject({
                observations: [{ energy: 777, source: "new-meter" }],
            });
        });
    }
}
test("open regional and plant forecasts refresh after new data arrives", async ({
    page,
}) => {
    await page.clock.install();
    await mockApi(page);
    let revised = false;
    await page.route("**/forecast/**", async (route) => {
        await route.fulfill({
            json: {
                timezone: region.timezone,
                freshness: "missing",
                result: null,
                unavailable_reason: revised
                    ? "estimated_actuals_unavailable"
                    : "forecast_unavailable",
            },
        });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await expect(
        page.getByRole("heading", { name: "No forecast for this day yet" }),
    ).toBeVisible();
    revised = true;
    await page.clock.fastForward(30_000);
    await expect(
        page.getByRole("heading", {
            name: "No estimated actuals for this day",
        }),
    ).toBeVisible();
    revised = false;
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(
        page.getByRole("heading", { name: "No forecast for this day yet" }),
    ).toBeVisible();
    revised = true;
    await page.clock.fastForward(30_000);
    await expect(
        page.getByRole("heading", {
            name: "No estimated actuals for this day",
        }),
    ).toBeVisible();
});

test("region collection schedule validates and saves distinct local times", async ({
    page,
}) => {
    await page.clock.install();
    await mockApi(page);
    let saves = 0;
    let schedule = {
        enabled: true,
        times: ["09:00"],
        timezone: region.timezone,
        daily_request_limit: 10,
        used_today: 2,
        remaining_today: 8,
        history_requests_per_day: 1,
        max_forecast_times: 9,
        effective_from: "2026-01-01T00:00:00Z",
        quota_utc_day: "2026-01-01",
        requests_by_outcome: { succeeded: 1, failed: 1, uncertain: 0 },
        recent_jobs: [
            {
                id: 1,
                endpoint: "forecasts",
                status: "failed",
                scheduled_slot: "2026-01-01T07:00:00Z",
                available_at: "2026-01-01T07:00:00Z",
                attempts: 1,
                last_error: "provider_failure",
            },
            {
                id: 2,
                endpoint: "estimated_actuals",
                status: "succeeded",
                scheduled_slot: "2026-01-01T07:00:00Z",
                available_at: "2026-01-01T07:00:00Z",
                attempts: 2,
                last_error: "quota_exhausted",
            },
        ],
    };
    await page.route("**/collection-schedule/", async (route) => {
        if (route.request().method() === "PUT") {
            saves++;
            expect(route.request().postDataJSON()).toEqual({
                enabled: true,
                times: ["09:00", "10:00", "11:00"],
            });
            schedule = { ...schedule, ...route.request().postDataJSON() };
        }
        await route.fulfill({ json: schedule });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page.getByRole("button", { name: "Data collection" }).click();
    await expect(page.getByText(/first eligible time each day/)).toBeVisible();
    await expect(
        page.getByText("Account requests used today (UTC)"),
    ).toBeVisible();
    await expect(
        page.getByText(/Tracked attempts: 1 succeeded, 1 failed, 0 uncertain/),
    ).toBeVisible();
    await expect(page.getByText(/Provider request failed/)).toBeVisible();
    await expect(
        page.getByText(/Historical estimates - Succeeded/),
    ).toBeVisible();
    await expect(page.getByText(/Daily account budget exhausted/)).toHaveCount(
        0,
    );
    await expect(
        page.getByText(/not a guarantee of provider quota/),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Save collection schedule" })
        .click();
    expect(saves).toBe(0);
    await page.getByRole("button", { name: "+ Add time" }).click();
    await page
        .getByRole("button", { name: "Save collection schedule" })
        .click();
    await expect(
        page.getByText("Enter a valid time in every row."),
    ).toBeVisible();
    await page.getByLabel("Collection time 2 (Europe/Istanbul)").fill("09:00");
    await page
        .getByRole("button", { name: "Save collection schedule" })
        .click();
    await expect(
        page.getByText("Each collection time must be unique."),
    ).toBeVisible();
    await page.getByLabel("Collection time 2 (Europe/Istanbul)").fill("11:00");
    schedule.used_today = 3;
    schedule.effective_from = "2026-01-01T00:00:30Z";
    const refreshed = page.waitForResponse(
        (response) =>
            response.url().includes("/collection-schedule/") &&
            response.request().method() === "GET",
    );
    await page.clock.fastForward(30_000);
    await refreshed;
    await expect(
        page.getByLabel("Collection time 2 (Europe/Istanbul)"),
    ).toHaveValue("11:00");
    await expect(
        page.getByText("Account requests used today (UTC)").locator(".."),
    ).toContainText("3");
    await page.getByRole("button", { name: "+ Add time" }).click();
    await page.getByLabel("Collection time 3 (Europe/Istanbul)").fill("10:00");
    await page
        .getByRole("button", { name: "Save collection schedule" })
        .click();
    await expect(
        page.getByLabel("Collection time 2 (Europe/Istanbul)"),
    ).toHaveValue("10:00");
    await expect(
        page.getByLabel("Collection time 3 (Europe/Istanbul)"),
    ).toHaveValue("11:00");
    expect(saves).toBe(1);
    await page
        .getByRole("button", { name: "Remove collection time 3" })
        .click();
    await expect(
        page.getByLabel("Collection time 3 (Europe/Istanbul)"),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Email reminders" }).click();
    await expect(
        page.getByText(/Sending an email does not request new provider data/),
    ).toBeVisible();
});

test("regional and plant data sources identify estimated actuals and retain archived forecasts", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-03-28T12:00:00Z"));
    await mockApi(page);
    const sources: string[] = [];
    await page.route("**/forecast/**", async (route) => {
        const url = new URL(route.request().url());
        const source = url.searchParams.get("data_source") ?? "missing";
        sources.push(source);
        await route.fulfill({
            json: {
                timezone: region.timezone,
                freshness: "fresh",
                source_kind:
                    source === "forecast" ||
                    url.searchParams.get("date")! >= "2026-03-28"
                        ? "forecast"
                        : "estimated_actuals",
                result: {
                    complete: true,
                    intervals: [],
                    energy_kwh_m2: 4,
                    energy_mwh: 5,
                },
            },
        });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page.getByLabel("Forecast date").fill("2026-03-27");
    await expect(
        page.getByText("Satellite/model estimates, not meter readings."),
    ).toBeVisible();
    expect(sources).toContain("auto");
    await page.getByLabel("Data source").selectOption("forecast");
    await expect.poll(() => sources.at(-1)).toBe("forecast");
    await page.getByLabel("Forecast date").fill("2026-03-28");
    await page.getByLabel("Data source").selectOption("estimated_actuals");
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-03-27");
    await expect.poll(() => sources.at(-1)).toBe("estimated_actuals");
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByLabel("Forecast date").fill("2026-03-27");
    await expect(
        page.getByText("Satellite/model estimates, not meter readings."),
    ).toBeVisible();
    await page.getByLabel("Data source").selectOption("forecast");
    await expect.poll(() => sources.at(-1)).toBe("forecast");
    await page.getByRole("button", { name: "Calibration" }).click();
    await expect(page.getByText(/historical estimated actuals/i)).toBeVisible();
});

test("forecast crops edge zeros, retains gaps and downloads the full safe CSV", async ({
    page,
}) => {
    await mockApi(page);
    const values = [0, 12.34567, null, 5, 0];
    await page.route("**/forecast/**", (route) =>
        route.fulfill({
            json: {
                date: "2026-01-01",
                timezone: region.timezone,
                freshness: "fresh",
                source_kind: "forecast",
                result: {
                    complete: false,
                    model_version: 'Ölçüm, "north"\nroof',
                    provenance: { supplier: " =SUM(A1:A2)", offset: -2.5 },
                    intervals: values.map((ghi_wm2, index) => ({
                        start: `2026-01-01T${String(index + 6).padStart(2, "0")}:00:00+03:00`,
                        end: `2026-01-01T${String(index + 7).padStart(2, "0")}:00:00+03:00`,
                        ghi_wm2,
                    })),
                },
            },
        }),
    );
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page.getByText("View interval data (3 intervals)").click();
    const table = page.getByRole("table");
    await expect(table.getByRole("row")).toHaveCount(4);
    await expect(
        table.getByRole("cell", { name: "07:00", exact: true }),
    ).toBeVisible();
    await expect(
        table.getByRole("cell", { name: "06:00", exact: true }),
    ).toHaveCount(0);
    await expect(page.locator(".chart-column.missing")).toHaveCount(1);
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download CSV", exact: true })
        .click();
    const bytes = await readFile((await (await download).path())!);
    expect([...bytes.subarray(0, 3)]).toEqual([239, 187, 191]);
    const csv = bytes.toString("utf8");
    const parsed = Papa.parse<Record<string, string>>(csv, {
        header: true,
        skipEmptyLines: true,
    });
    expect(parsed.errors).toEqual([]);
    expect(parsed.data.map((row) => row.ghi_wm2)).toEqual([
        "0",
        "12.34567",
        "",
        "5",
        "0",
    ]);
    expect(parsed.data[0]).toMatchObject({
        start: "2026-01-01T06:00:00+03:00",
        timezone: region.timezone,
        "result.model_version": 'Ölçüm, "north"\nroof',
        "result.provenance.supplier": "' =SUM(A1:A2)",
        "result.provenance.offset": "-2.5",
    });
    expect(csv).toContain(",-2.5,");
});

test("journal retains unmeasured reference slots and internal missing comparisons", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/production/**", (route) =>
        route.fulfill({
            json: {
                timezone: region.timezone,
                plant_id: 21,
                configuration_id: 31,
                start_date: "2026-01-01",
                end_date: "2026-01-01",
                resolution_minutes: 60,
                days: [],
                observations: [],
                intervals: [false, true, false, true, false].map(
                    (reference_available, index) => ({
                        starts_at: `2026-01-01T${String(index + 6).padStart(2, "0")}:00:00+03:00`,
                        ends_at: `2026-01-01T${String(index + 7).padStart(2, "0")}:00:00+03:00`,
                        reference_available,
                        can_enter: reference_available,
                        observation: null,
                        unavailable_reason: reference_available
                            ? null
                            : "estimated_actuals_unavailable",
                    }),
                ),
            },
        }),
    );
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await expect(page.getByRole("table").getByRole("row")).toHaveCount(4);
    const missing = page.getByRole("spinbutton", {
        name: "Production 2026-01-01T08:00:00+03:00",
        exact: true,
    });
    await expect(missing).toBeDisabled();
    await page
        .getByRole("spinbutton", {
            name: "Production 2026-01-01T07:00:00+03:00",
            exact: true,
        })
        .fill("0");
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download production journal CSV" })
        .click();
    const csv = await readFile((await (await download).path())!, "utf8");
    const records = Papa.parse<Record<string, string>>(csv, {
        header: true,
        skipEmptyLines: true,
    }).data;
    expect(records).toHaveLength(5);
    expect(records[1]).toMatchObject({
        draft_energy: "0",
        draft_unit: "kWh",
        reference_available: "true",
    });
});

test("all-zero forecasts remain visible as measured zero values", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/forecast/**", (route) =>
        route.fulfill({
            json: {
                timezone: region.timezone,
                freshness: "fresh",
                result: {
                    complete: true,
                    intervals: [
                        {
                            start: "2026-01-01T09:00:00Z",
                            end: "2026-01-01T10:00:00Z",
                            ghi_wm2: 0,
                        },
                        {
                            start: "2026-01-01T10:00:00Z",
                            end: "2026-01-01T11:00:00Z",
                            ghi_wm2: null,
                        },
                        {
                            start: "2026-01-01T11:00:00Z",
                            end: "2026-01-01T12:00:00Z",
                            ghi_wm2: 0,
                        },
                    ],
                },
            },
        }),
    );
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page.getByText("View interval data (3 intervals)").click();
    await expect(
        page.getByRole("table").getByRole("cell", { name: "0", exact: true }),
    ).toHaveCount(2);
    await expect(page.locator(".chart-column.missing")).toHaveCount(1);
});

test("mock contract: saves cached forecast reminders without provider queries", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page.getByRole("button", { name: "Email reminders" }).click();
    await page.getByLabel("Enable forecast emails for this region").check();
    await page.getByLabel("Delivery times").fill("08:00, 17:30");
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().includes("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON()).toEqual({
        enabled: true,
        email: "",
        times: ["08:00", "17:30"],
    });
    await expect(page.getByText("Provider queries per email")).toBeVisible();
});
test("mock contract: visible API error, empty state and accessible mobile layout", async ({
    page,
}) => {
    await mockApi(page, { failed: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await expect(
        page.getByRole("alert").filter({ hasText: "temporarily unavailable" }),
    ).toContainText("temporarily unavailable");
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    const scan = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze();
    expect(scan.violations).toEqual([]);
});
test("CSV rejects ambiguous units, offsets, overlaps, invalid energy and oversized batches", () => {
    const header = "starts_at,ends_at,energy,quality,source\n";
    expect(() => parseProduction("start,end,power\n")).toThrow("header");
    expect(() =>
        parseProduction(
            header + "2026-01-01T09:00:00,2026-01-01T09:30:00,-1,valid,meter",
        ),
    ).toThrow("timezone-aware");
    expect(() =>
        parseProduction(
            header + "2026-01-01T09:00:00Z,2026-01-01T09:30:00Z,-1,valid,meter",
        ),
    ).toThrow("non-negative");
    const row = "2026-01-01T09:00:00Z,2026-01-01T09:30:00Z,1,valid,meter";
    expect(() => parseProduction(header + row + "\n" + row)).toThrow(
        "overlapping",
    );
    expect(() =>
        parseProduction(header + Array(2001).fill(row).join("\n")),
    ).toThrow("2,000");
});

test("CSV preserves quoted commas and multiline source fields and rejects missing energy", () => {
    const header = "starts_at,ends_at,energy,quality,source\r\n";
    const interval = "2026-01-01T09:00:00Z,2026-01-01T09:30:00Z";
    const rows = parseProduction(
        header + interval + ',0,valid,"Meter, north\nroof"\r\n',
    );
    expect(rows).toEqual([
        {
            starts_at: "2026-01-01T09:00:00Z",
            ends_at: "2026-01-01T09:30:00Z",
            energy: 0,
            quality: "valid",
            source: "Meter, north\nroof",
        },
    ]);
    expect(() => parseProduction(header + interval + ",,valid,meter")).toThrow(
        "non-negative",
    );
    expect(() =>
        parseProduction(header + interval + ',1,valid,"unterminated'),
    ).toThrow("could not be parsed");
});

test("mock contract: follows tenant pagination and keeps missing intervals visible", async ({
    page,
}, testInfo) => {
    await mockApi(page);
    await page.route(/\/api\/tenants\/(?:\?.*)?$/, async (route) => {
        const second = new URL(route.request().url()).searchParams.has("page");
        await route.fulfill({
            json: {
                count: 2,
                next: second ? null : "http://backend.test/api/tenants/?page=2",
                previous: null,
                results: [
                    {
                        id: second ? 2 : 1,
                        name: second ? "Second workspace" : "Sunfield Energy",
                    },
                ],
            },
        });
    });
    await page.route(/\/forecast\/\?/, async (route) =>
        route.fulfill({
            json: {
                timezone: region.timezone,
                freshness: "fresh",
                collected_at: "2026-01-01T07:00:00Z",
                result: {
                    energy_kwh_m2: null,
                    partial_energy_kwh_m2: 2.5,
                    coverage_fraction: 0.5,
                    complete: false,
                    model_version: "fixture-model",
                    quality_flags: ["synthetic_fixture"],
                    intervals: [
                        {
                            start: "2026-01-01T09:00:00Z",
                            end: "2026-01-01T09:30:00Z",
                            ghi_wm2: 500,
                            quality_flags: [],
                        },
                        {
                            start: "2026-01-01T09:30:00Z",
                            end: "2026-01-01T10:00:00Z",
                            ghi_wm2: null,
                            quality_flags: ["missing"],
                        },
                        {
                            start: "2026-01-01T10:00:00Z",
                            end: "2026-01-01T10:30:00Z",
                            ghi_wm2: 250,
                            quality_flags: [],
                        },
                    ],
                },
            },
        }),
    );
    await page.goto("/");
    await expect(
        page.getByRole("option", { name: "Second workspace" }),
    ).toHaveCount(1);
    await page.screenshot({
        path: testInfo.outputPath("overview-desktop.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await expect(page.getByRole("status")).toContainText("Partial coverage");
    await expect(
        page.getByText("synthetic fixture", { exact: true }),
    ).toBeVisible();
    await page.getByText("View interval data (3 intervals)").click();
    await expect(
        page.getByRole("cell", { name: "500", exact: true }),
    ).toBeVisible();
    const scan = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze();
    expect(scan.violations).toEqual([]);
});
async function openJournal(page: Page) {
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Production journal" }),
    ).toBeVisible();
}

test("performance hides stale CSV while a selected range is loading", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    await mockApi(page);
    let release: () => void = () => {};
    const responseReady = new Promise<void>((resolve) => {
        release = resolve;
    });
    let requested = false;
    await page.route("**/plants/21/performance/**", async (route) => {
        const selected = new URL(route.request().url()).searchParams.has(
            "start_date",
        );
        if (selected) {
            requested = true;
            await responseReady;
        }
        await route.fulfill({
            json: {
                start_date: selected ? "2026-11-01" : "2026-10-01",
                end_date: selected ? "2026-11-02" : "2026-10-02",
                matched_days: 0,
                requested_days: 2,
                metrics: null,
                baseline_metrics: null,
                days: [],
            },
        });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    const performance = page.locator("section").filter({
        has: page.getByRole("heading", {
            name: "Forecast performance",
            exact: true,
        }),
    });
    const csv = performance.getByRole("button", {
        name: "Download CSV",
        exact: true,
    });
    await expect(csv).toBeVisible();
    await page.getByLabel("Start date").fill("2026-11-01");
    await page.getByLabel("End date").fill("2026-11-02");
    await page.getByRole("button", { name: "Load performance" }).click();
    try {
        await expect.poll(() => requested).toBe(true);
        await expect(csv).toHaveCount(0);
        await expect(performance.getByRole("status")).toContainText(
            "Loading performance",
        );
    } finally {
        release();
    }
    await expect(csv).toBeVisible();
    const download = page.waitForEvent("download");
    await csv.click();
    const exported = await download;
    expect(exported.suggestedFilename()).toBe(
        "performance-2026-11-01-2026-11-02.csv",
    );
    const records = Papa.parse<Record<string, string>>(
        await readFile((await exported.path())!, "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(records[0]).toMatchObject({
        start_date: "2026-11-01",
        end_date: "2026-11-02",
    });
});

test("forecast performance shows replay metrics, missing days and selected range", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    await mockApi(page);
    let lastQuery = "";
    await page.route("**/plants/21/performance/**", async (route) => {
        lastQuery = new URL(route.request().url()).search;
        await route.fulfill({
            json: {
                start_date: "2026-11-01",
                end_date: "2026-11-02",
                matched_days: 1,
                requested_days: 2,
                metrics: {
                    daily_mae_mwh: 1.5,
                    daily_bias_mwh: -1.5,
                    source_positive_mae_mw: lastQuery ? null : 0.3,
                },
                baseline_metrics: { daily_mae_mwh: 2.5 },
                days: [
                    {
                        date: "2026-11-01",
                        status: "matched",
                        reason: null,
                        measured_energy_mwh: 10,
                        predicted_energy_mwh: 8.5,
                        error_mwh: -1.5,
                    },
                    {
                        date: "2026-11-02",
                        status: "missing",
                        reason: "forecast_not_available",
                        measured_energy_mwh: null,
                        predicted_energy_mwh: null,
                        error_mwh: null,
                    },
                ],
            },
        });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(
        page.getByRole("heading", { name: "Forecast performance" }),
    ).toBeVisible();
    await expect(
        page.getByText("Day-ahead forecast was not available"),
    ).toBeVisible();
    await expect(
        page.getByText("This does not establish", { exact: false }),
    ).toBeVisible();
    await expect(
        page.getByText("Baseline daylight energy error"),
    ).toBeVisible();
    await expect(
        page
            .locator(".stat")
            .filter({ hasText: "Baseline daylight energy error" }),
    ).toContainText("2.5");
    await expect(
        page.getByText("Generating-interval error", { exact: true }),
    ).toBeVisible();
    await page.getByLabel("Start date").fill("2026-11-01");
    await page.getByLabel("End date").fill("2026-11-02");
    await page.getByRole("button", { name: "Load performance" }).click();
    await expect
        .poll(() => lastQuery)
        .toContain("start_date=2026-11-01&end_date=2026-11-02");
    await expect.poll(() => lastQuery).toContain("daylight_only=true");
    await expect(page.getByRole("cell", { name: "8.5" })).toBeVisible();
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download CSV", exact: true })
        .last()
        .click();
    const exported = Papa.parse<Record<string, string>>(
        await readFile((await (await download).path())!, "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(exported).toHaveLength(2);
    expect(exported[0]).toMatchObject({
        date: "2026-11-01",
        error_mwh: "-1.5",
        "metrics.daily_bias_mwh": "-1.5",
        timezone: region.timezone,
    });
    expect(exported[1]).toMatchObject({
        date: "2026-11-02",
        error_mwh: "",
        reason: "forecast_not_available",
    });
    await expect(
        page.locator(".stat").filter({ hasText: "Generating-interval error" }),
    ).toContainText("—");
});

test("calibration detail explains support, rejection and incumbent comparison", async ({
    page,
}) => {
    await mockApi(page);
    const run = {
        id: 40,
        window_start: "2026-01-01",
        window_end: "2026-01-10",
        status: "rejected",
        assessment: "provisional",
        factor: 1.1,
        result: {
            metric_scope: "daylight_intervals",
            valid_days: 10,
            training_days: 8,
            holdout_days: 2,
            training_support: { informative_days: 4, minimum_days: 5 },
            reasons: ["incumbent_mae_regression"],
            rolling_origin: { status: "diagnostic", folds: [{}, {}] },
            baseline: { daily_mae_mwh: 3 },
            candidate: { daily_mae_mwh: 2 },
            incumbent: { daily_mae_mwh: 1 },
            incumbent_factor: 1.2,
        },
    };
    await page.route("**/plants/21/calibrations/", (route) =>
        route.fulfill({ json: paged([run]) }),
    );
    await page.route("**/plants/21/calibrations/40/", (route) =>
        route.fulfill({ json: run }),
    );
    await openJournal(page);
    await page.getByRole("button", { name: "View details" }).click();
    await expect(
        page.getByText(
            "The candidate increased error compared with the selection at training.",
        ),
    ).toBeVisible();
    await expect(page.getByText("4 / 5 required")).toBeVisible();
    await expect(
        page.getByText(
            "Metrics use whole meter intervals intersecting daylight, not 24-hour totals.",
        ),
    ).toBeVisible();
    await expect(page.getByText("Daylight energy MAE (MWh)")).toBeVisible();
    await expect(page.getByText("Daylight energy bias (MWh)")).toBeVisible();
    await expect(
        page.getByText("Rolling-origin diagnostic: diagnostic; 2 folds."),
    ).toBeVisible();
    await expect(
        page.getByRole("columnheader", {
            name: "Selection at training (factor 1.2)",
        }),
    ).toBeVisible();
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download calibration details CSV" })
        .click();
    const exported = Papa.parse<Record<string, string>>(
        await readFile((await (await download).path())!, "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(exported[0]).toMatchObject({
        id: "40",
        factor: "1.1",
        "result.training_support.informative_days": "4",
        "result.incumbent_factor": "1.2",
    });
});

test("production journal saves missing readings, preserves dates and shows archived availability", async ({
    page,
}) => {
    await mockApi(page);
    type Saved = {
        id: number;
        configuration_id: number;
        starts_at: string;
        ends_at: string;
        energy_mwh: number;
        quality: string;
        source: string;
    };
    const observations: Saved[] = [];
    const previousRun = {
        id: 40,
        window_start: "2025-12-01",
        window_end: "2025-12-07",
        status: "ready",
        assessment: "provisional",
        factor: 1.1,
        result: {},
    };
    await page.route("**/plants/21/calibrations/", async (route) => {
        await route.fulfill({
            json: paged(
                observations.length
                    ? [
                          previousRun,
                          {
                              ...previousRun,
                              id: 41,
                              status: "pending",
                              assessment: "pending",
                              factor: null,
                          },
                      ]
                    : [previousRun],
            ),
        });
    });
    await page.route("**/plants/21/calibrations/40/", async (route) => {
        await route.fulfill({ json: previousRun });
    });

    await page.route("**/plants/21/observations/", async (route) => {
        const body = route.request().postDataJSON();
        expect(body).toMatchObject({ configuration_id: 31, unit: "MWh" });
        for (const row of body.observations)
            observations.push({
                ...row,
                energy_mwh: row.energy,
                configuration_id: 31,
                id: observations.length + 1,
            });
        await route.fulfill({
            json: {
                created: body.observations.length,
                existing: 0,
                count: body.observations.length,
            },
        });
    });
    await page.route("**/plants/21/production/**", async (route) => {
        const url = new URL(route.request().url()),
            date = url.searchParams.get("start_date") ?? "2026-01-07",
            resolution = Number(url.searchParams.get("resolution_minutes"));
        const starts_at = `${date}T09:00:00Z`,
            ends_at = `${date}T${resolution === 30 ? "09:30" : "10:00"}:00Z`;
        const observation =
            observations.find(
                (row) => row.starts_at === starts_at && row.ends_at === ends_at,
            ) ?? null;
        await route.fulfill({
            json: {
                timezone: region.timezone,
                available_start_date: "2026-01-01",
                available_end_date: "2026-01-07",
                start_date: date,
                end_date: date,
                days: [
                    {
                        date,
                        available_count: 1,
                        recorded_count: observation ? 1 : 0,
                        missing_count: observation ? 0 : 1,
                    },
                ],
                intervals: [
                    {
                        starts_at,
                        ends_at,
                        observation,
                        reference_available: true,
                        can_enter: !observation,
                        unavailable_reason: observation
                            ? "already_recorded"
                            : null,
                    },
                    {
                        starts_at: `${date}T11:00:00Z`,
                        ends_at: `${date}T12:00:00Z`,
                        observation: null,
                        can_enter: false,
                        unavailable_reason: "estimated_actuals_unavailable",
                    },
                    {
                        starts_at: `${date}T13:00:00Z`,
                        ends_at: `${date}T14:00:00Z`,
                        reference_available: true,
                        observation: null,
                        can_enter: true,
                        unavailable_reason: null,
                    },
                ],
                observations,
            },
        });
    });
    await openJournal(page);
    await page
        .getByRole("button", { name: "View details", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Calibration #40", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Request calibration" }),
    ).toHaveCount(0);
    await expect(page.getByLabel("Day", { exact: true })).not.toHaveAttribute(
        "min",
    );
    await expect(
        page.getByLabel("Production 2026-01-07T11:00:00Z"),
    ).toBeDisabled();
    await expect(
        page.getByText("No archived estimated actuals for this interval."),
    ).toBeVisible();
    await page.getByLabel("Production 2026-01-07T09:00:00Z").fill("250");
    await page.getByRole("button", { name: "Save meter readings" }).click();
    await expect(
        page.getByRole("cell", { name: "0.25 MWh", exact: true }),
    ).toBeVisible();
    expect(observations).toHaveLength(1);
    await expect(
        page.getByRole("cell", { name: "#41", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Calibration #40", exact: true }),
    ).toBeVisible();
    await page.getByLabel("Day", { exact: true }).fill("2026-01-06");
    await expect(
        page.getByLabel("Production 2026-01-06T09:00:00Z"),
    ).toHaveValue("");
    await page.getByLabel("Production 2026-01-06T09:00:00Z").fill("100");
    await page.getByLabel("Meter interval").selectOption("30");
    await expect(
        page.getByText("Meter entries \u00b7 30 minutes", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByLabel("Production 2026-01-06T09:00:00Z"),
    ).toHaveValue("");
    await page.getByLabel("Entry energy unit").selectOption("MWh");
    await page.getByLabel("Production 2026-01-06T09:00:00Z").fill("0.5");
    await page.getByRole("button", { name: "Save meter readings" }).click();
    await expect(
        page.getByRole("cell", { name: "0.5 MWh", exact: true }),
    ).toBeVisible();
    expect(observations[1]).toMatchObject({
        energy_mwh: 0.5,
        ends_at: "2026-01-06T09:30:00Z",
    });
    await page.getByLabel("Day", { exact: true }).fill("2026-01-07");
    await page.getByLabel("Meter interval").selectOption("60");
    await expect(
        page.getByRole("cell", { name: "0.25 MWh", exact: true }),
    ).toBeVisible();
});

test("production journal ignores stale dates and preserves new drafts during a save", async ({
    page,
}) => {
    await mockApi(page);
    let releaseDate!: () => void;
    const dateGate = new Promise<void>((resolve) => {
        releaseDate = resolve;
    });
    let releaseSave!: () => void;
    const saveGate = new Promise<void>((resolve) => {
        releaseSave = resolve;
    });
    let dateRequested!: () => void;
    const oldDateRequested = new Promise<void>((resolve) => {
        dateRequested = resolve;
    });
    await page.route("**/plants/21/production/**", async (route) => {
        const date =
            new URL(route.request().url()).searchParams.get("start_date") ??
            "2026-01-07";
        if (date === "2026-01-06") {
            dateRequested();
            await dateGate;
        }
        await route.fulfill({
            json: {
                timezone: region.timezone,
                available_start_date: "2026-01-01",
                available_end_date: "2026-01-07",
                start_date: date,
                end_date: date,
                days: [],
                intervals: [
                    {
                        starts_at: `${date}T09:00:00Z`,
                        ends_at: `${date}T10:00:00Z`,
                        observation: null,
                        can_enter: true,
                        reference_available: true,
                        unavailable_reason: null,
                    },
                ],
                observations: [],
            },
        });
    });
    await page.route("**/plants/21/observations/", async (route) => {
        expect(route.request().postDataJSON().observations).toEqual([
            {
                starts_at: "2026-01-07T09:00:00Z",
                ends_at: "2026-01-07T10:00:00Z",
                energy: 0.25,
                quality: "valid",
                source: "meter",
            },
        ]);
        await saveGate;
        await route.fulfill({ json: { created: 1, existing: 0, count: 1 } });
    });
    await openJournal(page);
    const entry = page.getByLabel("Production 2026-01-07T09:00:00Z");
    await entry.fill("250");
    await page.getByRole("button", { name: "Save meter readings" }).click();
    await expect(page.getByRole("button", { name: "Saving…" })).toBeDisabled();
    await entry.fill("300");
    releaseSave();
    await expect(
        page.getByText("1 meter reading saved.", { exact: true }),
    ).toBeVisible();
    await expect(entry).toHaveValue("300");
    await page.getByLabel("Day", { exact: true }).fill("2026-01-06");
    await oldDateRequested;
    await page.getByLabel("Day", { exact: true }).fill("2026-01-05");
    await expect(
        page.getByLabel("Production 2026-01-05T09:00:00Z"),
    ).toBeVisible();
    releaseDate();
    await expect(
        page.getByLabel("Production 2026-01-06T09:00:00Z"),
    ).toHaveCount(0);
    await expect(page.getByLabel("Day", { exact: true })).toHaveValue(
        "2026-01-05",
    );
});

test("production journal stops loading after a failed history request", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/plants/21/production/**", async (route) => {
        await route.fulfill({
            status: 503,
            json: { detail: "Production history is temporarily unavailable." },
        });
    });
    await openJournal(page);
    await expect(
        page.getByRole("alert").filter({
            hasText: "Production history is temporarily unavailable.",
        }),
    ).toBeVisible();
    await expect(
        page.getByText("Loading production history?", { exact: true }),
    ).toHaveCount(0);
});

for (const source of ["forecast", "estimated_actuals"] as const) {
    test(`new plant explains later configuration when projecting ${source}`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
        await mockApi(page);
        await page.route("**/plants/", async (route) => {
            await route.fulfill({
                json: paged([
                    {
                        ...plant,
                        created_at: "2026-11-10T10:00:00Z",
                        current_configuration: {
                            ...plant.current_configuration,
                            created_at: "2026-11-10T10:00:00Z",
                        },
                    },
                ]),
            });
        });
        await page.route("**/forecast/**", async (route) => {
            const electrical = new URL(route.request().url()).pathname.includes(
                "/plants/",
            );
            await route.fulfill({
                json: {
                    timezone: region.timezone,
                    freshness: "fresh",
                    source_kind: source,
                    configuration_id: electrical ? 31 : undefined,
                    calibration_id: null,
                    result: {
                        complete: true,
                        coverage_fraction: 1,
                        energy_kwh_m2: 4,
                        energy_mwh: 5,
                        intervals: [],
                        quality_flags: electrical
                            ? [
                                  "historical_configuration_assumed",
                                  "uncalibrated",
                              ]
                            : [],
                    },
                },
            });
        });
        await page.goto("/");
        await page.getByRole("button", { name: /Antalya solar park/ }).click();
        await expect(
            page.getByRole("heading", { name: "Regional irradiance" }),
        ).toBeVisible();
        await page
            .getByRole("button", { name: "Solar plants", exact: true })
            .click();
        await page.getByRole("button", { name: /East array/ }).click();
        await page.getByLabel("Data source").selectOption(source);
        await page
            .getByLabel("Forecast date")
            .fill(source === "forecast" ? "2026-11-10" : "2026-11-09");
        await expect(
            page.getByRole("heading", { name: "Electrical generation" }),
        ).toBeVisible();
        await expect(
            page.getByText(
                source === "forecast"
                    ? "Calculated from the archived forecast using the plant configuration entered later. No calibration is applied."
                    : "Calculated from estimated actuals using the plant configuration entered later. No calibration is applied.",
                { exact: true },
            ),
        ).toBeVisible();
        await expect(
            page.getByText("historical configuration assumed", { exact: true }),
        ).toBeVisible();
        await expect(
            page.getByText("uncalibrated", { exact: true }),
        ).toBeVisible();
        await expect(
            page.getByRole("heading", {
                name: "No configuration for this day",
            }),
        ).toHaveCount(0);
    });
}

for (const kind of ["region", "plant"] as const) {
    test(`recycle bin confirms deletion and restores a ${kind} with associated data`, async ({
        page,
    }) => {
        await mockApi(page);
        let deleted = false;
        const entity = kind === "region" ? region : plant;
        const entityPath = `/api/tenants/1/regions/11/${kind === "plant" ? "plants/21/" : ""}`;
        await page.route("**/api/tenants/1/**", async (route) => {
            const path = new URL(route.request().url()).pathname;
            if (path === entityPath && route.request().method() === "DELETE") {
                expect(route.request().headers()["x-csrftoken"]).toBe(
                    "signed-token",
                );
                deleted = true;
                await route.fulfill({ status: 204 });
            } else if (path === `${entityPath}restore/`) {
                expect(route.request().method()).toBe("POST");
                expect(route.request().headers()["x-csrftoken"]).toBe(
                    "signed-token",
                );
                expect(route.request().postDataJSON()).toEqual({});
                deleted = false;
                await route.fulfill({ json: entity });
            } else if (
                path === "/api/tenants/1/regions/" &&
                route.request().method() === "GET"
            ) {
                await route.fulfill({
                    json: paged(deleted && kind === "region" ? [] : [region]),
                });
            } else if (
                path === "/api/tenants/1/regions/11/plants/" &&
                route.request().method() === "GET"
            ) {
                await route.fulfill({
                    json: paged(deleted && kind === "plant" ? [] : [plant]),
                });
            } else if (path.includes("/trash/")) {
                const ownList = path.endsWith(`/trash/${kind}s/`);
                await route.fulfill({
                    json: paged(
                        deleted && ownList
                            ? [
                                  {
                                      id: entity.id,
                                      name: entity.name,
                                      deleted_at: "2026-11-10T12:00:00Z",
                                      purge_after: "2026-12-10T12:00:00Z",
                                      can_restore: true,
                                      ...(kind === "plant"
                                          ? {
                                                region_id: 11,
                                                region_name: region.name,
                                                parent_deleted: false,
                                            }
                                          : {}),
                                  },
                              ]
                            : [],
                    ),
                });
            } else await route.fallback();
        });
        await page.goto("/");
        await page.getByRole("button", { name: /Antalya solar park/ }).click();
        if (kind === "plant") {
            await page
                .getByRole("button", { name: "Solar plants", exact: true })
                .click();
            await page.getByRole("button", { name: /East array/ }).click();
        }
        await page
            .getByRole("button", { name: `Delete ${kind}`, exact: true })
            .click();
        const confirmation = page.getByRole("region", {
            name: `Delete ${kind} confirmation`,
        });
        await expect(confirmation).toContainText("30 days");
        await expect(confirmation).toContainText(
            "permanently deleted automatically",
        );
        await confirmation
            .getByRole("button", { name: "Cancel", exact: true })
            .click();
        expect(deleted).toBe(false);
        await page
            .getByRole("button", { name: `Delete ${kind}`, exact: true })
            .click();
        await page
            .getByRole("button", {
                name: `Move ${kind} to recycle bin`,
                exact: true,
            })
            .click();
        await expect(
            page.getByRole("button", { name: new RegExp(entity.name) }),
        ).toHaveCount(0);
        await page
            .getByRole("button", { name: "Recycle bin", exact: true })
            .click();
        await expect(
            page.getByRole("heading", { name: "Recycle bin", exact: true }),
        ).toBeVisible();
        const row = page.getByRole("row").filter({
            has: page.getByRole("rowheader", {
                name: entity.name,
                exact: true,
            }),
        });
        await expect(
            row.locator('time[datetime="2026-12-10T12:00:00Z"]'),
        ).toBeVisible();
        await row
            .getByRole("button", {
                name: `Restore ${entity.name}`,
                exact: true,
            })
            .click();
        await expect(
            page.getByText(`${entity.name} restored. Find it in Overview.`, {
                exact: true,
            }),
        ).toBeVisible();
        await expect(
            page.getByRole("button", {
                name: `Restore ${entity.name}`,
                exact: true,
            }),
        ).toHaveCount(0);
        await page
            .getByRole("button", { name: "Overview", exact: true })
            .click();
        await page.getByRole("button", { name: /Antalya solar park/ }).click();
        if (kind === "plant") {
            await page
                .getByRole("button", { name: "Solar plants", exact: true })
                .click();
            await expect(
                page.getByRole("button", { name: /East array/ }),
            ).toBeVisible();
        } else
            await expect(
                page.getByRole("heading", { name: region.name, exact: true }),
            ).toBeVisible();
    });
}

test("recycle bin disables expired records and asks to restore a parent region first", async ({
    page,
}) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockApi(page);
    await page.route("**/trash/regions/", async (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 91,
                    name: "Expired region",
                    deleted_at: "2026-01-01T00:00:00Z",
                    purge_after: "2026-01-31T00:00:00Z",
                    can_restore: false,
                },
            ]),
        }),
    );
    await page.route("**/trash/plants/", async (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 92,
                    name: "Plant in deleted region",
                    deleted_at: "2026-11-10T00:00:00Z",
                    purge_after: "2026-12-10T00:00:00Z",
                    can_restore: false,
                    region_id: 11,
                    region_name: region.name,
                    parent_deleted: true,
                },
            ]),
        }),
    );
    await page.goto("/");
    await page
        .getByRole("button", { name: "Recycle bin", exact: true })
        .click();
    await expect(
        page.getByRole("button", {
            name: "Restore Expired region",
            exact: true,
        }),
    ).toBeDisabled();
    await expect(
        page.getByText("The 30-day restore period has ended.", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", {
            name: "Restore Plant in deleted region",
            exact: true,
        }),
    ).toBeDisabled();
    await expect(
        page.getByText("Restore its region first.", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: /permanently delete|purge/i }),
    ).toHaveCount(0);
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    const scan = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze();
    expect(scan.violations).toEqual([]);
});

test("recycle bin ignores a prior workspace response and restores only in the selected workspace", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/", async (route) =>
        route.fulfill({
            json: paged([
                { id: 1, name: "Sunfield Energy" },
                { id: 2, name: "Second workspace" },
            ]),
        }),
    );
    let releaseFirst!: () => void;
    const firstGate = new Promise<void>((resolve) => {
        releaseFirst = resolve;
    });
    let requestedFirst!: () => void;
    const firstRequest = new Promise<void>((resolve) => {
        requestedFirst = resolve;
    });
    await page.route("**/trash/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith("/plants/")) {
            await route.fulfill({ json: paged([]) });
            return;
        }
        const first = path.includes("/tenants/1/");
        if (first) {
            requestedFirst();
            await firstGate;
        }
        await route.fulfill({
            json: paged([
                {
                    id: first ? 101 : 102,
                    name: first
                        ? "First workspace deleted region"
                        : "Second workspace deleted region",
                    deleted_at: "2026-11-10T00:00:00Z",
                    purge_after: "2026-12-10T00:00:00Z",
                    can_restore: true,
                },
            ]),
        });
    });
    await page.route("**/regions/102/restore/", async (route) => {
        expect(new URL(route.request().url()).pathname).toBe(
            "/api/tenants/2/regions/102/restore/",
        );
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        await route.fulfill({ json: { ...region, id: 102 } });
    });
    await page.goto("/");
    await page
        .getByRole("button", { name: "Recycle bin", exact: true })
        .click();
    await firstRequest;
    await page.getByRole("combobox", { name: /Workspace/ }).selectOption("2");
    await expect(
        page.getByRole("button", {
            name: "Restore Second workspace deleted region",
            exact: true,
        }),
    ).toBeVisible();
    releaseFirst();
    await expect(
        page.getByRole("button", {
            name: "Restore First workspace deleted region",
            exact: true,
        }),
    ).toHaveCount(0);
    await page
        .getByRole("button", {
            name: "Restore Second workspace deleted region",
            exact: true,
        })
        .click();
    await expect(
        page.getByText(
            "Second workspace deleted region restored. Find it in Overview.",
            { exact: true },
        ),
    ).toBeVisible();
});
