import { expect, test, type Page } from "./support/test";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";
import { mockApi, paged, region, user } from "./support/workspace";

type Saved = { id: number; energy: number; quality: string; source: string };
async function productionApi(page: Page, viewer = false) {
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    await mockApi(page);
    if (viewer)
        await page.route("**/api/tenants/", (route) =>
            route.fulfill({
                json: paged([{ id: 1, name: "Tenant", role: "viewer" }]),
            }),
        );
    const provider: (number | null)[] = Array.from({ length: 24 }, (_, hour) =>
        hour >= 7 && hour < 19 ? 1 : 0,
    );
    const saved = new Map<number, Saved>();
    const heads = new Map<number, number>();
    const writes: Record<string, unknown>[] = [];
    let id = 0;
    const state = {
        provider,
        saved,
        heads,
        writes,
        conflict: false,
        connected: true,
        total: undefined as number | null | undefined,
    };
    await page.route("**/production-source/", (route) =>
        route.fulfill({
            json: {
                connected: state.connected,
                name: state.connected ? "G4-BOR-1 GES" : "",
                archived_days: 3,
                last_error: "",
            },
        }),
    );
    await page.route("**/production/?*", (route) => {
        const url = new URL(route.request().url());
        const date = url.searchParams.get("start_date")!;
        const intervals = provider.map((energy, hour) => {
            const starts_at = `${date}T${String(hour).padStart(2, "0")}:00:00+03:00`;
            const ends_at = new Date(
                Date.parse(starts_at) + 3600000,
            ).toISOString();
            const observation = saved.get(hour);
            return {
                starts_at,
                ends_at,
                revision_id: heads.get(hour) ?? null,
                observation: observation
                    ? {
                          id: observation.id,
                          energy_mwh: observation.energy,
                          quality: observation.quality,
                          source: observation.source,
                      }
                    : null,
                effective: observation
                    ? {
                          energy_mwh: observation.energy,
                          quality: observation.quality,
                          source: observation.source,
                          source_kind: null,
                          provisional: false,
                      }
                    : energy === null
                      ? null
                      : {
                            energy_mwh: energy,
                            quality: "valid",
                            source: "epias",
                            source_kind: "settlement_injection",
                            provisional: false,
                        },
                provider_original:
                    energy === null
                        ? null
                        : {
                              starts_at,
                              ends_at,
                              energy_mwh: energy,
                              source_kind: "settlement_injection",
                              snapshot_id: 1,
                              collected_at: "2026-11-10T00:00:00Z",
                              provisional: false,
                          },
                reference_available: true,
                can_enter: !observation,
                unavailable_reason: null,
                can_edit: true,
                can_revert: Boolean(observation),
                edit_unavailable_reason: null,
            };
        });
        const complete = intervals.every((row) => row.effective != null);
        return route.fulfill({
            json: {
                timezone: region.timezone,
                plant_id: 21,
                configuration_id: 31,
                start_date: date,
                end_date: date,
                resolution_minutes: 60,
                available_start_date: "2026-01-01",
                available_end_date: date,
                days: [
                    {
                        date,
                        complete,
                        total_mwh:
                            state.total !== undefined
                                ? state.total
                                : complete
                                  ? intervals.reduce(
                                        (sum, row) =>
                                            sum +
                                            (row.effective?.energy_mwh ?? 0),
                                        0,
                                    )
                                  : null,
                        missing_count: intervals.filter(
                            (row) => row.effective == null,
                        ).length,
                    },
                ],
                intervals,
                observations: [],
            },
        });
    });
    await page.route("**/production/changes/", async (route) => {
        const body = route.request().postDataJSON();
        writes.push(body);
        if (state.conflict) {
            state.conflict = false;
            heads.set(8, 99);
            saved.set(8, {
                id: 99,
                energy: 4,
                quality: "valid",
                source: "meter",
            });
            await route.fulfill({
                status: 409,
                json: {
                    detail: "Production changed. Reload the current values before saving.",
                },
            });
            return;
        }
        for (const change of body.changes) {
            const hour = (new Date(change.starts_at).getUTCHours() + 3) % 24;
            expect(change.expected_revision_id).toBe(heads.get(hour) ?? null);
            heads.set(hour, ++id);
            if (change.action === "revert") saved.delete(hour);
            else
                saved.set(hour, {
                    id,
                    energy: change.energy / (body.unit === "kWh" ? 1000 : 1),
                    quality: change.quality,
                    source: change.source,
                });
        }
        await route.fulfill({
            json: {
                created: body.changes.length,
                existing: 0,
                count: body.changes.length,
                ids: [id],
            },
        });
    });
    return state;
}
async function openProduction(page: Page) {
    await page.goto("/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Actual production" }),
    ).toBeVisible();
}
const table = (page: Page) =>
    page.getByRole("table", { name: /Production intervals/ });
const row = (page: Page, interval: string) =>
    table(page)
        .getByRole("row")
        .filter({
            has: page.getByRole("rowheader", { name: interval, exact: true }),
        });

function oneInterval(energy: number) {
    return {
        timezone: region.timezone,
        start_date: "2026-11-09",
        end_date: "2026-11-09",
        native_intervals: true,
        days: [{ date: "2026-11-09", total_mwh: null }],
        observations: [],
        intervals: [
            {
                starts_at: "2026-11-09T08:00:00+03:00",
                ends_at: "2026-11-09T09:00:00+03:00",
                revision_id: null,
                observation: null,
                effective: {
                    energy_mwh: energy,
                    quality: "valid",
                    source: "epias",
                    source_kind: "settlement_injection",
                    aggregated: false,
                },
                provider_originals: [],
                can_edit: true,
                can_revert: false,
            },
        ],
    };
}

test("actual production defaults to generation hours and full day restores every zero and missing hour", async ({
    page,
}) => {
    const state = await productionApi(page, true);
    state.provider[8] = null;
    state.provider[10] = 0;
    state.connected = false;
    await openProduction(page);
    await expect(
        page.getByRole("button", { name: "Actuals", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await expect(
        page.getByRole("button", { name: "Comparison", exact: true }),
    ).toBeVisible();
    const fullDay = page.getByRole("checkbox", { name: "Show full day" });
    await expect(fullDay).not.toBeChecked();
    await expect(table(page).getByRole("row")).toHaveCount(13);
    await expect(row(page, "00:00–01:00")).toHaveCount(0);
    await expect(row(page, "07:00–08:00")).toBeVisible();
    await expect(row(page, "18:00–19:00")).toBeVisible();
    await expect(row(page, "10:00–11:00").getByRole("cell").first()).toHaveText(
        "0 Settlement",
    );
    await expect(
        row(page, "08:00–09:00").getByText("—", { exact: true }),
    ).toBeVisible();
    await fullDay.check();
    await expect(table(page).getByRole("row")).toHaveCount(25);
    await expect(row(page, "00:00–01:00").getByRole("cell").first()).toHaveText(
        "0 Settlement",
    );
    await expect(
        row(page, "08:00–09:00").getByText("—", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Total: —")).toBeVisible();
    await expect(page.getByRole("button", { name: /^Edit / })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Import CSV" })).toHaveCount(
        0,
    );
    await expect(page.getByText(/^Provider:/)).toHaveCount(0);
    await fullDay.uncheck();
    await expect(table(page).getByRole("row")).toHaveCount(13);
    await page
        .getByText("Source and correction details", { exact: true })
        .click();
    await expect(page.getByText("EPİAŞ · Not connected")).toBeVisible();
    await expect(
        page.getByText(/Saved production remains available/),
    ).toBeVisible();
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download CSV" }).click();
    const records = Papa.parse<Record<string, string>>(
        await readFile((await (await download).path())!, "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(records).toHaveLength(24);
    expect(records[8].energy).toBe("");
    expect(records[0]).toMatchObject({
        energy: "0",
        unit: "MWh",
        timezone: region.timezone,
    });
});

test("full day exposes night corrections and keeps the filter fixed until the draft closes", async ({
    page,
}) => {
    const state = await productionApi(page);
    await openProduction(page);
    const fullDay = page.getByRole("checkbox", { name: "Show full day" });
    await fullDay.check();
    await page
        .getByRole("button", { name: "Edit 00:00–01:00", exact: true })
        .click();
    await expect(fullDay).toBeDisabled();
    await page.getByLabel("Production for 00:00–01:00 (MWh)").fill("0.5");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(row(page, "00:00–01:00")).toContainText("0.5");
    await expect(fullDay).toBeEnabled();
    expect(state.writes[0]).toMatchObject({
        changes: [{ energy: 0.5 }],
    });
    await fullDay.uncheck();
    await expect(row(page, "00:00–01:00")).toContainText("Corrected");
    await expect(row(page, "19:00–20:00")).toHaveCount(0);
});

test("edits survive provider refresh and revert explicitly restores the latest provider value", async ({
    page,
}) => {
    const state = await productionApi(page);
    await openProduction(page);
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    const input = page.getByLabel("Production for 08:00–09:00 (MWh)");
    await input.fill("2,5");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(row(page, "08:00–09:00")).toContainText("2.5");
    await expect(row(page, "08:00–09:00")).toContainText("Corrected");
    await expect(row(page, "08:00–09:00")).toContainText("Provider: 1 MWh");
    await expect(
        page.getByRole("button", { name: "Edit 08:00–09:00", exact: true }),
    ).toBeFocused();
    await expect(page.getByText(/^Provider:/)).toHaveCount(1);
    state.provider[8] = 3;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(row(page, "08:00–09:00")).toContainText("Provider: 3 MWh");
    expect(state.saved.get(8)?.energy).toBe(2.5);
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Use provider value for 08:00–09:00" })
        .click();
    await expect(row(page, "08:00–09:00").getByRole("cell").first()).toHaveText(
        "3 Settlement",
    );
    await expect(page.getByText(/^Provider:/)).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Edit 08:00–09:00", exact: true }),
    ).toBeFocused();
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    await input.fill("0");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    expect(state.writes[2]).toMatchObject({
        changes: [{ expected_revision_id: 2, energy: 0 }],
    });
    await expect(row(page, "08:00–09:00")).toContainText("Corrected");
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Edit 08:00–09:00", exact: true }),
    ).toBeFocused();
});

test("generation filtering preserves native intervals and repeated local hours across daylight saving", async ({
    page,
}) => {
    await productionApi(page, true);
    const intervals = [
        ["00:00:00+02:00", "01:30:00+02:00", 0],
        ["01:30:00+02:00", "02:00:00+02:00", 1],
        ["02:00:00+02:00", "02:00:00+01:00", 0],
        ["02:00:00+01:00", "02:30:00+01:00", null],
        ["02:30:00+01:00", "03:00:00+01:00", 1],
        ["03:00:00+01:00", "23:00:00+01:00", 0],
    ].map(([start, end, energy]) => ({
        ...oneInterval(1).intervals[0],
        starts_at: `2026-10-25T${start}`,
        ends_at: `2026-10-25T${end}`,
        effective:
            energy === null
                ? null
                : {
                      ...oneInterval(1).intervals[0].effective,
                      energy_mwh: energy,
                  },
    }));
    await page.route("**/production/?*", (route) =>
        route.fulfill({
            json: {
                ...oneInterval(1),
                timezone: "Europe/Berlin",
                start_date: "2026-10-25",
                end_date: "2026-10-25",
                intervals,
            },
        }),
    );
    await openProduction(page);
    await expect(table(page).getByRole("row")).toHaveCount(5);
    await expect(row(page, "02:00 GMT+2–02:00 GMT+1")).toContainText(
        "0 Settlement",
    );
    await expect(row(page, "02:00 GMT+1–02:30 GMT+1")).toContainText("—");
    await page.getByRole("checkbox", { name: "Show full day" }).check();
    await expect(table(page).getByRole("row")).toHaveCount(7);
});

test("conflicting edits preserve attempted energy and require current values to be reviewed", async ({
    page,
}) => {
    const state = await productionApi(page);
    state.conflict = true;
    await openProduction(page);
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    const input = page.getByLabel("Production for 08:00–09:00 (MWh)");
    await input.fill("2.5");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(
        page.getByRole("alert").filter({ hasText: "Production changed." }),
    ).toBeVisible();
    await expect(input).toHaveValue("2.5");
    await expect(
        page.getByRole("button", { name: "Save", exact: true }),
    ).toBeDisabled();
    await page.getByRole("button", { name: "Reload current values" }).click();
    await expect(input).toHaveValue("2.5");
    await expect(row(page, "08:00–09:00")).toContainText("4");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(
        page.getByText("Production saved.", { exact: true }),
    ).toBeVisible();
    expect(state.writes[1]).toMatchObject({
        changes: [{ expected_revision_id: 99, energy: 2.5 }],
    });
});

test("manual-only days distinguish absent production from a measured all-zero day", async ({
    page,
}) => {
    const state = await productionApi(page);
    state.provider.fill(null);
    state.connected = false;
    await openProduction(page);
    await expect(page.getByText("No production data")).toBeVisible();
    await expect(table(page).getByRole("row")).toHaveCount(25);
    await expect(page.getByText("Total: 0 MWh", { exact: true })).toHaveCount(
        0,
    );
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    await page.getByLabel("Production for 08:00–09:00 (MWh)").fill("0");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(row(page, "08:00–09:00")).toContainText("Corrected");
    state.provider.fill(0);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.getByText("Total: 0 MWh", { exact: true })).toBeVisible();
    await expect(table(page).getByRole("row")).toHaveCount(25);
});

test("reloading a conflict preserves the draft when the native interval disappeared", async ({
    page,
}) => {
    const state = await productionApi(page);
    state.conflict = true;
    await openProduction(page);
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    await page.getByLabel("Production for 08:00–09:00 (MWh)").fill("2.5");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(
        page.getByRole("alert").filter({ hasText: "Production changed." }),
    ).toBeVisible();
    await page.route("**/production/?*", (route) =>
        route.fulfill({
            json: {
                timezone: region.timezone,
                start_date: "2026-11-09",
                end_date: "2026-11-09",
                native_intervals: true,
                days: [{ date: "2026-11-09", total_mwh: null }],
                observations: [],
                intervals: [
                    {
                        starts_at: "2026-11-09T09:00:00+03:00",
                        ends_at: "2026-11-09T10:00:00+03:00",
                        revision_id: null,
                        observation: null,
                        effective: {
                            energy_mwh: 1,
                            quality: "valid",
                            source: "epias",
                            source_kind: "settlement_injection",
                        },
                        provider_originals: [],
                        can_edit: true,
                        can_revert: false,
                    },
                ],
            },
        }),
    );
    await page.getByRole("button", { name: "Reload current values" }).click();
    await expect(
        page.getByLabel("Attempted production for 08:00–09:00 (MWh)"),
    ).toHaveValue("2.5");
    await expect(
        page.getByRole("alert").filter({
            hasText: "This interval changed and is no longer available.",
        }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Save", exact: true }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(
        page.getByRole("group", { name: "Production intervals" }),
    ).toBeFocused();
    await page
        .getByRole("button", { name: "Edit 09:00–10:00", exact: true })
        .click();
    await expect(
        page.getByLabel("Production for 09:00–10:00 (MWh)"),
    ).toHaveValue("1");
});

test("importing a different native interval preserves an open draft for recovery", async ({
    page,
}) => {
    const state = await productionApi(page);
    let imported = false;
    await page.route("**/production/?*", (route) => {
        if (!imported) return route.fallback();
        const history = oneInterval(0.25);
        history.intervals[0].ends_at = "2026-11-09T08:30:00+03:00";
        return route.fulfill({ json: history });
    });
    await page.route("**/production/changes/", (route) => {
        const body = route.request().postDataJSON();
        state.writes.push(body);
        imported = true;
        return route.fulfill({
            json: { created: 1, existing: 0, count: 1, ids: [1] },
        });
    });
    await openProduction(page);
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    await page.getByLabel("Production for 08:00–09:00 (MWh)").fill("2.5");
    await page.getByRole("button", { name: "Import CSV", exact: true }).click();
    await page.getByLabel("Production CSV").setInputFiles({
        name: "half-hour.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(
            "starts_at,ends_at,energy,quality,source\n2026-11-09T08:00:00+03:00,2026-11-09T08:30:00+03:00,250,valid,meter",
        ),
    });
    await page
        .getByRole("button", { name: "Preview import", exact: true })
        .click();
    await page.getByRole("button", { name: "Confirm import (1)" }).click();
    await expect(
        page.getByLabel("Attempted production for 08:00–09:00 (MWh)"),
    ).toHaveValue("2.5");
    await expect(
        page.getByRole("button", { name: "Save", exact: true }),
    ).toHaveCount(0);
    expect(state.writes).toHaveLength(1);
    expect(state.writes[0]).toMatchObject({
        changes: [{ ends_at: "2026-11-09T08:30:00+03:00", energy: 250 }],
    });
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page
        .getByRole("button", { name: "Edit 08:00–08:30", exact: true })
        .click();
    await expect(
        page.getByLabel("Production for 08:00–08:30 (MWh)"),
    ).toHaveValue("0.25");
});

test("a failed import refresh preserves the draft and releases an invalidated pending reload", async ({
    page,
}) => {
    const state = await productionApi(page);
    state.conflict = true;
    let failRefresh = false;
    let holdReload = false;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    let reloadRequested!: () => void;
    const pendingReload = new Promise<void>((resolve) => {
        reloadRequested = resolve;
    });
    await page.route("**/production/?*", async (route) => {
        if (failRefresh)
            return route.fulfill({
                status: 503,
                json: { detail: "Import refresh temporarily unavailable." },
            });
        if (!holdReload) return route.fallback();
        holdReload = false;
        reloadRequested();
        await gate;
        return route.fulfill({ json: oneInterval(9) });
    });
    await page.route("**/production/changes/", (route) => {
        const body = route.request().postDataJSON();
        if (body.changes[0].ends_at !== "2026-11-09T08:30:00+03:00")
            return route.fallback();
        failRefresh = true;
        return route.fulfill({
            json: { created: 1, existing: 0, count: 1, ids: [100] },
        });
    });
    await openProduction(page);
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    await page.getByLabel("Production for 08:00–09:00 (MWh)").fill("2.5");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(
        page.getByRole("alert").filter({ hasText: "Production changed." }),
    ).toBeVisible();
    holdReload = true;
    await page.getByRole("button", { name: "Reload current values" }).click();
    await pendingReload;
    await page.getByRole("button", { name: "Import CSV", exact: true }).click();
    await page.getByLabel("Production CSV").setInputFiles({
        name: "half-hour.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(
            "starts_at,ends_at,energy,quality,source\n2026-11-09T08:00:00+03:00,2026-11-09T08:30:00+03:00,250,valid,meter",
        ),
    });
    await page
        .getByRole("button", { name: "Preview import", exact: true })
        .click();
    await page.getByRole("button", { name: "Confirm import (1)" }).click();
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "Could not refresh saved production." }),
    ).toBeVisible();
    await expect(
        page.getByLabel("Production for 08:00–09:00 (MWh)"),
    ).toHaveValue("2.5");
    await expect(page.getByLabel("Day", { exact: true })).toBeEnabled();
    await expect(
        page.getByRole("button", { name: "Reload current values" }),
    ).toBeEnabled();
    await expect(
        page.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeEnabled();
    const lateResponse = page.waitForResponse(
        async (response) =>
            response.url().includes("/production/?") &&
            response.status() === 200 &&
            (await response.json()).intervals[0].effective.energy_mwh === 9,
    );
    release();
    await (await lateResponse).finished();
    await expect(
        page.getByLabel("Production for 08:00–09:00 (MWh)"),
    ).toHaveValue("2.5");
    failRefresh = false;
    await page.getByRole("button", { name: "Reload current values" }).click();
    await expect(row(page, "08:00–09:00")).toContainText("4");
    await expect(
        page.getByLabel("Production for 08:00–09:00 (MWh)"),
    ).toHaveValue("2.5");
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Edit 08:00–09:00", exact: true }),
    ).toBeFocused();
});

test("production polling pauses during editing and hidden documents and retains saved values on refresh failure", async ({
    page,
}) => {
    await page.clock.install({ time: new Date("2026-11-10T12:00:00Z") });
    const state = await productionApi(page);
    let requests = 0;
    let fail = false;
    await page.route("**/production/?*", (route) => {
        requests++;
        return fail
            ? route.fulfill({
                  status: 503,
                  json: { detail: "Temporary production read failure." },
              })
            : route.fallback();
    });
    await openProduction(page);
    await expect(table(page)).toBeVisible();
    let previous = requests;
    state.provider[8] = 4;
    await page.clock.fastForward(30_000);
    await expect(row(page, "08:00–09:00")).toContainText("4 Settlement");
    expect(requests).toBeGreaterThan(previous);
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    await page.getByLabel("Production for 08:00–09:00 (MWh)").fill("2.5");
    previous = requests;
    state.provider[8] = 7;
    await page.clock.fastForward(90_000);
    expect(requests).toBe(previous);
    await expect(
        page.getByLabel("Production for 08:00–09:00 (MWh)"),
    ).toHaveValue("2.5");
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.clock.fastForward(30_000);
    await expect(row(page, "08:00–09:00")).toContainText("7 Settlement");
    fail = true;
    await page.clock.fastForward(30_000);
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "Could not refresh saved production." }),
    ).toBeVisible();
    await expect(row(page, "08:00–09:00")).toContainText("7 Settlement");
    fail = false;
    previous = requests;
    await page.evaluate(() =>
        Object.defineProperty(document, "visibilityState", {
            configurable: true,
            value: "hidden",
        }),
    );
    await page.clock.fastForward(60_000);
    expect(requests).toBe(previous);
    await page.evaluate(() =>
        Object.defineProperty(document, "visibilityState", {
            configurable: true,
            value: "visible",
        }),
    );
    state.provider[8] = 8;
    await page.clock.fastForward(30_000);
    await expect(row(page, "08:00–09:00")).toContainText("8 Settlement");
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "Could not refresh saved production." }),
    ).toHaveCount(0);
});

test("a late production poll cannot replace a newer saved response", async ({
    page,
}) => {
    await page.clock.install({ time: new Date("2026-11-10T12:00:00Z") });
    await productionApi(page);
    let polls = 0;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    await openProduction(page);
    await expect(table(page)).toBeVisible();
    await page.route("**/production/?*", async (route) => {
        const current = ++polls;
        if (current === 1) await gate;
        return route.fulfill({ json: oneInterval(current === 1 ? 4 : 5) });
    });
    await page.clock.fastForward(30_000);
    await expect.poll(() => polls).toBe(1);
    await expect(row(page, "08:00–09:00")).toContainText("1 Settlement");
    await page.clock.fastForward(30_000);
    await expect(row(page, "08:00–09:00")).toContainText("5 Settlement");
    const lateResponse = page.waitForResponse(
        async (response) =>
            response.url().includes("/production/?") &&
            (await response.json()).intervals[0].effective.energy_mwh === 4,
    );
    release();
    await (await lateResponse).finished();
    await page.clock.runFor(20);
    await expect(row(page, "08:00–09:00")).toContainText("5 Settlement");
});

test("production uses the server total and exposes invalid saved quality", async ({
    page,
}) => {
    const state = await productionApi(page);
    state.total = null;
    state.saved.set(8, {
        id: 1,
        energy: 9,
        quality: "invalid",
        source: "meter",
    });
    state.heads.set(8, 1);
    await openProduction(page);
    await expect(page.getByText("Total: —")).toBeVisible();
    await expect(row(page, "08:00–09:00")).toContainText("Invalid");
    await page
        .getByRole("button", { name: "Edit 08:00–09:00", exact: true })
        .click();
    await expect(page.getByLabel("Data quality")).toHaveValue("invalid");
    await page.getByLabel("Data quality").selectOption("valid");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    expect(state.writes[0]).toMatchObject({
        changes: [{ quality: "valid", energy: 9 }],
    });
});

test("CSV preview reviews effective values and unit changes require a new preview", async ({
    page,
}) => {
    const state = await productionApi(page);
    await page.route("**/production/preview/", (route) => {
        const body = route.request().postDataJSON();
        return route.fulfill({
            json: {
                changes: body.observations.map(
                    (item: Record<string, unknown>) => ({
                        ...item,
                        action: "set",
                        expected_revision_id: null,
                    }),
                ),
                preview: body.observations.map(
                    (item: {
                        starts_at: string;
                        ends_at: string;
                        energy: number;
                    }) => ({
                        starts_at: item.starts_at,
                        ends_at: item.ends_at,
                        current_energy_mwh: 1,
                        new_energy_mwh:
                            item.energy / (body.unit === "kWh" ? 1000 : 1),
                        corrected: false,
                    }),
                ),
                warnings: [
                    {
                        code: "provider_hour_incomplete",
                        starts_at: "2026-11-09T08:00:00+03:00",
                        ends_at: "2026-11-09T09:00:00+03:00",
                    },
                ],
            },
        });
    });
    await openProduction(page);
    await page.getByRole("button", { name: "Import CSV", exact: true }).click();
    await page.getByLabel("Production CSV").setInputFiles({
        name: "production.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(
            "starts_at,ends_at,energy,quality,source\n2026-11-09T08:00:00+03:00,2026-11-09T08:30:00+03:00,250,valid,meter",
        ),
    });
    await page
        .getByRole("button", { name: "Preview import", exact: true })
        .click();
    await expect(
        page.getByRole("table", { name: "Production import preview" }),
    ).toContainText("0.25");
    await expect(page.getByText("Saved", { exact: true })).toHaveCount(0);
    expect(state.writes).toHaveLength(0);
    await expect(
        page.getByText(/This import only replaces part of the provider hour/),
    ).toBeVisible();
    await page
        .getByRole("combobox", { name: "Energy unit", exact: true })
        .selectOption("MWh");
    await expect(
        page.getByRole("button", { name: "Confirm import (1)" }),
    ).toHaveCount(0);
    await page
        .getByRole("button", { name: "Preview import", exact: true })
        .click();
    await expect(
        page.getByRole("table", { name: "Production import preview" }),
    ).toContainText("250");
    state.provider[8] = 2;
    await page.getByRole("button", { name: "Confirm import (1)" }).click();
    await expect(
        page.getByText(
            "Import complete: 1 created, 0 already present, 1 total.",
        ),
    ).toBeVisible();
    expect(state.writes[0]).toMatchObject({
        unit: "MWh",
        changes: [{ expected_revision_id: null, energy: 250 }],
    });
    expect(state.saved.get(8)?.energy).toBe(250);
});

test("native timeline preserves half-hour and offset-hour corrections with their provider bounds", async ({
    page,
}) => {
    const state = await productionApi(page);
    state.heads.set(9, 2);
    state.saved.set(9, {
        id: 2,
        energy: 0.75,
        quality: "valid",
        source: "meter",
    });
    await page.route("**/production/?*", (route) => {
        expect(
            new URL(route.request().url()).searchParams.get("native_intervals"),
        ).toBe("true");
        const date = "2026-11-09";
        const at = (time: string) => `${date}T${time}:00+03:00`;
        const provider = (
            start: string,
            end: string,
            energy: number,
            snapshot_id: number,
        ) => ({
            starts_at: at(start),
            ends_at: at(end),
            energy_mwh: energy,
            source_kind: "settlement_injection",
            snapshot_id,
        });
        const value = (energy_mwh: number, source_kind = "manual") => ({
            energy_mwh,
            quality: "valid",
            source: "meter",
            source_kind,
            aggregated: false,
        });
        const manual = (
            start: string,
            end: string,
            energy: number,
            id: number,
            provider_originals: ReturnType<typeof provider>[],
        ) => ({
            starts_at: at(start),
            ends_at: at(end),
            revision_id: id,
            observation: {
                id,
                energy_mwh: energy,
                quality: "valid",
                source: "meter",
            },
            effective: value(energy),
            provider_originals,
            can_edit: true,
            can_revert: true,
        });
        const gap = (start: string, end: string) => ({
            starts_at: at(start),
            ends_at: at(end),
            revision_id: null,
            observation: null,
            effective: null,
            provider_originals: [],
            can_edit: true,
            can_revert: false,
        });
        return route.fulfill({
            json: {
                timezone: region.timezone,
                start_date: date,
                end_date: date,
                native_intervals: true,
                days: [{ date, total_mwh: null }],
                intervals: [
                    {
                        starts_at: at("07:00"),
                        ends_at: at("08:00"),
                        revision_id: null,
                        observation: null,
                        effective: value(1, "settlement_injection"),
                        provider_originals: [provider("07:00", "08:00", 1, 1)],
                        can_edit: true,
                        can_revert: false,
                    },
                    manual("08:00", "08:30", 0.5, 1, [
                        provider("08:00", "09:00", 2, 2),
                    ]),
                    gap("08:30", "09:00"),
                    gap("09:00", "09:30"),
                    manual("09:30", "10:30", 0.75, 2, [
                        provider("09:00", "10:00", 3, 3),
                        provider("10:00", "11:00", 4, 4),
                    ]),
                ],
                observations: [],
            },
        });
    });
    await openProduction(page);
    await expect(
        page.getByRole("combobox", { name: "Interval", exact: true }),
    ).toHaveCount(0);
    await expect(table(page).getByRole("row")).toHaveCount(6);
    await expect(row(page, "07:00\u201308:00")).toContainText("Settlement");
    await expect(row(page, "08:00\u201308:30")).toContainText(
        "Provider 08:00\u201309:00: 2 MWh",
    );
    await expect(row(page, "08:30\u201309:00")).toContainText("\u2014");
    await expect(row(page, "09:30\u201310:30")).toContainText(
        "Provider 09:00\u201310:00: 3 MWh",
    );
    await expect(row(page, "09:30\u201310:30")).toContainText(
        "Provider 10:00\u201311:00: 4 MWh",
    );
    await page
        .getByRole("button", { name: "Edit 09:30\u201310:30", exact: true })
        .click();
    await page.getByLabel("Production for 09:30\u201310:30 (MWh)").fill("1");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(
        page.getByText("Production saved.", { exact: true }),
    ).toBeVisible();
    expect(state.writes[0]).toMatchObject({
        changes: [
            {
                starts_at: "2026-11-09T09:30:00+03:00",
                ends_at: "2026-11-09T10:30:00+03:00",
                expected_revision_id: 2,
                energy: 1,
            },
        ],
    });
});

test("changing dates hides the old production export and ignores late responses", async ({
    page,
}) => {
    await productionApi(page);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    let requested!: () => void;
    const pending = new Promise<void>((resolve) => {
        requested = resolve;
    });
    await page.route("**/production/?*", async (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        );
        if (date !== "2026-11-08") return route.fallback();
        requested();
        await gate;
        await route.fulfill({
            status: 503,
            json: { detail: "Late old date failure." },
        });
    });
    await openProduction(page);
    await expect(table(page)).toBeVisible();
    await page.getByLabel("Day", { exact: true }).fill("2026-11-08");
    await pending;
    await expect(
        page.getByRole("button", { name: "Download CSV" }),
    ).toHaveCount(0);
    await page.getByLabel("Day", { exact: true }).fill("2026-11-07");
    await expect(table(page)).toBeVisible();
    release();
    await expect(page.getByLabel("Day", { exact: true })).toHaveValue(
        "2026-11-07",
    );
    await expect(page.getByText("Late old date failure.")).toHaveCount(0);
});

test("failed production requests stop loading without exposing stale values", async ({
    page,
}) => {
    await productionApi(page);
    await page.route("**/production/?*", (route) =>
        route.fulfill({
            status: 503,
            json: { detail: "Production is temporarily unavailable." },
        }),
    );
    await openProduction(page);
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "Production is temporarily unavailable." }),
    ).toBeVisible();
    await expect(page.getByText("Loading production…")).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Download CSV" }),
    ).toHaveCount(0);
});

test("Turkish production at 320px keeps native corrections, decimal commas and import review usable", async ({
    page,
}, testInfo) => {
    const state = await productionApi(page);
    await page.setViewportSize({ width: 320, height: 780 });
    state.heads.set(13, 1);
    state.saved.set(13, {
        id: 1,
        energy: 0.5,
        quality: "valid",
        source: "meter",
    });
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: { ...user, language_preference: "tr" },
                language: "tr",
                csrf_token: "signed-token",
            },
        }),
    );
    await page.route("**/production/?*", (route) =>
        route.fulfill({
            json: {
                timezone: region.timezone,
                start_date: "2026-11-09",
                end_date: "2026-11-09",
                native_intervals: true,
                days: [{ date: "2026-11-09", total_mwh: null }],
                observations: [],
                intervals: [
                    {
                        starts_at: "2026-11-09T13:30:00+03:00",
                        ends_at: "2026-11-09T14:30:00+03:00",
                        revision_id: 1,
                        observation: {
                            id: 1,
                            quality: "valid",
                            source: "meter",
                            energy_mwh: 0.5,
                        },
                        effective: {
                            quality: "valid",
                            source: "meter",
                            source_kind: "manual",
                            energy_mwh: 0.5,
                            aggregated: false,
                        },
                        can_edit: true,
                        can_revert: true,
                        provider_originals: [
                            {
                                starts_at: "2026-11-09T13:00:00+03:00",
                                ends_at: "2026-11-09T14:00:00+03:00",
                                snapshot_id: 1,
                                energy_mwh: 1,
                            },
                            {
                                starts_at: "2026-11-09T14:00:00+03:00",
                                ends_at: "2026-11-09T15:00:00+03:00",
                                snapshot_id: 2,
                                energy_mwh: 2,
                            },
                        ],
                    },
                ],
            },
        }),
    );
    await page.goto("/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Üretim", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Gerçekleşen üretim" }),
    ).toBeVisible();
    const fullDay = page.getByRole("checkbox", { name: "Tüm günü göster" });
    await expect(fullDay).not.toBeChecked();
    await fullDay.check();
    await fullDay.uncheck();
    await expect(
        page.getByText("Sağlayıcı 13:00–14:00: 1 MWh", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("Sağlayıcı 14:00–15:00: 2 MWh", { exact: true }),
    ).toBeVisible();
    const noOverflow = async () =>
        expect(
            await page.evaluate(
                () => document.documentElement.scrollWidth <= window.innerWidth,
            ),
        ).toBe(true);
    await noOverflow();
    await page.screenshot({
        path: testInfo.outputPath("production-tr320-normal.png"),
        fullPage: true,
    });
    await page
        .getByRole("button", { name: "13:30–14:30 aralığını düzenle" })
        .click();
    await page.getByLabel("13:30–14:30 aralığının üretimi (MWh)").fill("0,75");
    await expect(
        page.getByRole("combobox", { name: "Veri durumu" }),
    ).toHaveValue("valid");
    await noOverflow();
    await page.screenshot({
        path: testInfo.outputPath("production-tr320-editor.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: "Kaydet", exact: true }).click();
    await expect(
        page.getByText("Üretim kaydedildi.", { exact: true }),
    ).toBeVisible();
    expect(state.writes[0]).toMatchObject({
        changes: [{ energy: 0.75, expected_revision_id: 1 }],
    });
    await page.getByRole("button", { name: "CSV aktar", exact: true }).click();
    await page.getByLabel("Üretim CSV dosyası", { exact: true }).setInputFiles({
        name: "production.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(
            "starts_at,ends_at,energy,quality,source\n2026-11-09T08:00:00+03:00,2026-11-09T09:00:00+03:00,250,valid,meter",
        ),
    });
    await page.getByRole("button", { name: "Aktarımı önizle" }).click();
    await expect(
        page.getByRole("button", { name: "Aktarımı onayla (1)" }),
    ).toBeVisible();
    await expect(page.getByText("Kaydedildi", { exact: true })).toHaveCount(0);
    await noOverflow();
    await page.screenshot({
        path: testInfo.outputPath("production-tr320-import.png"),
        fullPage: true,
    });
});
