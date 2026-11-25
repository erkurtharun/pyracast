import type { Request } from "@playwright/test";
import { instant, test, expect, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { parseProduction } from "../src/lib/production";
import { csvTexts } from "./support/production-csv";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";
import {
    answer,
    region,
    plant,
    user,
    paged,
    mockApi,
    sessionIdentity,
    tenantRow,
} from "./support/workspace";
import { errorBody } from "./support/errors";

function productionCsv(energy: number | string, source = "meter") {
    const start = instant.getTime() - 86_400_000;
    const stamp = (at: number) => new Date(at).toISOString();
    return `starts_at,ends_at,energy_kwh,quality,source\n${stamp(start)},${stamp(start + 1_800_000)},${energy},valid,${source}`;
}

test("mock contract: signs in with CSRF, selects region, and signs out", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.goto("/app/");
    await page.getByLabel("Email", { exact: true }).fill(user.email);
    await page.getByLabel("Password", { exact: true }).fill("fixture-password");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "No forecast data for this day." }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(
        page.locator("form").getByRole("button", { name: "Sign in" }),
    ).toBeVisible();
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
        await route.fulfill({
            json: { ...sessionIdentity(null), csrf_token: "one-token" },
        });
    });
    await page.goto("/app/", { waitUntil: "domcontentloaded" });
    await observed;
    await page.evaluate(() => {
        window.dispatchEvent(new Event("focus"));
        document.dispatchEvent(new Event("visibilitychange"));
    });
    release();
    await expect(
        page.locator("form").getByRole("button", { name: "Sign in" }),
    ).toBeVisible();
    expect(requests).toBe(1);
});
test("one plant request saves location and technical details before approval", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    let writes = 0;
    await page.route("**/api/tenants/1/sites/", async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        writes++;
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        expect(route.request().postDataJSON()).toMatchObject({
            name: "New solar park",
            latitude: 36.5,
            longitude: 30.2,
            timezone: "Europe/Berlin",
            dc_capacity_mw: 130,
            inverter_capacity_mw: 100,
            export_limit_mw: null,
            mounting_type: "fixed",
            surface_tilt_degrees: 0,
            surface_azimuth_degrees: 180,
        });
        return route.fallback();
    });
    await page.clock.setFixedTime(new Date("2026-03-28T22:30:00Z"));
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add a plant" }).click();
    await page.getByLabel("Plant name", { exact: true }).fill("New solar park");
    await page.getByLabel("Latitude").fill("36.5");
    await page.getByLabel("Longitude").fill("30.2");
    await page.getByLabel("Time zone").selectOption("Europe/Berlin");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("Panel capacity (MWp)").fill("130");
    await page.getByLabel("Inverter capacity (MW AC, optional)").fill("100");
    await page.getByLabel("Mounting").selectOption("fixed");
    await page.getByLabel("Panel tilt (°, optional)").fill("0");
    await page.getByLabel("Panel azimuth (°, optional)").fill("180");
    await expect(
        page.getByLabel("Panel azimuth (°, optional)"),
    ).toHaveAccessibleDescription("Clockwise from north.");
    await expect(page.getByText("Capacity and mounting guide")).toHaveCount(0);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    expect(writes).toBe(0);
    await expect(
        page.getByRole("heading", { name: "Check the details" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await expect(page.getByLabel("Panel capacity (MWp)")).toHaveValue("130");
    await expect(page.getByLabel("Panel tilt (°, optional)")).toHaveValue("0");
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await expect(page.getByLabel("Plant name", { exact: true })).toHaveValue(
        "New solar park",
    );
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByRole("button", { name: "Send for approval" }).click();
    await expect(
        page.getByRole("heading", { name: "New solar park" }),
    ).toBeVisible();
    await expect(
        page.getByText(/Waiting for approval by Pyracast/),
    ).toBeVisible();
    await expect(
        page.getByText(
            "You can still change the technical details and the location.",
        ),
    ).toBeVisible();
    await expect(page.getByLabel("Forecast date")).toHaveCount(0);
    for (const removed of [
        "Next steps",
        "Pyracast checks the plant details and connects its data source.",
        "After approval, forecasts appear here and are collected on schedule.",
    ])
        await expect(page.getByText(removed)).toHaveCount(0);
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page.getByLabel("Panel capacity (MWp)")).toHaveValue("130");
    await expect(page.getByText("Capacity and mounting guide")).toHaveCount(0);
    expect(writes).toBe(1);
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
            surface_tilt_degrees: 33,
            surface_azimuth_degrees: 180,
        },
    };
    const bodies: Record<string, unknown>[] = [];
    await page.route("**/plants/21/", async (route) => {
        if (route.request().method() !== "PUT") return route.fallback();
        const body = route.request().postDataJSON();
        bodies.push(body);
        const { name, expected_revision, ...parameters } = body;
        updated = {
            ...updated,
            name,
            current_configuration: {
                ...updated.current_configuration,
                ...parameters,
                id: 31 + bodies.length,
            },
            parameters: { ...updated.parameters, ...parameters },
            parameters_revision: expected_revision + 1,
        };
        await route.fulfill({ json: updated });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Import CSV" }).click();
    await page.getByLabel("CSV file", { exact: true }).setInputFiles({
        name: "production.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(productionCsv(250)),
    });
    await expect(page.getByText(/1 row read/)).toBeVisible();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByLabel("Mounting").selectOption("fixed");
    await page.getByLabel("Panel tilt (°, optional)").fill("0");
    await page.getByLabel("Panel azimuth (°, optional)").fill("360");
    await page.getByRole("button", { name: "Save technical details" }).click();
    expect(bodies).toHaveLength(0);
    await page.getByLabel("Panel azimuth (°, optional)").fill("180");
    await page.getByRole("button", { name: "Save technical details" }).click();
    await expect.poll(() => bodies.length).toBe(1);
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await expect(page.getByText(/1 row read/)).toHaveCount(0);
    expect(bodies[0]).toMatchObject({
        mounting_type: "fixed",
        surface_tilt_degrees: 0,
        surface_azimuth_degrees: 180,
        dc_capacity_mw: 5,
        inverter_capacity_mw: 4,
        export_limit_mw: null,
    });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page.getByLabel("Panel tilt (°, optional)")).toHaveValue("0");
    await page.getByLabel("Mounting").selectOption("tracking");
    await page.getByRole("button", { name: "Save technical details" }).click();
    await expect.poll(() => bodies.length).toBe(2);
    expect(bodies[1]).toMatchObject({
        mounting_type: "tracking",
        surface_tilt_degrees: null,
        surface_azimuth_degrees: null,
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
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await expect(
        page.getByRole("heading", {
            name: "No technical details for this day.",
        }),
    ).toBeVisible();
    await expect(
        page.getByText("Choose a later date.", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("configuration_unavailable")).toHaveCount(0);
});

test("mock contract: imports validated production with explicit energy units", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Import CSV" }).click();
    await page.getByLabel("CSV file", { exact: true }).setInputFiles({
        name: "production.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(productionCsv(250)),
    });
    await expect(page.getByText(/1 row read/)).toBeVisible();
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    await page
        .getByRole("button", { name: "Import 1 value", exact: true })
        .click();
    await expect(
        page.getByText("Imported. New values: 1 · Already saved: 0"),
    ).toBeVisible();
});

test("CSV selection replaces validated rows and recovers after an invalid file", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Import CSV" }).click();
    const input = page.getByLabel("CSV file", { exact: true });
    const select = (name: string, energy: string) =>
        input.setInputFiles({
            name,
            mimeType: "text/csv",
            buffer: Buffer.from(productionCsv(energy)),
        });
    await select("first.csv", "250");
    await expect(page.getByText(/first\.csv · 1 row read/)).toBeVisible();
    await select("invalid.csv", "-1");
    await expect(page.getByText(/rows? read/)).toHaveCount(0);
    await expect(
        page.getByRole("alert").filter({
            hasText: "Row 2: write the energy as zero or a positive number.",
        }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    await expect(
        page.getByText("Choose a CSV file first.", { exact: true }),
    ).toBeVisible();
    await select("replacement.csv", "777");
    await expect(page.getByText(/replacement\.csv · 1 row read/)).toBeVisible();
    await expect(
        page.getByText("Row 2: write the energy as zero or a positive number."),
    ).toHaveCount(0);
    const imported = page.waitForRequest(
        (request) =>
            request.method() === "POST" &&
            request.url().includes("/production/changes/"),
    );
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    await page
        .getByRole("button", { name: "Import 1 value", exact: true })
        .click();
    expect((await imported).postDataJSON()).toMatchObject({
        configuration_id: 31,
        unit: "kWh",
        changes: [{ energy: 777, source: "meter" }],
    });
});

for (const staleRead of ["resolve", "reject"] as const) {
    for (const selection of ["new file", "clear"] as const) {
        test(`CSV ignores an old ${staleRead} after ${selection}`, async ({
            page,
        }) => {
            await page.addInitScript(
                ({ staleRead }) => {
                    const read = (file: File) => new Response(file).text();
                    File.prototype.text = function () {
                        if (this.name !== "old.csv") return read(this);
                        return new Promise<string>((resolve, reject) => {
                            Object.assign(window, {
                                releaseOldCsv: async () => {
                                    if (staleRead === "reject")
                                        reject(
                                            new Error("Old file read failed."),
                                        );
                                    else resolve(await read(this));
                                },
                            });
                        });
                    };
                },
                { staleRead },
            );
            await mockApi(page);
            await page.goto("/app/");
            await page
                .getByRole("button", { name: /East array|Antalya solar park/ })
                .click();
            await page
                .getByRole("button", { name: "Production", exact: true })
                .click();
            await page.getByRole("button", { name: "Import CSV" }).click();
            const csv = (energy: number, source: string) =>
                Buffer.from(productionCsv(energy, source));
            const input = page.getByLabel("CSV file", { exact: true });
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
                    page.getByText(/new\.csv · 1 row read/),
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
                await expect(page.getByText(/rows? read/)).toHaveCount(0);
                await expect(
                    page.getByRole("button", {
                        name: "Preview",
                        exact: true,
                    }),
                ).toBeVisible();
                return;
            }
            const imported = page.waitForRequest(
                (request) =>
                    request.method() === "POST" &&
                    request.url().includes("/production/changes/"),
            );
            await page
                .getByRole("button", { name: "Preview", exact: true })
                .click();
            await page
                .getByRole("button", { name: "Import 1 value", exact: true })
                .click();
            expect((await imported).postDataJSON()).toMatchObject({
                changes: [{ energy: 777, source: "new-meter" }],
            });
        });
    }
}
test("open regional and plant forecasts refresh after new data arrives", async ({
    page,
}) => {
    await mockApi(page);
    let reason = "forecast_unavailable";
    await page.route("**/api/**/forecast/**", async (route) => {
        await route.fulfill({
            json: {
                timezone: region.timezone,
                freshness: "missing",
                result: null,
                unavailable_reason: reason,
            },
        });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    const irradiance = page.getByRole("button", {
        name: "Irradiance forecast",
        exact: true,
    });
    await irradiance.click();
    await expect(irradiance).toHaveAttribute("aria-current", "page");
    await expect(
        page.getByRole("heading", { name: "No forecast data for this day." }),
    ).toBeVisible();
    reason = "estimated_actuals_unavailable";
    await page.clock.fastForward(30_000);
    await expect(
        page.getByRole("heading", {
            name: "No satellite data for this day.",
        }),
    ).toBeVisible();
    // The Generation view mounts with its cached answer and revalidates it; the tick below
    // must come after that read has been applied, or the poll finds it still in flight.
    reason = "configuration_unavailable";
    const generation = page.getByRole("button", {
        name: "Generation forecast",
        exact: true,
    });
    await generation.click();
    await expect(generation).toHaveAttribute("aria-current", "page");
    await expect(
        page.getByRole("heading", {
            name: "No technical details for this day.",
        }),
    ).toBeVisible();
    reason = "estimated_actuals_unavailable";
    await page.clock.fastForward(30_000);
    await expect(
        page.getByRole("heading", {
            name: "No satellite data for this day.",
        }),
    ).toBeVisible();
});

test("a date picked while a forecast view switch loads keeps the chosen view", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-03-28T12:00:00Z"));
    await mockApi(page);
    let holding = false;
    let release = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route(
        (url) =>
            url.searchParams.has("_rsc") && url.pathname.endsWith("/forecast/"),
        async (route) => {
            if (holding) {
                holding = false;
                await held;
            }
            await route.fallback();
        },
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    const irradiance = page.getByRole("button", {
        name: "Irradiance forecast",
        exact: true,
    });
    await irradiance.click();
    await expect(irradiance).toHaveAttribute("aria-current", "page");
    holding = true;
    const generation = page.getByRole("button", {
        name: "Generation forecast",
        exact: true,
    });
    await generation.click();
    await page.getByLabel("Forecast date").fill("2026-03-27");
    release();
    await expect(page).toHaveURL(/\/forecast\/\?date=2026-03-27$/);
    await expect(generation).toHaveAttribute("aria-current", "page");
    await expect(page.getByLabel("Interval", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-03-27");
});

async function holdNavigation(page: Page, section: string) {
    let seen!: () => void;
    let settle!: () => void;
    let end!: () => void;
    const requested = new Promise<void>((resolve) => (seen = resolve));
    const settled = new Promise<void>((resolve) => (settle = resolve));
    const ended = new Promise<void>((resolve) => (end = resolve));
    let held = false;
    await page.route(
        (url) =>
            url.searchParams.has("_rsc") &&
            url.pathname.endsWith(`/${section}/`),
        async (route) => {
            if (held) return route.fallback();
            held = true;
            const request = route.request();
            const finish = (done: Request) => {
                if (done !== request) return;
                page.off("requestfinished", finish);
                end();
            };
            page.on("requestfinished", finish);
            seen();
            await settled;
            return route.fallback();
        },
    );
    // The held response has finished and the plant screen has no navigation pending.
    const release = async () => {
        settle();
        await ended;
        await expect(
            page.getByRole("navigation", { name: "Plant sections" }),
        ).not.toHaveAttribute("aria-busy", "true");
    };
    return { requested, release };
}

async function openPlant(page: Page, address: string) {
    await page.clock.setFixedTime(new Date("2026-03-28T12:00:00Z"));
    await mockApi(page);
    await page.goto(`/app/w/1/plants/11/${address}`);
    return (name: string) => page.getByRole("button", { name, exact: true });
}

test("the displayed section clicked while another section loads keeps its date and view", async ({
    page,
}) => {
    const button = await openPlant(
        page,
        "forecast/?date=2026-03-27&view=irradiance",
    );
    await expect(button("Irradiance forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
    const production = await holdNavigation(page, "production");
    await button("Production").click();
    await production.requested;
    await button("Forecast").click();
    await page.getByLabel("Forecast date").fill("2026-03-26");
    await expect(page).toHaveURL(
        /\/forecast\/\?date=2026-03-26&view=irradiance$/,
    );
    await production.release();
    await expect(page).toHaveURL(
        /\/forecast\/\?date=2026-03-26&view=irradiance$/,
    );
    await expect(button("Irradiance forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-03-26");
    // No product event marks a discarded answer as handled, so a further change probes the base after it.
    await page.getByLabel("Forecast date").fill("2026-03-25");
    await expect(page).toHaveURL(
        /\/forecast\/\?date=2026-03-25&view=irradiance$/,
    );
    await expect(button("Irradiance forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
});

test("a forecast view chosen while another section loads applies to the forecast", async ({
    page,
}) => {
    const button = await openPlant(
        page,
        "forecast/?date=2026-03-27&view=irradiance",
    );
    await expect(button("Irradiance forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
    const production = await holdNavigation(page, "production");
    await button("Production").click();
    await production.requested;
    await button("Generation forecast").click();
    await expect(page).toHaveURL(/\/forecast\/\?date=2026-03-27$/);
    await production.release();
    await expect(page).toHaveURL(/\/forecast\/\?date=2026-03-27$/);
    await expect(button("Generation forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
    await page.getByLabel("Forecast date").fill("2026-03-26");
    await expect(page).toHaveURL(/\/forecast\/\?date=2026-03-26$/);
    await expect(button("Generation forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
});

for (const source of ["estimated_actuals", "archived_forecast"] as const)
    for (const picked of ["2026-03-29", ""] as const)
        test(`${source} chosen while ${picked ? "a later date" : "a cleared date"} loads keeps the date being navigated to`, async ({
            page,
        }) => {
            const reads: URL[] = [];
            page.on("request", (request) => {
                const url = new URL(request.url());
                if (url.pathname.endsWith("/plants/21/forecast/"))
                    reads.push(url);
            });
            await openPlant(page, "forecast/?date=2026-03-27");
            const day = page.getByLabel("Forecast date");
            const choice = page.getByLabel("Data", { exact: true });
            await expect(day).toHaveValue("2026-03-27");
            const forecast = await holdNavigation(page, "forecast");
            await day.fill(picked);
            await forecast.requested;
            await choice.selectOption(source);
            await forecast.release();
            await expect(page).toHaveURL(
                picked ? /\/forecast\/\?date=2026-03-29$/ : /\/forecast\/$/,
            );
            await expect(day).toHaveValue("2026-03-29");
            await expect(day).toHaveAttribute("max", "2026-03-28");
            await expect(choice).toHaveValue(source);
            await expect(
                page.getByText(
                    "Choose a date within 2025-03-27 – 2026-03-28.",
                    { exact: true },
                ),
            ).toBeVisible();
            expect(
                reads.filter(
                    (url) =>
                        url.searchParams.get("data_source") === source &&
                        (url.searchParams.get("date") ?? "") > "2026-03-28",
                ),
            ).toEqual([]);
            await day.fill("2026-03-26");
            await expect(page).toHaveURL(/\/forecast\/\?date=2026-03-26$/);
            await expect(choice).toHaveValue(source);
        });

test("a second day step taken while the first loads continues from the day being navigated to", async ({
    page,
}) => {
    const button = await openPlant(page, "production/?date=2026-03-25");
    const day = page.getByLabel("Day", { exact: true });
    await expect(day).toHaveValue("2026-03-25");
    const production = await holdNavigation(page, "production");
    await button("Previous day").click();
    await production.requested;
    await button("Previous day").click();
    await expect(page).toHaveURL(/\/production\/\?date=2026-03-23$/);
    await production.release();
    await expect(page).toHaveURL(/\/production\/\?date=2026-03-23$/);
    await expect(day).toHaveValue("2026-03-23");
    await button("Next day").click();
    await expect(page).toHaveURL(/\/production\/\?date=2026-03-24$/);
    await expect(day).toHaveValue("2026-03-24");
});

test("Back while section changes load leaves the restored section in charge of its controls", async ({
    page,
}) => {
    const button = await openPlant(page, "forecast/");
    await button("Production").click();
    await expect(page).toHaveURL(/\/production\/$/);
    await button("Forecast").click();
    await expect(page).toHaveURL(/\/forecast\/$/);
    const production = await holdNavigation(page, "production");
    const settings = await holdNavigation(page, "settings");
    await button("Production").click();
    await production.requested;
    await button("Settings").click();
    await settings.requested;
    await page.goBack();
    await expect(page).toHaveURL(/\/production\/$/);
    await expect(
        page.getByRole("combobox", { name: "Workspace", exact: true }),
    ).toHaveValue("1");
    await expect(page.getByRole("main")).not.toBeEmpty();
    await production.release();
    await settings.release();
    await expect(page).toHaveURL(/\/production\/$/);
    await expect(button("Production")).toHaveAttribute("aria-current", "page");
    await button("Comparison").click();
    await expect(page).toHaveURL(/\/production\/\?view=comparison$/);
    await expect(button("Comparison")).toHaveAttribute("aria-current", "page");
    await button("Calibration").click();
    await expect(page).toHaveURL(/\/calibration\/$/);
    await expect(button("Calibration")).toHaveAttribute("aria-current", "page");
});

test("a superseded section change that fails loads its own section in full", async ({
    page,
}) => {
    const button = await openPlant(page, "forecast/?view=irradiance");
    await expect(button("Irradiance forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
    let seen!: () => void;
    let fail!: () => void;
    const requested = new Promise<void>((resolve) => (seen = resolve));
    const failing = new Promise<void>((resolve) => (fail = resolve));
    await page.route(
        (url) =>
            url.searchParams.has("_rsc") &&
            url.pathname.endsWith("/production/"),
        async (route) => {
            seen();
            await failing;
            await route.fulfill({ status: 503, body: "unavailable" });
        },
        { times: 1 },
    );
    await button("Production").click();
    await requested;
    await button("Generation forecast").click();
    await expect(page).toHaveURL(/\/forecast\/$/);
    await expect(button("Generation forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
    fail();
    // Next.js answers a failed navigation request with a document load of its target,
    // also after a later navigation committed.
    await expect(page).toHaveURL(/\/production\/$/);
    await expect(button("Production")).toHaveAttribute("aria-current", "page");
    await expect(page.getByLabel("Day", { exact: true })).toBeVisible();
});

for (const overtaken of ["a section change", "Back"] as const)
    test(`a view switch overtaken by ${overtaken} is not the base for the next change`, async ({
        page,
    }) => {
        const button = await openPlant(page, "forecast/");
        if (overtaken === "Back") {
            await button("Production").click();
            await expect(page).toHaveURL(/\/production\/$/);
            await button("Forecast").click();
            await expect(page).toHaveURL(/\/forecast\/$/);
        }
        const forecast = await holdNavigation(page, "forecast");
        await button("Irradiance forecast").click();
        await forecast.requested;
        if (overtaken === "Back") await page.goBack();
        else await button("Production").click();
        await expect(page).toHaveURL(/\/production\/$/);
        await forecast.release();
        await expect(page).toHaveURL(/\/production\/$/);
        // History moves to the forecast without a click, which would replace a stuck pending view.
        if (overtaken === "Back") await page.goForward();
        else await page.goBack();
        await expect(page).toHaveURL(/\/forecast\/$/);
        await expect(button("Generation forecast")).toHaveAttribute(
            "aria-current",
            "page",
        );
        await page.getByLabel("Forecast date").fill("2026-03-26");
        await expect(page).toHaveURL(/\/forecast\/\?date=2026-03-26$/);
        await expect(button("Generation forecast")).toHaveAttribute(
            "aria-current",
            "page",
        );
    });

test("generation-only controls rest while the irradiance view loads", async ({
    page,
}) => {
    const button = await openPlant(page, "forecast/?date=2026-03-29");
    await expect(button("Generation forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
    const controls = page.locator(".forecast-controls");
    await expect(page.getByLabel("Interval", { exact: true })).toBeEnabled();
    const forecast = await holdNavigation(page, "forecast");
    await button("Irradiance forecast").click();
    await forecast.requested;
    await expect(page.getByLabel("Interval", { exact: true })).toBeDisabled();
    await expect(controls).toHaveAttribute("aria-busy", "true");
    await expect(page.getByLabel("Forecast date")).toBeEnabled();
    await forecast.release();
    await expect(page).toHaveURL(
        /\/forecast\/\?date=2026-03-29&view=irradiance$/,
    );
    await expect(button("Irradiance forecast")).toHaveAttribute(
        "aria-current",
        "page",
    );
    await expect(controls).not.toHaveAttribute("aria-busy", "true");
    await expect(page.getByLabel("Interval", { exact: true })).toHaveCount(0);
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-03-29");
    await expect(page.getByLabel("Data", { exact: true })).toHaveValue(
        "forecast",
    );
});

test("the outgoing production view's disclosures stay closed while the comparison loads", async ({
    page,
}) => {
    const button = await openPlant(page, "production/");
    const importer = page.locator("#production-import");
    // A pointer click at the disclosure's position, as a person makes it.
    const toggle = async () => {
        const summary = importer.locator(":scope > summary");
        await summary.scrollIntoViewIfNeeded();
        const box = await summary.boundingBox();
        await page.mouse.click(
            box!.x + box!.width / 2,
            box!.y + box!.height / 2,
        );
    };
    await toggle();
    await expect(importer).toHaveAttribute("open", "");
    await toggle();
    await expect(importer).not.toHaveAttribute("open", /.*/);
    const comparison = await holdNavigation(page, "production");
    await button("Comparison").click();
    await comparison.requested;
    await toggle();
    await expect(importer).not.toHaveAttribute("open", /.*/);
    await expect(page.getByLabel("CSV file", { exact: true })).toBeHidden();
    await comparison.release();
    await expect(page).toHaveURL(/\/production\/\?view=comparison$/);
    await expect(button("Comparison")).toHaveAttribute("aria-current", "page");
    await expect(importer).toHaveCount(0);
});

test("region collection schedule validates and saves distinct local times", async ({
    page,
}) => {
    await mockApi(page);
    let saves = 0;
    let reads = 0;
    let schedule = {
        enabled: true,
        times: ["09:00"],
        timezone: region.timezone,
        last_success_at: null as string | null,
        next_collection_at: "2026-01-01T07:00:00Z",
        state: "collecting",
        inactive_reason: null,
        requests_today: 0,
        history_requests_reserved_per_day: 1,
        history_local_time: "03:00",
        history_retry_until_local: "09:00",
        history_retry_minutes: 30,
        max_forecast_times: 9,
        effective_from: "2026-01-01T00:00:00Z",
        recent_jobs: [
            {
                id: 1,
                endpoint: "forecasts",
                status: "failed",
                scheduled_for: "2026-01-01T07:00:00Z",
                available_at: "2026-01-01T07:00:00Z",
                attempts: 1,
                last_error: "provider_failure",
            },
            {
                id: 2,
                endpoint: "estimated_actuals",
                status: "succeeded",
                scheduled_for: "2026-01-01T07:00:00Z",
                available_at: "2026-01-01T07:00:00Z",
                attempts: 2,
                last_error: "quota_exhausted",
            },
            {
                id: 3,
                endpoint: "estimated_actuals",
                status: "succeeded",
                scheduled_for: "2026-01-01T00:00:00Z",
                available_at: "2026-01-01T00:00:00Z",
                attempts: 1,
                last_error: "history_incomplete",
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
        } else reads++;
        await route.fulfill({ json: schedule });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Forecast update times" }).click();
    await expect(
        page.getByText(
            "Production calculated from satellite data is updated daily at 03:00. Turning off automatic updates stops it too.",
        ),
    ).toBeVisible();
    await expect(page.getByText(/in plant time/)).toHaveCount(0);
    expect(reads).toBe(1);
    await page.getByText("Update details", { exact: true }).click();
    await expect(
        page.getByText(
            /Retries for production calculated from satellite data keep enough requests/,
        ),
    ).toBeVisible();
    await expect(
        page.getByText("Last update", { exact: true }).locator(".."),
    ).toContainText("Not yet");
    await expect(
        page.getByText("Next planned update").locator(".."),
    ).toContainText("Jan 1, 2026, 10:00 AM");
    await page.getByText("Recent update jobs", { exact: true }).click();
    await expect(page.getByText(/Provider request failed/)).toBeVisible();
    await expect(
        page.getByText(/Historical estimates · Succeeded/),
    ).toBeVisible();
    await expect(
        page.getByText(/Historical estimates · Partly updated/),
    ).toBeVisible();
    await expect(page.getByText(/Daily account budget exhausted/)).toHaveCount(
        0,
    );
    for (const accountDetail of [
        "Requests used",
        "Usage details",
        "Tracked attempts:",
        "remaining requests are not guaranteed",
    ]) {
        await expect(page.getByText(accountDetail)).toHaveCount(0);
    }
    await page.getByRole("button", { name: "Save" }).click();
    expect(saves).toBe(0);
    await page.getByRole("button", { name: "Add time" }).click();
    await page.getByRole("button", { name: "Save" }).click();
    await expect(
        page.getByText("Enter a valid time in every row."),
    ).toBeVisible();
    await page.getByLabel("Update time 2", { exact: true }).fill("09:00");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(
        page.getByText("Choose a different time for each update."),
    ).toBeVisible();
    await page.getByLabel("Update time 2", { exact: true }).fill("11:00");
    schedule.last_success_at = "2026-01-01T06:00:00Z";
    schedule.effective_from = "2026-01-01T00:00:30Z";
    const refreshed = page.waitForResponse(
        (response) =>
            response.url().includes("/collection-schedule/") &&
            response.request().method() === "GET",
    );
    await page.clock.fastForward(30_000);
    await refreshed;
    await expect(page.getByLabel("Update time 2", { exact: true })).toHaveValue(
        "11:00",
    );
    await expect(
        page.getByText("Last update", { exact: true }).locator(".."),
    ).toContainText("Jan 1, 2026, 9:00 AM");
    expect(reads).toBe(2);
    await page.getByRole("button", { name: "Add time" }).click();
    await page.getByLabel("Update time 3", { exact: true }).fill("10:00");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByLabel("Update time 2", { exact: true })).toHaveValue(
        "10:00",
    );
    await expect(page.getByLabel("Update time 3", { exact: true })).toHaveValue(
        "11:00",
    );
    expect(saves).toBe(1);
    await page.getByRole("button", { name: "Remove update time 3" }).click();
    await expect(page.getByLabel("Update time 3", { exact: true })).toHaveCount(
        0,
    );
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Forecast email" }).click();
    await expect(
        page.getByText("At most 10 a day.", { exact: true }),
    ).toBeVisible();
});

test("on a past day the data choice names satellite data only once satellite figures are shown", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-03-28T12:00:00Z"));
    await mockApi(page);
    let answer: "failure" | "empty" | "satellite" = "failure";
    await page.route("**/plants/21/forecast/**", (route) => {
        if (answer === "failure")
            return route.fulfill({
                status: 503,
                json: errorBody("busy", "Forecast service is busy."),
            });
        return route.fulfill({
            json: {
                date: "2026-03-27",
                timezone: region.timezone,
                freshness: "archived",
                source_kind: "estimated_actuals",
                ...(answer === "empty"
                    ? {
                          result: null,
                          unavailable_reason: "estimated_actuals_unavailable",
                      }
                    : {
                          unavailable_reason: null,
                          result: {
                              complete: true,
                              energy_mwh: 7,
                              coverage_fraction: 1,
                              intervals: [],
                          },
                      }),
            },
        });
    });
    await page.goto("/app/w/1/plants/11/forecast/?date=2026-03-27");
    const choice = page.getByLabel("Data", { exact: true });
    const panel = page.locator("section.panel");
    await expect(panel.getByRole("alert")).toContainText(
        "Forecast service is busy.",
    );
    await expect(choice).toHaveValue("forecast");
    answer = "empty";
    await panel.getByRole("button", { name: "Try again" }).click();
    await expect(
        panel.getByRole("heading", { name: "No satellite data for this day." }),
    ).toBeVisible();
    await expect(choice).toHaveValue("forecast");
    answer = "satellite";
    await page.clock.fastForward(30_000);
    await expect(
        panel.getByRole("heading", {
            name: "Production calculated from satellite data",
        }),
    ).toBeVisible();
    await expect(choice).toHaveValue("estimated_actuals");
});

test("the data choice names what each view shows and keeps the date", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-03-28T12:00:00Z"));
    await mockApi(page);
    const sources: string[] = [];
    const plantRequests: URL[] = [];
    const generation = page.locator(".stat", { hasText: "Daily total" });
    const choice = page.getByLabel("Data", { exact: true });
    const day = page.getByLabel("Forecast date");
    const heading = page.locator("section.panel h2");
    await page.route("**/api/**/forecast/**", async (route) => {
        const url = new URL(route.request().url());
        const source = url.searchParams.get("data_source") ?? "missing";
        sources.push(source);
        if (url.pathname.includes("/plants/")) plantRequests.push(url);
        const date = url.searchParams.get("date")!;
        const number = Number(date.slice(-2));
        await route.fulfill({
            json: {
                timezone: region.timezone,
                freshness: "fresh",
                source_kind:
                    source === "archived_forecast" || date >= "2026-03-28"
                        ? "forecast"
                        : "estimated_actuals",
                result: {
                    complete: true,
                    intervals: [],
                    energy_kwh_m2: 4,
                    energy_mwh: 100 + number + number / 100,
                },
            },
        });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await expect(choice.locator("option")).toHaveText([
        "Forecast",
        "Last forecast before sunrise",
        "Irradiance calculated from satellite data",
    ]);
    await day.fill("2026-03-27");
    await expect(heading).toHaveText(
        "Irradiance calculated from satellite data",
    );
    await expect(choice).toHaveValue("estimated_actuals");
    expect(sources.at(-1)).toBe("auto");
    await choice.selectOption("archived_forecast");
    await expect.poll(() => sources.at(-1)).toBe("archived_forecast");
    await expect(heading).toHaveText("Irradiance forecast");
    await expect(choice).toHaveValue("archived_forecast");
    await expect(day).toHaveValue("2026-03-27");
    await day.fill("2026-03-28");
    await choice.selectOption("estimated_actuals");
    await expect(day).toHaveValue("2026-03-28");
    await expect(day).toHaveAttribute("max", "2026-03-28");
    await expect.poll(() => sources.at(-1)).toBe("estimated_actuals");
    await page
        .getByRole("button", { name: "Generation forecast", exact: true })
        .click();
    await expect(choice).toHaveValue("estimated_actuals");
    await expect(choice.locator("option")).toHaveText([
        "Forecast",
        "Last forecast before sunrise",
        "Production calculated from satellite data",
    ]);
    await expect(day).toHaveValue("2026-03-28");
    await expect
        .poll(() => plantRequests.at(-1)?.searchParams.get("date"))
        .toBe("2026-03-28");
    const asked = plantRequests.length;
    await day.fill("2026-03-29");
    await expect(day).toHaveValue("2026-03-29");
    await expect(choice).toHaveValue("estimated_actuals");
    expect(plantRequests).toHaveLength(asked);
    await choice.selectOption("forecast");
    await expect(day).toHaveValue("2026-03-29");
    await expect(page.getByLabel("Interval", { exact: true })).toHaveValue(
        "60",
    );
    await expect(heading).toHaveText("Generation forecast");
    await expect(choice).toHaveValue("forecast");
    await expect(generation).toContainText("129.29");
    expect(
        plantRequests
            .map((url) => url.search)
            .filter((search) => search.includes("date=2026-03-29")),
    ).toEqual(["?date=2026-03-29&resolution_minutes=60&data_source=auto"]);
    await day.fill("2026-03-27");
    await expect(generation).toContainText("127.27");
    await expect(heading).toHaveText(
        "Production calculated from satellite data",
    );
    await expect(choice).toHaveValue("estimated_actuals");
    await page.getByLabel("Interval", { exact: true }).selectOption("30");
    await expect
        .poll(() =>
            plantRequests.at(-1)?.searchParams.get("resolution_minutes"),
        )
        .toBe("30");
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page
        .getByText("Source of actual production", { exact: true })
        .click();
    await expect(
        page.getByText(
            "Your entry stays in use even when a later EPİAŞ value arrives.",
            { exact: true },
        ),
    ).toBeVisible();
});

test("forecast crops edge zeros, retains gaps and downloads the full safe CSV", async ({
    page,
}) => {
    await mockApi(page);
    const values = [0, 12.34567, null, 5, 0];
    await page.route("**/api/**/forecast/**", (route) =>
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
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await page.getByText("Show table", { exact: true }).click();
    const table = page.getByRole("table");
    await expect(table.getByRole("row")).toHaveCount(4);
    await expect(table.getByRole("cell", { name: /^07:00–/ })).toBeVisible();
    await expect(table.getByRole("cell", { name: /^06:00–/ })).toHaveCount(0);
    await expect(page.locator(".chart-column.missing")).toHaveCount(1);
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download CSV", exact: true })
        .click();
    const bytes = await readFile(await (await download).path());
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
    expect(parsed.data[0]).toEqual({
        plant_id: "21",
        plant_name: "East array",
        timezone: region.timezone,
        date: "2026-01-01",
        source_kind: "Forecast",
        starts_at: "2026-01-01T06:00:00+03:00",
        ends_at: "2026-01-01T07:00:00+03:00",
        ghi_wm2: "0",
        quality_flags: "",
    });
    expect(csv).not.toContain("SUM(");
});

test("all-zero forecasts remain visible as measured zero values", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/**/forecast/**", (route) =>
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
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await page.getByText("Show table", { exact: true }).click();
    await expect(
        page.getByRole("table").getByRole("cell", { name: "0", exact: true }),
    ).toHaveCount(2);
    await expect(page.locator(".chart-column.missing")).toHaveCount(1);
});

test("mock contract: saves cached forecast reminders without provider queries", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Forecast email" }).click();
    await page.getByLabel("Send forecast email for this plant").check();
    await page.getByLabel("Send time 1", { exact: true }).fill("08:00");
    await page.getByRole("button", { name: "Add time", exact: true }).click();
    await page.getByLabel("Send time 2", { exact: true }).fill("17:30");
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().includes("/reminders/"),
    );
    await page.getByRole("button", { name: "Save" }).click();
    expect((await outgoing).postDataJSON()).toEqual({
        enabled: true,
        include_workspace_members: false,
        recipients: [user.email],
        language: "en",
        times: ["08:00", "17:30"],
        expected_revision: 0,
        hide_recipients: true,
    });
    await expect(
        page.getByText("At most 10 a day.", { exact: true }),
    ).toBeVisible();
});
test("mock contract: visible API error, empty state and accessible mobile layout", async ({
    page,
}) => {
    await mockApi(page, { failed: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/app/");
    await expect(
        page.getByRole("alert").filter({ hasText: "temporarily unavailable" }),
    ).toContainText("temporarily unavailable");
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    const scan = await accessibilityViolations(page);
    expect(scan).toEqual([]);
});
test("CSV rejects ambiguous units, offsets, overlaps, invalid energy and oversized batches", () => {
    const header = "starts_at,ends_at,energy_kwh,quality,source\n";
    for (const columns of [
        "start,end,power",
        "starts_at,ends_at,energy,quality,source",
        "starts_at,ends_at,energy_kwh,energy_mwh,quality,source",
        "starts_at,ends_at,energy_kwh,quality,source,plant",
    ])
        expect(() => parseProduction(`${columns}\n`, csvTexts())).toThrow(
            "energy_kwh or energy_mwh",
        );
    expect(() =>
        parseProduction(
            header + "2026-01-01T09:00:00,2026-01-01T09:30:00,-1,valid,meter",
            csvTexts(),
        ),
    ).toThrow("with their offset");
    expect(() =>
        parseProduction(
            header + "2026-01-01T09:00:00Z,2026-01-01T09:30:00Z,-1,valid,meter",
            csvTexts(),
        ),
    ).toThrow("zero or a positive number");
    const row = "2026-01-01T09:00:00Z,2026-01-01T09:30:00Z,1,valid,meter";
    expect(() =>
        parseProduction(header + row + "\n" + row, csvTexts()),
    ).toThrow("Some times in the file overlap.");
    expect(() =>
        parseProduction(header + Array(2001).fill(row).join("\n"), csvTexts()),
    ).toThrow("2,000");
});

test("CSV preserves quoted commas and multiline source fields and rejects missing energy", () => {
    const header = "starts_at,ends_at,energy_kwh,quality,source\r\n";
    const interval = "2026-01-01T09:00:00Z,2026-01-01T09:30:00Z";
    const parsed = parseProduction(
        header + interval + ',0,valid,"Meter, north\nroof"\r\n',
        csvTexts(),
    );
    expect(parsed.unit).toBe("kWh");
    expect(parsed.observations).toEqual([
        {
            starts_at: "2026-01-01T09:00:00Z",
            ends_at: "2026-01-01T09:30:00Z",
            energy: 0,
            quality: "valid",
            source: "Meter, north\nroof",
        },
    ]);
    expect(() =>
        parseProduction(header + interval + ",,valid,meter", csvTexts()),
    ).toThrow("zero or a positive number");
    expect(() =>
        parseProduction(
            header + interval + ',1,valid,"unterminated',
            csvTexts(),
        ),
    ).toThrow("could not be read as CSV");
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
                next: second ? null : "/api/tenants/?page=2",
                previous: null,
                results: [
                    {
                        id: second ? 2 : 1,
                        name: second ? "Second workspace" : "Sunfield Energy",
                        role: "viewer",
                    },
                ],
            },
        });
    });
    await page.route(/\/api\/.*\/forecast\/\?/, async (route) =>
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
    await page.goto("/app/");
    await expect(
        page.getByRole("option", { name: "Second workspace" }),
    ).toHaveCount(1);
    await page.screenshot({
        path: testInfo.outputPath("overview-desktop.png"),
        fullPage: true,
    });
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await expect(page.getByRole("status")).toHaveText(
        "Data is missing for some hours.",
    );
    await expect(page.getByText(/^Forecast data: /)).toBeVisible();
    await expect(page.getByText(/synthetic fixture/)).toHaveCount(0);
    await page.getByText("Show table", { exact: true }).click();
    await expect(
        page.getByRole("cell", { name: "500", exact: true }),
    ).toBeVisible();
    const scan = await accessibilityViolations(page);
    expect(scan).toEqual([]);
});
async function openJournal(page: Page) {
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Actual production" }),
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
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    const performance = page.locator("section").filter({
        has: page.getByRole("heading", {
            name: "Comparison",
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
    await page.getByRole("button", { name: "Show", exact: true }).click();
    try {
        await expect.poll(() => requested).toBe(true);
        await expect(csv).toHaveCount(0);
        await expect(performance.getByRole("status")).toContainText("Loading…");
    } finally {
        release();
    }
    await expect(csv).toBeVisible();
    const download = page.waitForEvent("download");
    await csv.click();
    const exported = await download;
    expect(exported.suggestedFilename()).toBe(
        "pyracast-east-array-2026-11-01-2026-11-02-comparison.csv",
    );
    const parsed = Papa.parse<Record<string, string>>(
        await readFile(await exported.path(), "utf8"),
        { header: true, skipEmptyLines: true },
    );
    expect(parsed.meta.fields).toContain("comparison");
    expect(parsed.data).toEqual([]);
});

test("forecast performance shows archived metrics, missing days and selected range", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    await mockApi(page);
    let lastQuery = "";
    await page.route("**/plants/21/performance/**", async (route) => {
        lastQuery = new URL(route.request().url()).search;
        await route.fulfill({
            json: {
                comparison: "archived_forecast",
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
                        source_kind: "estimated_actuals",
                        provisional_production: false,
                        calibration_in_sample: false,
                    },
                    {
                        date: "2026-11-02",
                        status: "missing",
                        reason: "forecast_not_available",
                        measured_energy_mwh: null,
                        predicted_energy_mwh: null,
                        error_mwh: null,
                        source_kind: null,
                        provisional_production: false,
                        calibration_in_sample: null,
                    },
                ],
            },
        });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Comparison", exact: true }),
    ).toBeVisible();

    await page.getByText("Additional metrics", { exact: true }).click();
    await page.getByText("Days not compared (1)", { exact: true }).click();
    await expect(
        page.getByText("No forecast to compare for this day: 2026-11-02"),
    ).toBeVisible();
    await page
        .getByText("How the comparison is measured", { exact: true })
        .click();
    await expect(
        page.getByText("Compares the last forecast before sunrise", {
            exact: false,
        }),
    ).toBeVisible();
    await expect(
        page.getByText("Mean absolute daily error without calibration"),
    ).toBeVisible();
    await expect(
        page.locator(".stat").filter({
            hasText: "Mean absolute daily error without calibration",
        }),
    ).toContainText("2.5");
    await expect(
        page.getByText("Mean absolute error in producing intervals", {
            exact: true,
        }),
    ).toBeVisible();
    await page.getByLabel("Start date").fill("2026-11-01");
    await page.getByLabel("End date").fill("2026-11-02");
    await page.getByRole("button", { name: "Show", exact: true }).click();
    await expect
        .poll(() => lastQuery)
        .toContain("start_date=2026-11-01&end_date=2026-11-02");
    await expect.poll(() => lastQuery).toContain("daylight_only=true");

    await page.getByText("Additional metrics", { exact: true }).click();
    await expect(page.getByRole("cell", { name: "8.5" })).toBeVisible();
    await expect(
        page.getByRole("img", {
            name: "Absolute error by day",
            exact: true,
        }),
    ).toBeVisible();
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download CSV", exact: true })
        .last()
        .click();
    const exported = Papa.parse<Record<string, string>>(
        await readFile(await (await download).path(), "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(exported).toHaveLength(2);
    expect(exported[0]).toMatchObject({
        plant_name: "East array",
        timezone: region.timezone,
        date: "2026-11-01",
        error_mwh: "-1.5",
    });
    expect(exported[0]).not.toHaveProperty("metrics.daily_bias_mwh");
    expect(exported[1]).toMatchObject({
        date: "2026-11-02",
        error_mwh: "",
        reason: "forecast_not_available",
    });
    await expect(
        page.locator(".stat").filter({
            hasText: "Mean absolute error in producing intervals",
        }),
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
        selection_state: "not_ready",
        reason: null,
        activation_blocker: "calibration_not_activatable",
        assessment: "provisional",
        factor: 1.1,
        result: {
            metric_scope: "daylight_intervals",
            valid_days: 10,
            training_days: 8,
            holdout_days: 2,
            training_support: { informative_days: 4, minimum_days: 5 },
            reasons: ["incumbent_mae_regression", "zero_production_holdout"],
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
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await page.getByRole("button", { name: "View details" }).click();
    await expect(
        page.getByText(
            "It increased the error compared with the calibration in use then.",
        ),
    ).toBeVisible();
    await expect(page.getByText("4 of 5 needed")).toBeVisible();
    await expect(
        page.getByText(
            "Days without production among the test days prevent a change of calibration.",
        ),
    ).toBeVisible();
    await expect(
        page.getByText(
            "Errors count whole intervals that overlap daylight, not 24-hour totals.",
        ),
    ).toBeVisible();
    await expect(
        page.getByText("Mean absolute daily error (MWh)", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("Average daily difference (MWh)", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("columnheader", {
            name: "Error with the calibration in use then (factor 1.2)",
        }),
    ).toBeVisible();
    const download = page.waitForEvent("download");
    await page
        .locator(".calibration-detail")
        .getByRole("button", { name: "Download CSV" })
        .click();
    const exported = Papa.parse<Record<string, string>>(
        await readFile(await (await download).path(), "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(exported[0]).toMatchObject({
        plant_name: "East array",
        id: "40",
        status: "rejected",
        factor: "1.1",
        activation_blocker: "calibration_not_activatable",
        training_days: "8",
        holdout_days: "2",
        baseline_daily_mae_mwh: "3",
        candidate_daily_mae_mwh: "2",
    });
});

test("recycle bin confirms deletion and restores a plant site with associated data", async ({
    page,
}) => {
    await mockApi(page);
    let deleted = false;
    const entity = region;
    const entityPath = "/api/tenants/1/regions/11/";
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
            path === "/api/tenants/1/sites/" &&
            route.request().method() === "GET"
        ) {
            await route.fulfill({
                json: paged(deleted ? [] : [{ ...region, plant }]),
            });
        } else if (path.includes("/trash/")) {
            const ownList = path.endsWith("/trash/regions/");
            await route.fulfill({
                json: paged(
                    deleted && ownList
                        ? [
                              {
                                  id: entity.id,
                                  name: entity.name,
                                  plant_name: entity.name,
                                  latitude: entity.latitude,
                                  longitude: entity.longitude,
                                  deleted_at: "2026-11-10T12:00:00Z",
                                  purge_after: "2026-12-10T12:00:00Z",
                                  restore_blocker: null,
                              },
                          ]
                        : [],
                ),
            });
        } else await route.fallback();
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
        .getByRole("button", { name: "Delete plant", exact: true })
        .click();
    const confirmation = page.getByRole("region", {
        name: "Delete plant",
    });
    await expect(confirmation).toContainText("30 days");
    await expect(confirmation).toContainText("deleted permanently");
    await confirmation
        .getByRole("button", { name: "Cancel", exact: true })
        .click();
    expect(deleted).toBe(false);
    await page
        .getByRole("button", { name: "Delete plant", exact: true })
        .click();
    await page
        .getByRole("button", {
            name: "Delete",
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
    const row = page.getByRole("listitem").filter({
        has: page.getByRole("button", {
            name: `Restore ${entity.name}`,
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
        page.getByText(`${entity.name} restored.`, {
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
        .getByRole("navigation", { name: "Main navigation" })
        .getByRole("button", { name: "Plants", exact: true })
        .click();
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: plant.name, exact: true }),
    ).toBeVisible();
});

test("recycle bin disables expired records", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockApi(page);
    await page.route("**/trash/regions/", async (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 91,
                    name: "Expired region",
                    plant_name: "Expired region",
                    latitude: 37,
                    longitude: 31,
                    deleted_at: "2026-01-01T00:00:00Z",
                    purge_after: "2026-01-31T00:00:00Z",
                    restore_blocker: "trash_retention_expired",
                },
            ]),
        }),
    );
    await page.goto("/app/");
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
        page.getByRole("button", { name: /permanently delete|purge/i }),
    ).toHaveCount(0);
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    const scan = await accessibilityViolations(page);
    expect(scan).toEqual([]);
});

test("a failed reload after a restore keeps the remaining plants until a retry loads them", async ({
    page,
}) => {
    await mockApi(page);
    const deleted = [91, 92].map((id) => ({
        id,
        name: `Deleted plant ${id}`,
        plant_name: `Deleted plant ${id}`,
        latitude: 37,
        longitude: 31,
        deleted_at: "2026-11-10T12:00:00Z",
        purge_after: "2026-12-10T12:00:00Z",
        restore_blocker: null,
    }));
    let restored = false;
    let unavailable = false;
    await page.route("**/trash/regions/", (route) =>
        unavailable
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "Recycle bin is busy."),
              })
            : route.fulfill({
                  json: paged(restored ? deleted.slice(1) : deleted),
              }),
    );
    await page.route("**/regions/91/restore/", (route) => {
        restored = true;
        unavailable = true;
        return route.fulfill({ json: { ...region, id: 91 } });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: "Recycle bin", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Restore Deleted plant 91", exact: true })
        .click();
    const notice = page
        .getByRole("alert")
        .filter({ hasText: "Could not refresh" });
    await expect(notice).toContainText("Recycle bin is busy.");
    const remaining = page.getByRole("button", {
        name: "Restore Deleted plant 92",
        exact: true,
    });
    await expect(remaining).toBeVisible();
    unavailable = false;
    await notice
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(notice).toHaveCount(0);
    await expect(remaining).toBeVisible();
    await expect(
        page.getByRole("button", {
            name: "Restore Deleted plant 91",
            exact: true,
        }),
    ).toHaveCount(0);
});

test("recycle bin ignores a prior workspace response and restores only in the selected workspace", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/", async (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
                tenantRow({ id: 2, name: "Second workspace", role: "admin" }),
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
    await page.route("**/api/**/trash/**", async (route) => {
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
                    plant_name: first
                        ? "First workspace deleted region"
                        : "Second workspace deleted region",
                    latitude: 37,
                    longitude: 31,
                    deleted_at: "2026-11-10T00:00:00Z",
                    purge_after: "2026-12-10T00:00:00Z",
                    restore_blocker: null,
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
    await page.goto("/app/");
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
        page.getByText("Second workspace deleted region restored.", {
            exact: true,
        }),
    ).toBeVisible();
});

test("a session that ends while the tab is open returns to sign in with a notice", async ({
    page,
}) => {
    const account = await mockApi(page);
    await page.route("**/api/tenants/", (route) =>
        account.current
            ? route.fallback()
            : route.fulfill({
                  status: 403,
                  json: errorBody("not_authenticated", "Sign in to continue."),
              }),
    );
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    account.current = null;
    await page.clock.fastForward(120_000);
    await expect(
        page.getByText("You were signed out. Sign in again."),
    ).toBeVisible();
    await expect(
        page.locator("form").getByRole("button", { name: "Sign in" }),
    ).toBeVisible();
});

test("a failed portfolio refresh keeps the plant cards until access is denied", async ({
    page,
}) => {
    await mockApi(page);
    let outcome: "ok" | "busy" | "denied" = "ok";
    await page.route("**/api/tenants/1/sites/", (route) =>
        outcome === "busy"
            ? route.fulfill({
                  status: 503,
                  json: errorBody(
                      "busy",
                      "Plants are temporarily unavailable.",
                  ),
              })
            : outcome === "denied"
              ? route.fulfill({
                    status: 403,
                    json: errorBody(
                        "membership_required",
                        "You are not a member of this workspace.",
                    ),
                })
              : route.fallback(),
    );
    await page.goto("/app/");
    const card = page.getByRole("button", { name: /East array/ });
    await expect(card).toBeVisible();
    outcome = "busy";
    await page.clock.fastForward(30_000);
    await expect(
        page.getByText("Plants are temporarily unavailable."),
    ).toBeVisible();
    await expect(card).toBeVisible();
    outcome = "ok";
    await page.clock.fastForward(30_000);
    await expect(
        page.getByText("Plants are temporarily unavailable."),
    ).toHaveCount(0);
    outcome = "denied";
    await page.clock.fastForward(30_000);
    await expect(
        page.getByText("You are not a member of this workspace."),
    ).toBeVisible();
    await expect(card).toHaveCount(0);
});

test("the plant header names the offset of the day on screen and the present offset elsewhere", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({
            json: paged([
                {
                    ...region,
                    timezone: "Europe/Berlin",
                    plant,
                    reminder: null,
                },
            ]),
        }),
    );
    const header = page.locator(".plant-time");
    await page.goto("/app/w/1/plants/11/forecast/?date=2026-07-15");
    await expect(header).toHaveText("Plant time: UTC+2");
    await expect(page.getByText("Dates in plant time")).toHaveCount(0);
    await page.goto("/app/w/1/plants/11/forecast/?date=2026-03-29");
    await expect(header).toHaveText("Plant time: UTC+1 / UTC+2");
    await page.goto("/app/w/1/plants/11/production/?date=2026-08-03");
    await expect(header).toHaveText("Plant time: UTC+2");
    await page.route("**/plants/21/performance/**", (route) =>
        route.fulfill({
            json: answer("PerformanceOutput", {
                start_date: "2026-10-11",
                end_date: "2026-11-09",
                matched_days: 0,
                requested_days: 30,
                metrics: null,
                baseline_metrics: null,
                days: [],
            }),
        }),
    );
    await page.goto("/app/w/1/plants/11/production/?view=comparison");
    await expect(header).toHaveText("Plant time: UTC+2 / UTC+1");
    await page.goto("/app/w/1/plants/11/settings/");
    await expect(header).toHaveText("Plant time: UTC+1");
});
