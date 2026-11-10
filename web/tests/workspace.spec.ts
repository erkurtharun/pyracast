import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { parseProduction } from "../src/lib/production";
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
        } else if (path.endsWith("/calibrations/"))
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
    await page.getByText("View interval data (2 intervals)").click();
    await expect(
        page.getByRole("cell", { name: "500", exact: true }),
    ).toBeVisible();
    const scan = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze();
    expect(scan.violations).toEqual([]);
});
