import { expect, test, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import {
    answer,
    mockApi,
    paged,
    tenantRow,
    user,
    type Mock,
} from "./support/workspace";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";
import { errorBody } from "./support/errors";
import type { components } from "../src/lib/schema";

type Schemas = components["schemas"];

test.use({ timezoneId: "Europe/Istanbul" });

async function workspace(
    page: Page,
    unknownImpact = false,
    configurationChanged = false,
    {
        configured = true,
        role = "admin",
    }: { configured?: boolean; role?: "admin" | "viewer" } = {},
) {
    const paged = (results: unknown[]) => ({
        results,
        count: results.length,
        next: null,
        previous: null,
    });
    let incident: Mock<Schemas["IncidentOutput"]> | null = null;
    let createdUnder: string | undefined;
    let creations = 0;
    let outageReads = 0;
    let conflict = false;
    let dropAnswer: (() => void) | null = null;
    const configuration = {
        id: 31,
        revision: 1,
        created_at: "2026-01-01T00:00:00Z",
        dc_capacity_mw: 5,
        inverter_capacity_mw: 4,
        export_limit_mw: 4,
        measurement_basis: "net_export",
    };
    const region = {
        id: 11,
        name: "Outage region",
        latitude: 36.9,
        longitude: 30.7,
        timezone: "Europe/Istanbul",
        revision: 1,
        status: "approved",
    };
    await page.clock.setFixedTime(new Date("2026-11-11T12:00:00Z"));
    await page.route("**/api/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        const method = route.request().method();
        let data: unknown;
        if (path === "/api/session/")
            data = {
                user: { id: 1, username: "user", email: "user@example.test" },
                csrf_token: "token",
            };
        else if (path === "/api/tenants/")
            data = paged([tenantRow({ id: 1, name: "Tenant", role })]);
        else if (path.endsWith("/change-requests/")) data = paged([]);
        else if (path.endsWith("/sites/"))
            data = paged([
                {
                    ...region,
                    plant: {
                        id: 21,
                        name: "Outage plant",
                        region_id: 11,
                        current_configuration: configured
                            ? configuration
                            : null,
                        parameters: configuration,
                        parameters_revision: 1,
                    },
                },
            ]);
        else if (path.endsWith("/performance/"))
            data = {
                metrics: null,
                baseline_metrics: null,
                days: [],
                matched_days: 0,
                requested_days: 1,
            };
        else if (path.endsWith("/outages/") && method === "GET") {
            outageReads++;
            const results = incident
                ? [{ ...incident, pending_request_id: null }]
                : [];
            data = answer("IncidentPage", {
                results,
                count: results.length,
                next: null,
                previous: null,
                ...(role === "admin"
                    ? {
                          can_propose: false,
                          proposal_unavailable_reason: "admin_edits_directly",
                      }
                    : {
                          can_propose: configured,
                          proposal_unavailable_reason: configured
                              ? null
                              : "configuration_unavailable",
                      }),
            });
        } else if (path.includes("/outages/")) {
            expect(route.request().headers()["x-csrftoken"]).toBe("token");
            const body = route.request().postDataJSON();
            const refuseStale = () =>
                route.fulfill({
                    status: 409,
                    json: answer("IncidentConflict", {
                        ...errorBody(
                            "incident_revision_changed",
                            "The outage changed. Review its current values and save again.",
                        ),
                        current: { ...incident, pending_request_id: null },
                    }),
                });
            if (conflict) {
                conflict = false;
                incident = {
                    ...incident,
                    title: "Changed meanwhile",
                    revision: Number(incident?.revision) + 1,
                };
                await refuseStale();
                return;
            }
            if (method === "POST" && path.endsWith("/outages/")) {
                const { idempotency_key, ...values } = body;
                expect(values.starts_at).toBe("2026-11-11T08:00:00.000Z");
                if (!incident || idempotency_key !== createdUnder) {
                    creations++;
                    createdUnder = idempotency_key;
                    incident = {
                        ...values,
                        id: 51,
                        revision: 1,
                        declared_configuration_id: configurationChanged
                            ? 30
                            : 31,
                        status: values.ends_at ? "resolved" : "active",
                        created_at: "2026-11-11T12:00:00Z",
                        updated_at: "2026-11-11T12:00:00Z",
                    };
                }
            } else {
                const { expected_revision, ...values } = body;
                if (expected_revision !== incident?.revision) {
                    await refuseStale();
                    return;
                }
                incident = {
                    ...incident,
                    ...values,
                    revision: Number(incident?.revision) + 1,
                };
                incident = {
                    ...incident,
                    status: path.endsWith("/cancel/")
                        ? "cancelled"
                        : incident?.ends_at
                          ? "resolved"
                          : "active",
                };
            }
            data = incident;
            if (dropAnswer) {
                const dropped = dropAnswer;
                dropAnswer = null;
                await route.abort("failed");
                dropped();
                return;
            }
        } else if (path.endsWith("/forecast/")) {
            const intervals = [0, 2, 0].map((power, index) => ({
                start: `2026-11-12T${String(index + 8).padStart(2, "0")}:00:00Z`,
                end: `2026-11-12T${String(index + 9).padStart(2, "0")}:00:00Z`,
                mean_power_mw: power,
                energy_mwh: power,
                quality_flags: [],
            }));
            const result = {
                complete: true,
                energy_mwh: 2,
                partial_energy_mwh: 2,
                coverage_fraction: 1,
                intervals,
                quality_flags: [],
            };
            const active = incident?.status === "active";
            data = {
                date: "2026-11-12",
                timezone: region.timezone,
                freshness: "fresh",
                source_kind: "forecasts",
                result,
                outage_scenario: active
                    ? {
                          evaluated_at: "2026-11-11T12:00:00Z",
                          status: unknownImpact
                              ? "unknown_impact"
                              : "estimated",
                          incident_revision_ids: [incident?.revision],
                          incidents: [incident],
                          result: {
                              ...result,
                              quality_flags: configurationChanged
                                  ? ["outage_configuration_changed"]
                                  : [],
                              energy_mwh: unknownImpact ? null : 0,
                              partial_energy_mwh: 0,
                              intervals: intervals.map((row) => ({
                                  ...row,
                                  mean_power_mw: unknownImpact ? null : 0,
                                  energy_mwh: unknownImpact ? null : 0,
                              })),
                          },
                      }
                    : null,
            };
        } else throw new Error(`Unmocked ${method} ${path}`);
        await route.fulfill({ json: data });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /Outage plant/ }).click();
    return {
        reads: () => outageReads,
        incident: () => incident,
        creations: () => creations,
        conflict: () => {
            conflict = true;
        },
        elsewhere: (change: Partial<Schemas["IncidentOutput"]>) => {
            incident = {
                ...incident,
                ...change,
                revision: Number(incident?.revision) + 1,
            };
        },
        dropAnswer: () =>
            new Promise<void>((resolve) => {
                dropAnswer = resolve;
            }),
    };
}

async function create(page: Page, impact = "Total outage") {
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Inverter disconnected");
    await page
        .getByLabel("Started at", { exact: true })
        .fill("2026-11-11T11:00");
    await page.getByLabel("Capacity impact").selectOption({ label: impact });
    await page
        .getByRole("button", { name: "Save outage", exact: true })
        .click();
    await expect(
        page.getByRole("heading", {
            name: "Inverter disconnected",
            exact: true,
        }),
    ).toBeVisible();
}

test("outage lifecycle preserves metadata, compares both scenarios and restores one forecast", async ({
    page,
}) => {
    const state = await workspace(page);
    expect(state.reads()).toBe(0);
    await create(page);
    expect(state.incident()).toMatchObject({
        available_dc_mw: 0,
        available_inverter_mw: 0,
        available_export_mw: 0,
        ends_at: null,
    });
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    await page.getByLabel("Capacity impact").selectOption("reduced");
    await page.getByLabel("Available panel DC capacity (MW)").fill("2");
    await page.getByLabel("Available inverter AC capacity (MW)").fill("1");
    await page.getByRole("button", { name: "Save outage changes" }).click();
    await expect(
        page
            .locator("dl > div")
            .filter({ hasText: "Available panels" })
            .getByText("2 MW", { exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(
        page.locator(".stat").getByText("Normal capacity", { exact: true }),
    ).toBeVisible();
    await expect(
        page.locator(".stat").getByText("With outages", { exact: true }),
    ).toBeVisible();
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download both scenarios CSV" })
        .click();
    const file = await (await download).path();
    const csv = Papa.parse<Record<string, string>>(
        await readFile(file, "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(csv).toHaveLength(3);
    expect(csv[1]).toEqual({
        plant_id: "21",
        plant_name: "Outage plant",
        timezone: "Europe/Istanbul",
        date: "2026-11-12",
        source_kind: "forecasts",
        starts_at: "2026-11-12T12:00:00+03:00",
        ends_at: "2026-11-12T13:00:00+03:00",
        normal_mean_power_mw: "2",
        normal_energy_mwh: "2",
        normal_quality_flags: "",
        affected_mean_power_mw: "0",
        affected_energy_mwh: "0",
        affected_quality_flags: "",
    });
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Inverter disconnected" }),
    ).toBeVisible();
    const accessibility = await accessibilityViolations(page);
    expect(accessibility).toEqual([]);
    await page
        .getByRole("button", { name: "Resolve outage", exact: true })
        .click();
    await page
        .getByLabel("Resolved at", { exact: true })
        .fill("2026-11-11T14:00");
    await page.getByRole("button", { name: "Save resolution" }).click();
    await expect(page.getByText("Resolved", { exact: true })).toBeVisible();
    expect(state.incident()).toMatchObject({
        ends_at: "2026-11-11T11:00:00.000Z",
        revision: 3,
    });
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(
        page.getByText("Estimated energy", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Download both scenarios CSV" }),
    ).toHaveCount(0);
});

test("a failed reload after saving keeps the outages until a retry loads them", async ({
    page,
}) => {
    await workspace(page);
    await create(page);
    let unavailable = false;
    await page.route("**/outages/", (route) =>
        unavailable && route.request().method() === "GET"
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "Outage service is busy."),
              })
            : route.fallback(),
    );
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    await page.getByLabel("Capacity impact").selectOption("reduced");
    await page.getByLabel("Available panel DC capacity (MW)").fill("2");
    await page.getByLabel("Available inverter AC capacity (MW)").fill("1");
    unavailable = true;
    await page.getByRole("button", { name: "Save outage changes" }).click();
    const notice = page
        .getByRole("alert")
        .filter({ hasText: "Could not refresh" });
    await expect(notice).toContainText("Outage service is busy.");
    await expect(
        page.getByRole("heading", {
            name: "Inverter disconnected",
            exact: true,
        }),
    ).toBeVisible();
    unavailable = false;
    await notice
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(notice).toHaveCount(0);
    await expect(
        page
            .locator("dl > div")
            .filter({ hasText: "Available panels" })
            .getByText("2 MW", { exact: true }),
    ).toBeVisible();
});

test("a creation saved again after its answer was lost keeps its key, also across a whitespace-only edit, and shows one outage, and an edit sends a new key", async ({
    page,
}) => {
    const state = await workspace(page);
    const keys: unknown[] = [];
    let reachable = false;
    await page.route("**/plants/21/outages/", (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        keys.push(route.request().postDataJSON().idempotency_key);
        return reachable ? route.fallback() : route.abort("failed");
    });
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Inverter disconnected");
    await page
        .getByLabel("Started at", { exact: true })
        .fill("2026-11-11T11:00");
    await page
        .getByLabel("Capacity impact")
        .selectOption({ label: "Total outage" });
    const save = page.getByRole("button", { name: "Save outage", exact: true });
    const unconfirmed = page
        .getByRole("alert")
        .filter({ hasText: "did not confirm the change" });
    await save.click();
    await expect(unconfirmed).toBeVisible();
    await page.getByLabel("Incident title").fill("Inverter tripped");
    reachable = true;
    const dropped = state.dropAnswer();
    await save.click();
    await dropped;
    await expect(unconfirmed).toBeVisible();
    await page.getByLabel("Incident title").fill("  Inverter tripped ");
    await save.click();
    await expect(
        page.getByRole("heading", { name: "Inverter tripped", exact: true }),
    ).toBeVisible();
    await expect(save).toHaveCount(0);
    await expect(page.locator("h3[data-outage]")).toHaveCount(1);
    expect(keys).toHaveLength(3);
    expect(keys[1]).not.toBe(keys[0]);
    expect(keys[2]).toBe(keys[1]);
    expect(typeof keys[1]).toBe("string");
    expect(state.creations()).toBe(1);
});

test("an edit saved again after its answer was lost closes as saved, shows the outage once at its new revision and sends no further write", async ({
    page,
}) => {
    const state = await workspace(page);
    await create(page);
    const revisions: unknown[] = [];
    page.on("request", (request) => {
        if (request.method() === "PUT")
            revisions.push(request.postDataJSON().expected_revision);
    });
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    await page.getByLabel("Capacity impact").selectOption("reduced");
    await page.getByLabel("Available panel DC capacity (MW)").fill("2.50");
    await page.getByLabel("Notes (optional)").fill(" Replaced fuse ");
    const save = page.getByRole("button", { name: "Save outage changes" });
    const dropped = state.dropAnswer();
    await save.click();
    await dropped;
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "did not confirm the change" }),
    ).toBeVisible();
    await save.click();
    await expect(save).toHaveCount(0);
    await expect(page.locator("h3[data-outage]")).toHaveCount(1);
    await expect(
        page
            .locator("dl > div")
            .filter({ hasText: "Available panels" })
            .getByText("2.5 MW", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("Someone changed this outage after you opened it."),
    ).toHaveCount(0);
    expect(state.incident()).toMatchObject({
        revision: 2,
        available_dc_mw: 2.5,
        notes: "Replaced fuse",
    });
    expect(revisions).toEqual([1, 1]);
});

test("a first save that meets an outage someone else saved with the same values shows the stale notice and saves only when asked", async ({
    page,
}) => {
    const state = await workspace(page, false, true);
    await create(page);
    const revisions: unknown[] = [];
    page.on("request", (request) => {
        if (request.method() === "PUT")
            revisions.push(request.postDataJSON().expected_revision);
    });
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    state.elsewhere({});
    const save = page.getByRole("button", { name: "Save outage changes" });
    await save.click();
    await expect(
        page.getByRole("alert").filter({
            hasText: "Someone changed this outage after you opened it.",
        }),
    ).toBeVisible();
    await expect(save).toBeEnabled();
    expect(revisions).toEqual([1]);
    expect(state.incident()?.revision).toBe(2);
    await save.click();
    await expect(save).toHaveCount(0);
    expect(revisions).toEqual([1, 2]);
    expect(state.incident()?.revision).toBe(3);
});

test("an edit sent again after an unanswered try to an outage cancelled meanwhile shows the cancelled outage and cannot send", async ({
    page,
}) => {
    const state = await workspace(page);
    await create(page);
    const revisions: unknown[] = [];
    let reachable = false;
    await page.route("**/plants/21/outages/51/", (route) => {
        revisions.push(route.request().postDataJSON().expected_revision);
        return reachable ? route.fallback() : route.abort("failed");
    });
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    const save = page.getByRole("button", { name: "Save outage changes" });
    await save.click();
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "did not confirm the change" }),
    ).toBeVisible();
    state.elsewhere({ status: "cancelled" });
    reachable = true;
    await save.click();
    const editor = page.getByRole("region", { name: "Edit outage" });
    await expect(
        editor.getByRole("status").filter({
            hasText:
                "This outage is now Cancelled, so your change cannot be applied.",
        }),
    ).toBeVisible();
    await expect(save).toHaveCount(0);
    expect(revisions).toEqual([1, 1]);
    expect(state.incident()).toMatchObject({
        revision: 2,
        status: "cancelled",
    });
});

test("unknown impact remains missing and stale edits show a conflict", async ({
    page,
}) => {
    const state = await workspace(page, true, true);
    await create(page, "Unknown impact");
    expect(state.incident()).toMatchObject({
        available_dc_mw: null,
        available_inverter_mw: null,
        available_export_mw: null,
    });
    await expect(
        page.getByText(/Configuration changed. Edit this report/),
    ).toBeVisible();
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(
        page.getByText("Outage impact is unknown for some intervals.", {
            exact: true,
        }),
    ).toBeVisible();
    await expect(
        page.getByText(
            "Update remaining capacity in Outages after this configuration change.",
        ),
    ).toBeVisible();
    await page.getByText(/View interval data/).click();
    await expect(
        page.getByRole("columnheader", {
            name: "With outages (MWh)",
            exact: true,
        }),
    ).toBeVisible();
    const intervalValues = page.getByRole("table").locator(".interval-value");
    await expect(intervalValues).toHaveText(["2", "—"]);
    await expect(
        page.getByRole("table").getByRole("cell", { name: /Data unavailable/ }),
    ).toHaveCount(1);
});

test("a stale edit keeps what was typed, shows what changed and saves only when asked", async ({
    page,
}) => {
    const state = await workspace(page);
    await create(page);
    const writes: string[] = [];
    page.on("request", (request) => {
        if (request.method() === "PUT") writes.push(request.url());
    });
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    await page.getByLabel("Capacity impact").selectOption("reduced");
    await page.getByLabel("Available panel DC capacity (MW)").fill("2");
    state.conflict();
    await page.getByRole("button", { name: "Save outage changes" }).click();
    await expect(
        page.getByRole("alert").filter({
            hasText: "Someone changed this outage after you opened it.",
        }),
    ).toBeVisible();
    await expect(
        page.getByText("Title: Inverter disconnected → Changed meanwhile"),
    ).toBeVisible();
    await expect(
        page.getByLabel("Available panel DC capacity (MW)"),
    ).toHaveValue("2");
    expect(writes).toHaveLength(1);
    expect(state.incident()).toMatchObject({
        revision: 2,
        title: "Changed meanwhile",
    });
    await page.getByRole("button", { name: "Save outage changes" }).click();
    await expect(
        page.getByRole("heading", { name: "Inverter disconnected" }),
    ).toBeVisible();
    expect(writes).toHaveLength(2);
    expect(state.incident()).toMatchObject({
        revision: 3,
        title: "Inverter disconnected",
        available_dc_mw: 2,
    });
});

test("the outage export waits for the list reloaded after a stale edit", async ({
    page,
}) => {
    const state = await workspace(page);
    await create(page);
    let holding = false;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    let reloading!: () => void;
    const reload = new Promise<void>((resolve) => {
        reloading = resolve;
    });
    await page.route("**/plants/21/outages/", async (route) => {
        if (holding && route.request().method() === "GET") {
            reloading();
            await gate;
        }
        return route.fallback();
    });
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    state.conflict();
    holding = true;
    await page.getByRole("button", { name: "Save outage changes" }).click();
    await reload;
    await expect(
        page.getByRole("alert").filter({
            hasText: "Someone changed this outage after you opened it.",
        }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Download outages CSV" }),
    ).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Working…" })).toBeDisabled();
    holding = false;
    release();
    await expect(
        page.getByRole("heading", { name: "Changed meanwhile" }),
    ).toBeVisible();
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download outages CSV" }).click();
    const records = Papa.parse<Record<string, string>>(
        await readFile(await (await download).path(), "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(records.map((record) => record.title)).toEqual([
        "Changed meanwhile",
    ]);
});

test("a stale edit can be discarded for the current version", async ({
    page,
}) => {
    const state = await workspace(page);
    await create(page);
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    state.conflict();
    await page.getByRole("button", { name: "Save outage changes" }).click();
    await page.getByRole("button", { name: "Discard my change" }).click();
    await expect(page.getByRole("region", { name: "Edit outage" })).toHaveCount(
        0,
    );
    await expect(
        page.getByRole("heading", { name: "Changed meanwhile" }),
    ).toBeVisible();
    expect(state.incident()?.revision).toBe(2);
});

test("rejects future times and requires confirmation to cancel an incorrect incident", async ({
    page,
}) => {
    const state = await workspace(page);
    await create(page);
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    await page
        .getByLabel("Started at", { exact: true })
        .fill("2026-11-12T11:00");
    await page.getByRole("button", { name: "Save outage changes" }).click();
    await expect(
        page.getByRole("alert").filter({ hasText: "not in the future" }),
    ).toBeVisible();
    expect(state.incident()?.revision).toBe(1);
    await page.getByRole("button", { name: "Close editor" }).click();
    await page.getByText("Cancel an incorrect entry", { exact: true }).click();
    await page
        .getByRole("button", { name: "Cancel outage", exact: true })
        .click();
    expect(state.incident()?.status).toBe("active");
    await page
        .getByRole("checkbox", {
            name: "I confirm this incident was recorded incorrectly.",
        })
        .check();
    await page
        .getByRole("button", { name: "Cancel outage", exact: true })
        .click();
    await page.getByText("1 cancelled entry", { exact: true }).click();
    await expect(page.getByText("Cancelled", { exact: true })).toBeVisible();
    expect(state.incident()?.revision).toBe(2);
});

for (const role of ["admin", "viewer"] as const) {
    test(`without a plant configuration the ${role} cannot report an outage and sees why`, async ({
        page,
    }) => {
        await workspace(page, false, false, { configured: false, role });
        await page
            .getByRole("button", { name: "Outages", exact: true })
            .click();
        await expect(
            page.getByText(
                "Outages can be reported once the plant configuration is approved.",
            ),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: /^(Report|Submit) outage$/ }),
        ).toHaveCount(0);
    });
}

test("reduced capacity needs at least one available value", async ({
    page,
}) => {
    const state = await workspace(page);
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Inverter disconnected");
    await page
        .getByLabel("Started at", { exact: true })
        .fill("2026-11-11T11:00");
    await page.getByLabel("Capacity impact").selectOption("reduced");
    await page
        .getByRole("button", { name: "Save outage", exact: true })
        .click();
    await expect(
        page.getByRole("alert").filter({
            hasText:
                "Enter at least one available capacity, or choose unknown impact.",
        }),
    ).toBeVisible();
    expect(state.incident()).toBeNull();
});

test("capacity inputs show the configured ceiling", async ({ page }) => {
    await workspace(page);
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Capacity impact").selectOption("reduced");
    await expect(
        page.getByLabel("Available panel DC capacity (MW)"),
    ).toHaveAttribute("max", "5");
    await expect(
        page.getByLabel("Available inverter AC capacity (MW)"),
    ).toHaveAttribute("max", "4");
    await expect(
        page.getByLabel("Available grid export capacity (MW)"),
    ).toHaveAttribute("max", "4");
});

test("an emptied resolution is refused before it is sent", async ({ page }) => {
    const state = await workspace(page);
    await create(page);
    const resolutions: string[] = [];
    page.on("request", (request) => {
        if (request.url().endsWith("/resolve/"))
            resolutions.push(request.url());
    });
    await page
        .getByRole("button", { name: "Resolve outage", exact: true })
        .click();
    const resolvedAt = page.getByLabel("Resolved at", {
        exact: true,
    });
    await resolvedAt.fill("");
    await page.getByRole("button", { name: "Save resolution" }).click();
    expect(
        await resolvedAt.evaluate(
            (input: HTMLInputElement) => input.validity.valueMissing,
        ),
    ).toBe(true);
    expect(resolutions).toEqual([]);
    expect(state.incident()?.revision).toBe(1);
});

test("an ongoing outage is tagged as a warning and a resolved one is labelled", async ({
    page,
}) => {
    await workspace(page);
    await create(page);
    const tag = page.locator("article.provenance .tag");
    await expect(tag).toHaveText("Active");
    await expect(tag).toHaveClass(/tag-warning/);
    await page
        .getByRole("button", { name: "Resolve outage", exact: true })
        .click();
    await page
        .getByLabel("Resolved at", { exact: true })
        .fill("2026-11-11T14:00");
    await page.getByRole("button", { name: "Save resolution" }).click();
    await expect(tag).toHaveText("Resolved");
    await expect(tag).toHaveClass(/tag-neutral/);
});

test.describe("in a browser zone west of the plant", () => {
    test.use({ timezoneId: "America/New_York" });

    test("outage times are entered and shown in plant time", async ({
        page,
    }) => {
        const state = await workspace(page);
        await page
            .getByRole("button", { name: "Outages", exact: true })
            .click();
        await expect(page.getByText(/device timezone/)).toHaveCount(0);
        await page
            .getByRole("button", { name: "Report outage", exact: true })
            .click();
        await expect(
            page.getByLabel("Started at", { exact: true }),
        ).toHaveValue("2026-11-11T15:00");
        await page.getByLabel("Incident title").fill("Inverter disconnected");
        await page
            .getByLabel("Started at", { exact: true })
            .fill("2026-11-11T11:00");
        await page
            .getByRole("button", { name: "Save outage", exact: true })
            .click();
        await expect(
            page.getByText(/Started Nov 11, 2026, 11:00\sAM/),
        ).toBeVisible();
        expect(state.incident()).toMatchObject({
            starts_at: "2026-11-11T08:00:00.000Z",
        });
    });
});

type OutageRow = Schemas["IncidentListOutput"];

function listedOutage(
    id: number,
    title: string,
    pending: number | null,
): OutageRow {
    return {
        id,
        revision: 2,
        kind: "inverter",
        title,
        notes: "",
        starts_at: "2026-11-09T08:00:00Z",
        ends_at: null,
        available_dc_mw: null,
        available_inverter_mw: null,
        available_export_mw: null,
        status: "active",
        declared_configuration_id: 31,
        created_at: "2026-11-09T08:10:00Z",
        updated_at: "2026-11-09T08:10:00Z",
        pending_request_id: pending,
    };
}

function memberList(held: number | null = null) {
    return answer("IncidentPage", {
        ...paged([
            listedOutage(4, "Inverter offline", held === 4 ? 9 : null),
            listedOutage(5, "Export limited", null),
        ]),
        can_propose: true,
        proposal_unavailable_reason: null,
    });
}

// A member whose clock runs, so the outage list is read again when the page regains focus.
async function memberOutages(
    page: Page,
    read: () =>
        ReturnType<typeof memberList> | Promise<ReturnType<typeof memberList>>,
) {
    await page.clock.install({ time: new Date("2026-11-10T12:00:00Z") });
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { user, language: "en", csrf_token: "signed-token" },
        }),
    );
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Workspace", role: "viewer" }),
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({ json: paged([]) }),
    );
    await page.route("**/api/**/outages/", async (route) =>
        route.fulfill({ json: await read() }),
    );
}

async function openOutages(page: Page) {
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Outages", exact: true }).click();
}

async function readOnFocus(page: Page) {
    await page.clock.runFor(5_000);
    await page.evaluate(() =>
        document.dispatchEvent(
            new Event("visibilitychange", { bubbles: true }),
        ),
    );
}

function outageCard(page: Page, title: string) {
    return page.locator("article").filter({
        has: page.getByRole("heading", { name: title }),
    });
}

// A route waits at the gate until the test opens it; `reached` settles once a request waits there.
function gate() {
    let open!: () => void;
    let arrive!: () => void;
    const opened = new Promise<void>((resolve) => {
        open = resolve;
    });
    const reached = new Promise<void>((resolve) => {
        arrive = resolve;
    });
    return {
        open,
        reached,
        async pass() {
            arrive();
            await opened;
        },
    };
}

const heldText =
    "A request of yours for this outage now waits for approval. Withdraw it before proposing another change.";

test("while a resolution is sent and the list reloads after it, no other proposal of the screen can be opened or sent", async ({
    page,
}) => {
    const answered = gate();
    const reload = gate();
    let sent = false;
    const submissions: unknown[] = [];
    await memberOutages(page, async () => {
        if (sent) await reload.pass();
        return memberList(sent ? 4 : null);
    });
    await page.route("**/change-requests/incidents/", async (route) => {
        submissions.push(route.request().postDataJSON());
        await answered.pass();
        sent = true;
        await route.fulfill({ status: 201, json: {} });
    });
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    const other = outageCard(page, "Export limited");
    await inverter
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    await inverter
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await answered.reached;

    const quiet = async () => {
        await expect(
            inverter.getByRole("button", { name: "Saving…", exact: true }),
        ).toBeDisabled();
        await expect(
            inverter.getByRole("button", {
                name: "Suggest changes",
                exact: true,
            }),
        ).toHaveCount(0);
        await expect(
            inverter.getByText("Request cancellation", { exact: true }),
        ).toHaveCount(0);
        for (const name of ["Suggest changes", "Submit resolution"])
            await expect(
                other.getByRole("button", { name, exact: true }),
            ).toBeDisabled();
        await expect(
            page.getByRole("button", { name: "Submit outage", exact: true }),
        ).toBeDisabled();
    };
    await quiet();
    answered.open();
    await reload.reached;
    await quiet();
    reload.open();
    await expect(page.getByText(/^Sent for approval\./)).toBeVisible();
    await expect(
        other.getByRole("button", { name: "Suggest changes", exact: true }),
    ).toBeEnabled();
    expect(submissions).toHaveLength(1);
});

test("an outage editor that is sending cannot be replaced by a new outage", async ({
    page,
}) => {
    const answered = gate();
    const submissions: unknown[] = [];
    await memberOutages(page, () => memberList());
    await page.route("**/change-requests/incidents/", async (route) => {
        submissions.push(route.request().postDataJSON());
        await answered.pass();
        await route.fulfill({ status: 201, json: {} });
    });
    await openOutages(page);
    await outageCard(page, "Inverter offline")
        .getByRole("button", { name: "Suggest changes", exact: true })
        .click();
    const editor = page.getByRole("region", { name: "Edit outage" });
    await editor.getByLabel("Incident title").fill("Inverter replaced");
    await editor
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await answered.reached;
    const submit = page.getByRole("button", {
        name: "Submit outage",
        exact: true,
    });
    await expect(submit).toBeDisabled();
    await expect(
        editor.getByRole("button", { name: "Saving…", exact: true }),
    ).toBeDisabled();
    answered.open();
    await expect(page.getByText(/^Sent for approval\./)).toBeVisible();
    await expect(editor).toHaveCount(0);
    await expect(submit).toBeEnabled();
    expect(submissions).toEqual([
        expect.objectContaining({
            action: "update",
            incident_id: 4,
            values: expect.objectContaining({ title: "Inverter replaced" }),
        }),
    ]);
});

test("a resolution form whose outage comes to wait for the member's own request keeps what was typed, cannot send and does not reopen by itself", async ({
    page,
}) => {
    let held: number | null = null;
    const submissions: unknown[] = [];
    await memberOutages(page, () => memberList(held));
    await page.route("**/change-requests/incidents/", (route) => {
        submissions.push(route.request().postDataJSON());
        return route.fulfill({ status: 201, json: {} });
    });
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    await inverter
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    const resolvedAt = inverter.getByLabel("Resolved at", { exact: true });
    await resolvedAt.fill("2026-11-10T14:00");

    held = 4;
    await readOnFocus(page);
    const pending = inverter.getByRole("link", {
        name: "Waiting for approval: view the request for Inverter offline",
    });
    await expect(pending).toBeVisible();
    await expect(
        inverter.getByRole("status").filter({ hasText: heldText }),
    ).toBeVisible();
    await expect(resolvedAt).toHaveValue("2026-11-10T14:00");
    await expect(
        inverter.getByRole("button", {
            name: "Send for approval",
            exact: true,
        }),
    ).toHaveCount(0);
    const close = inverter.getByRole("button", { name: "Close", exact: true });
    await expect(close).toBeFocused();

    held = null;
    await readOnFocus(page);
    await expect(pending).toHaveCount(0);
    await expect(resolvedAt).toHaveValue("2026-11-10T14:00");
    await expect(
        inverter.getByRole("button", {
            name: "Send for approval",
            exact: true,
        }),
    ).toHaveCount(0);
    await close.click();
    await expect(resolvedAt).toHaveCount(0);
    await readOnFocus(page);
    await expect(
        inverter.getByRole("button", {
            name: "Submit resolution",
            exact: true,
        }),
    ).toBeVisible();
    await expect(resolvedAt).toHaveCount(0);
    expect(submissions).toHaveLength(0);
});

test("a conflict answer that already carries the member's own request holds the editor before the list reloads", async ({
    page,
}) => {
    const reload = gate();
    let conflicted = false;
    const submissions: unknown[] = [];
    const current: OutageRow = {
        ...listedOutage(4, "Changed meanwhile", 9),
        revision: 3,
    };
    await memberOutages(page, async () => {
        if (!conflicted) return memberList();
        await reload.pass();
        return { ...memberList(4), results: [current] };
    });
    await page.route("**/change-requests/incidents/", async (route) => {
        submissions.push(route.request().postDataJSON());
        conflicted = true;
        await route.fulfill({
            status: 409,
            json: answer("IncidentConflict", {
                ...errorBody(
                    "incident_revision_changed",
                    "The outage changed. Review its current values and save again.",
                ),
                current,
            }),
        });
    });
    await openOutages(page);
    await outageCard(page, "Inverter offline")
        .getByRole("button", { name: "Suggest changes", exact: true })
        .click();
    const editor = page.getByRole("region", { name: "Edit outage" });
    await editor.getByLabel("Incident title").fill("Inverter replaced");
    await editor
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await reload.reached;

    await expect(
        editor.getByRole("alert").filter({
            hasText: "Someone changed this outage after you opened it.",
        }),
    ).toBeVisible();
    await expect(
        editor.getByRole("status").filter({ hasText: heldText }),
    ).toBeVisible();
    await expect(
        editor.getByRole("button", { name: "Send for approval", exact: true }),
    ).toHaveCount(0);
    await expect(editor.getByLabel("Incident title")).toHaveValue(
        "Inverter replaced",
    );
    reload.open();
    await expect(
        page.getByRole("link", {
            name: "Waiting for approval: view the request for Changed meanwhile",
        }),
    ).toBeVisible();
    await expect(
        editor.getByRole("button", { name: "Send for approval", exact: true }),
    ).toHaveCount(0);
    expect(submissions).toHaveLength(1);
});

function memberPage(
    results: OutageRow[],
    reason: string | null = null,
): ReturnType<typeof memberList> {
    return answer("IncidentPage", {
        ...paged(results),
        can_propose: reason === null,
        proposal_unavailable_reason: reason,
    });
}

// Another tab saves the session's language; this tab reads the session again when its window regains focus.
async function sessionLanguage(page: Page) {
    let language = "en";
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: { ...user, language_preference: language },
                language,
                csrf_token: "signed-token",
            },
        }),
    );
    return async (next: string) => {
        language = next;
        await page.clock.runFor(5_000);
        await page.evaluate(() => window.dispatchEvent(new Event("focus")));
        await expect(page.locator("html")).toHaveAttribute("lang", next);
        await expect(
            page.getByRole("heading", { name: "Santral kesintileri" }),
        ).toBeVisible();
    };
}

const cancelledText =
    "This outage is now Cancelled, so your change cannot be applied.";

function cancelledFour(): OutageRow {
    return {
        ...listedOutage(4, "Inverter offline", null),
        revision: 3,
        status: "cancelled",
    };
}

test("a language change keeps what was typed into an open resolution and where the focus was", async ({
    page,
}) => {
    await memberOutages(page, () => memberList());
    const speak = await sessionLanguage(page);
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    await inverter
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    await inverter
        .getByLabel("Resolved at", { exact: true })
        .fill("2026-11-10T14:00");
    await inverter
        .getByLabel("Reason (optional)", { exact: true })
        .fill("Inverter restarted");

    await speak("tr");
    await expect(
        inverter.getByLabel("Bitiş zamanı", { exact: true }),
    ).toHaveValue("2026-11-10T14:00");
    const reason = inverter.getByLabel("Gerekçe (isteğe bağlı)", {
        exact: true,
    });
    await expect(reason).toHaveValue("Inverter restarted");
    await expect(reason).toBeFocused();
});

test("a language change while a resolution is sent keeps it sending, sends it once and reports no success before the answer", async ({
    page,
}) => {
    const answered = gate();
    const submissions: unknown[] = [];
    await memberOutages(page, () => memberList(submissions.length ? 4 : null));
    const speak = await sessionLanguage(page);
    await page.route("**/change-requests/incidents/", async (route) => {
        submissions.push(route.request().postDataJSON());
        await answered.pass();
        await route.fulfill({ status: 201, json: {} });
    });
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    await inverter
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    await inverter
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await answered.reached;

    await speak("tr");
    await expect(
        inverter.getByRole("button", { name: "Kaydediliyor…", exact: true }),
    ).toBeDisabled();
    await expect(
        inverter.getByRole("button", { name: "Onaya gönder", exact: true }),
    ).toHaveCount(0);
    await expect(page.getByText("Kaydedildi.", { exact: true })).toHaveCount(0);
    answered.open();
    await expect(page.getByText(/^Onaya gönderildi\./)).toBeVisible();
    await expect(page.getByText("Kaydedildi.", { exact: true })).toHaveCount(0);
    expect(submissions).toHaveLength(1);
});

test("an open resolution whose outage someone else cancels stays in view, held, with what was typed", async ({
    page,
}) => {
    let cancelled = false;
    await memberOutages(page, () =>
        cancelled
            ? memberPage([
                  cancelledFour(),
                  listedOutage(5, "Export limited", null),
              ])
            : memberList(),
    );
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    await inverter
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    const resolvedAt = inverter.getByLabel("Resolved at", { exact: true });
    await resolvedAt.fill("2026-11-10T14:00");

    cancelled = true;
    await readOnFocus(page);
    await expect(
        inverter.getByRole("status").filter({ hasText: cancelledText }),
    ).toBeVisible();
    await expect(resolvedAt).toBeVisible();
    await expect(resolvedAt).toHaveValue("2026-11-10T14:00");
    await expect(
        inverter.getByRole("button", {
            name: "Send for approval",
            exact: true,
        }),
    ).toHaveCount(0);
    await expect(
        inverter.getByRole("button", { name: "Close", exact: true }),
    ).toBeFocused();
});

test("an open cancellation whose outage someone else cancels stays in view, held, with what was typed", async ({
    page,
}) => {
    let cancelled = false;
    await memberOutages(page, () =>
        cancelled
            ? memberPage([
                  cancelledFour(),
                  listedOutage(5, "Export limited", null),
              ])
            : memberList(),
    );
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    await inverter.getByText("Request cancellation", { exact: true }).click();
    const reason = inverter.getByLabel("Reason (optional)", { exact: true });
    await reason.fill("Entered twice");
    const confirmation = inverter.getByLabel(
        "I confirm this incident was recorded incorrectly.",
    );
    await confirmation.check();

    cancelled = true;
    await readOnFocus(page);
    await expect(
        inverter.getByRole("status").filter({ hasText: cancelledText }),
    ).toBeVisible();
    await expect(reason).toBeVisible();
    await expect(reason).toHaveValue("Entered twice");
    await expect(confirmation).toBeChecked();
    await expect(
        inverter.getByRole("button", {
            name: "Send for approval",
            exact: true,
        }),
    ).toHaveCount(0);
    await expect(
        inverter.getByText("Request cancellation", { exact: true }),
    ).toBeFocused();
});

test("an open resolution whose outage leaves the list stays in view, held, until it is closed", async ({
    page,
}) => {
    let removed = false;
    await memberOutages(page, () =>
        removed
            ? memberPage([listedOutage(5, "Export limited", null)])
            : memberList(),
    );
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    await inverter
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    const resolvedAt = inverter.getByLabel("Resolved at", { exact: true });
    await resolvedAt.fill("2026-11-10T14:00");

    removed = true;
    await readOnFocus(page);
    await expect(
        inverter.getByRole("status").filter({
            hasText:
                "This outage is no longer listed, so your change cannot be applied.",
        }),
    ).toBeVisible();
    await expect(resolvedAt).toHaveValue("2026-11-10T14:00");
    await expect(
        inverter.getByRole("button", {
            name: "Send for approval",
            exact: true,
        }),
    ).toHaveCount(0);
    const close = inverter.getByRole("button", { name: "Close", exact: true });
    await expect(close).toBeFocused();
    await close.click();
    await expect(inverter).toHaveCount(0);
    await expect(
        page.getByRole("heading", { name: "Plant outages" }),
    ).toBeFocused();
});

test("a conflict whose current version is cancelled keeps its messages in view after the reload, and discarding it closes the form", async ({
    page,
}) => {
    const reload = gate();
    let conflicted = false;
    const current = cancelledFour();
    await memberOutages(page, async () => {
        if (!conflicted) return memberList();
        await reload.pass();
        return memberPage([current, listedOutage(5, "Export restored", null)]);
    });
    await page.route("**/change-requests/incidents/", async (route) => {
        conflicted = true;
        await route.fulfill({
            status: 409,
            json: answer("IncidentConflict", {
                ...errorBody(
                    "incident_revision_changed",
                    "The outage changed. Review its current values and save again.",
                ),
                current,
            }),
        });
    });
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    await inverter.getByText("Request cancellation", { exact: true }).click();
    const reason = inverter.getByLabel("Reason (optional)", { exact: true });
    await reason.fill("Entered twice");
    await inverter
        .getByLabel("I confirm this incident was recorded incorrectly.")
        .check();
    await inverter
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await reload.reached;
    reload.open();
    await expect(
        page.getByRole("heading", { name: "Export restored" }),
    ).toBeVisible();

    await expect(
        inverter.getByRole("alert").filter({
            hasText: "Someone changed this outage after you opened it.",
        }),
    ).toBeVisible();
    await expect(
        inverter.getByRole("status").filter({ hasText: cancelledText }),
    ).toBeVisible();
    await expect(reason).toHaveValue("Entered twice");
    await inverter
        .getByRole("button", { name: "Discard my change", exact: true })
        .click();
    await expect(reason).toHaveCount(0);
});

test("closing or sending a workflow returns the focus to the control that opened it, or else to its outage's heading", async ({
    page,
}) => {
    let sent = false;
    await memberOutages(page, () => memberList(sent ? 4 : null));
    await page.route("**/change-requests/incidents/", (route) => {
        sent = true;
        return route.fulfill({ status: 201, json: {} });
    });
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    const other = outageCard(page, "Export limited");
    const resolve = inverter.getByRole("button", {
        name: "Submit resolution",
        exact: true,
    });
    await resolve.click();
    await inverter.getByRole("button", { name: "Close", exact: true }).click();
    await expect(resolve).toBeFocused();

    const suggest = other.getByRole("button", {
        name: "Suggest changes",
        exact: true,
    });
    await suggest.click();
    await page
        .getByRole("region", { name: "Edit outage" })
        .getByRole("button", { name: "Close editor", exact: true })
        .click();
    await expect(suggest).toBeFocused();

    const propose = page.getByRole("button", {
        name: "Submit outage",
        exact: true,
    });
    await propose.click();
    await page
        .getByRole("region", { name: "Report outage" })
        .getByRole("button", { name: "Close editor", exact: true })
        .click();
    await expect(propose).toBeFocused();

    await resolve.click();
    await inverter
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await expect(page.getByText(/^Sent for approval\./)).toBeVisible();
    await expect(resolve).toHaveCount(0);
    await expect(
        inverter.getByRole("heading", { name: "Inverter offline" }),
    ).toBeFocused();
});

test("the limit message is in the page once while it holds an open editor", async ({
    page,
}) => {
    const limitText =
        "Too many of your requests for this plant await approval. Withdraw one or wait for a decision before proposing another outage or a change to one.";
    let limited = false;
    await memberOutages(page, () =>
        limited
            ? memberPage(
                  [
                      listedOutage(4, "Inverter offline", null),
                      listedOutage(5, "Export limited", null),
                  ],
                  "pending_request_limit",
              )
            : memberList(),
    );
    await openOutages(page);
    await outageCard(page, "Inverter offline")
        .getByRole("button", { name: "Suggest changes", exact: true })
        .click();
    const editor = page.getByRole("region", { name: "Edit outage" });
    limited = true;
    await readOnFocus(page);
    await expect(editor.getByRole("status")).toHaveText(limitText);
    await expect(page.getByText(limitText, { exact: true })).toHaveCount(1);
    await editor
        .getByRole("button", { name: "Close editor", exact: true })
        .click();
    await expect(editor).toHaveCount(0);
    await expect(page.getByText(limitText, { exact: true })).toHaveCount(1);
});

test("an administrator who loses direct access with an editor open on an outage holding their request is held at once", async ({
    page,
}) => {
    let role: "admin" | "viewer" = "admin";
    await memberOutages(page, () => memberList(4));
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([tenantRow({ id: 1, name: "Workspace", role })]),
        }),
    );
    await openOutages(page);
    await outageCard(page, "Inverter offline")
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    const editor = page.getByRole("region", { name: "Edit outage" });
    await editor.getByLabel("Incident title").fill("Inverter replaced");
    await expect(
        editor.getByRole("button", {
            name: "Save outage changes",
            exact: true,
        }),
    ).toBeEnabled();

    role = "viewer";
    await page.clock.runFor(120_001);
    await expect(
        editor.getByRole("status").filter({ hasText: heldText }),
    ).toBeVisible();
    await expect(
        editor.getByRole("button", { name: "Send for approval", exact: true }),
    ).toHaveCount(0);
    await expect(editor.getByLabel("Incident title")).toHaveValue(
        "Inverter replaced",
    );
});

test("a resolution whose outage someone else cancels while it is sent stays held with what was typed once the send fails", async ({
    page,
}) => {
    const answered = gate();
    let cancelled = false;
    await memberOutages(page, () =>
        cancelled
            ? memberPage([
                  cancelledFour(),
                  listedOutage(5, "Export limited", null),
              ])
            : memberList(),
    );
    await page.route("**/change-requests/incidents/", async (route) => {
        await answered.pass();
        await route.fulfill({
            status: 503,
            json: errorBody("busy", "The service is busy."),
        });
    });
    await openOutages(page);
    const inverter = outageCard(page, "Inverter offline");
    await inverter
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    const resolvedAt = inverter.getByLabel("Resolved at", { exact: true });
    await resolvedAt.fill("2026-11-10T14:00");
    await inverter
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await answered.reached;

    cancelled = true;
    await readOnFocus(page);
    await expect(
        inverter.getByText("Cancelled", { exact: true }),
    ).toBeVisible();
    answered.open();
    await expect(
        inverter.getByRole("button", { name: "Saving…", exact: true }),
    ).toHaveCount(0);
    await expect(
        inverter.getByRole("status").filter({ hasText: cancelledText }),
    ).toBeVisible();
    await expect(resolvedAt).toHaveValue("2026-11-10T14:00");
    await expect(
        inverter.getByRole("button", {
            name: "Send for approval",
            exact: true,
        }),
    ).toHaveCount(0);
});

test("an administrator demoted while a failing save is sent is held on an outage holding their request", async ({
    page,
}) => {
    const answered = gate();
    let role: "admin" | "viewer" = "admin";
    await memberOutages(page, () => memberList(4));
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([tenantRow({ id: 1, name: "Workspace", role })]),
        }),
    );
    await page.route("**/outages/4/", async (route) => {
        await answered.pass();
        await route.fulfill({
            status: 403,
            json: errorBody(
                "workspace_admin_required",
                "Only a workspace administrator can change outages.",
            ),
        });
    });
    await openOutages(page);
    await outageCard(page, "Inverter offline")
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    const editor = page.getByRole("region", { name: "Edit outage" });
    await editor.getByLabel("Incident title").fill("Inverter replaced");
    await editor
        .getByRole("button", { name: "Save outage changes", exact: true })
        .click();
    await answered.reached;

    role = "viewer";
    await readOnFocus(page);
    await expect(
        page.getByRole("button", { name: "Submit outage", exact: true }),
    ).toBeVisible();
    answered.open();
    await expect(
        editor.getByRole("button", { name: "Saving…", exact: true }),
    ).toHaveCount(0);
    await expect(
        editor.getByRole("status").filter({ hasText: heldText }),
    ).toBeVisible();
    await expect(
        editor.getByRole("button", { name: "Send for approval", exact: true }),
    ).toHaveCount(0);
    await expect(editor.getByLabel("Incident title")).toHaveValue(
        "Inverter replaced",
    );
});

test("a conflict whose reload repeats the limit already listed while the change was sent holds the editor", async ({
    page,
}) => {
    const answered = gate();
    let limited = false;
    const current: OutageRow = {
        ...listedOutage(4, "Changed meanwhile", null),
        revision: 3,
    };
    await memberOutages(page, () =>
        limited
            ? memberPage(
                  [current, listedOutage(5, "Export limited", null)],
                  "pending_request_limit",
              )
            : memberList(),
    );
    await page.route("**/change-requests/incidents/", async (route) => {
        await answered.pass();
        await route.fulfill({
            status: 409,
            json: answer("IncidentConflict", {
                ...errorBody(
                    "incident_revision_changed",
                    "The outage changed. Review its current values and save again.",
                ),
                current,
            }),
        });
    });
    await openOutages(page);
    await outageCard(page, "Inverter offline")
        .getByRole("button", { name: "Suggest changes", exact: true })
        .click();
    const editor = page.getByRole("region", { name: "Edit outage" });
    await editor.getByLabel("Incident title").fill("Inverter replaced");
    await editor
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await answered.reached;

    limited = true;
    await readOnFocus(page);
    await expect(
        page.getByRole("heading", { name: "Changed meanwhile" }),
    ).toBeVisible();
    answered.open();
    await expect(
        editor.getByRole("alert").filter({
            hasText: "Someone changed this outage after you opened it.",
        }),
    ).toBeVisible();
    await expect(
        editor.getByRole("status").filter({
            hasText: "Too many of your requests for this plant await approval.",
        }),
    ).toBeVisible();
    await expect(
        editor.getByRole("button", { name: "Send for approval", exact: true }),
    ).toHaveCount(0);
    await expect(editor.getByLabel("Incident title")).toHaveValue(
        "Inverter replaced",
    );
});
