import { expect, test, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { tenantRow } from "./support/workspace";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";
import { errorBody } from "./support/errors";

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
    let incident: Record<string, unknown> | null = null;
    let outageReads = 0;
    let conflict = false;
    const configuration = {
        id: 31,
        revision: 1,
        binding_id: 1,
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
                        submission: configured ? null : configuration,
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
            data = paged(incident ? [incident] : []);
        } else if (path.includes("/outages/")) {
            expect(route.request().headers()["x-csrftoken"]).toBe("token");
            const body = route.request().postDataJSON();
            if (conflict) {
                await route.fulfill({
                    status: 409,
                    json: errorBody(
                        "incident_revision_changed",
                        "This outage changed. Reload before editing.",
                    ),
                });
                return;
            }
            if (method === "POST" && path.endsWith("/outages/")) {
                expect(body.starts_at).toBe("2026-11-11T08:00:00.000Z");
                incident = {
                    ...body,
                    id: 51,
                    revision: 1,
                    declared_configuration_id: configurationChanged ? 30 : 31,
                    status: body.ends_at ? "resolved" : "active",
                    created_at: "2026-11-11T12:00:00Z",
                    updated_at: "2026-11-11T12:00:00Z",
                };
            } else {
                expect(body.expected_revision).toBe(incident?.revision);
                incident = {
                    ...incident,
                    ...body,
                    revision: Number(incident?.revision) + 1,
                };
                if (incident && path.endsWith("/resolve/"))
                    incident.status = "resolved";
                if (incident && path.endsWith("/cancel/"))
                    incident.status = "cancelled";
            }
            data = incident;
        } else if (path.endsWith("/forecast/")) {
            const intervals = [0, 2, 0].map((power, index) => ({
                start: `2026-11-12T0${index + 8}:00:00Z`,
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
        conflict: () => {
            conflict = true;
        },
    };
}

async function create(page: Page, impact = "Total outage") {
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Inverter disconnected");
    await page
        .getByLabel("Started at (device time)", { exact: true })
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
    expect(csv[1]).toMatchObject({
        normal_mean_power_mw: "2",
        affected_mean_power_mw: "0",
        normal_energy_mwh: "2",
        affected_energy_mwh: "0",
    });
    expect(csv[1]?.["outage_scenario.incidents"]).toContain(
        "Inverter disconnected",
    );
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
        .getByLabel("Resolved at (device time)", { exact: true })
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
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    state.conflict();
    await page.getByRole("button", { name: "Save outage changes" }).click();
    await expect(
        page.getByRole("alert").filter({ hasText: "This outage changed" }),
    ).toBeVisible();
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
        .getByLabel("Started at (device time)", { exact: true })
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
        .getByLabel("Started at (device time)", { exact: true })
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
    const resolvedAt = page.getByLabel("Resolved at (device time)", {
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
        .getByLabel("Resolved at (device time)", { exact: true })
        .fill("2026-11-11T14:00");
    await page.getByRole("button", { name: "Save resolution" }).click();
    await expect(tag).toHaveText("Resolved");
    await expect(tag).toHaveClass(/tag-neutral/);
});
