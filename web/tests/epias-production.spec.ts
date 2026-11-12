import { expect, test } from "./support/test";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";

test("connects, displays partial reported production and disconnects", async ({
    page,
}) => {
    const paged = (results: unknown[]) => ({
        results,
        count: results.length,
        next: null,
        previous: null,
    });
    const region = {
        id: 11,
        name: "EPİAŞ region",
        latitude: 36.9,
        longitude: 30.7,
        timezone: "Europe/Istanbul",
        surface: "horizontal",
        revision: 1,
        status: "approved",
    };
    const configuration = {
        id: 31,
        revision: 1,
        binding_id: 1,
        created_at: "2026-01-01T00:00:00Z",
        dc_capacity_mw: 5,
        inverter_capacity_mw: 4,
        export_limit_mw: null,
        measurement_basis: "net_export",
    };
    const plant = {
        id: 21,
        name: "EPİAŞ plant",
        region_id: 11,
        created_at: "2026-01-01T00:00:00Z",
        current_configuration: configuration,
    };
    let connected = false;
    let requestedDate = "";
    let requestedSource = "";
    await page.route("**/api/**", async (route) => {
        const url = new URL(route.request().url()),
            path = url.pathname;
        let data: unknown;
        if (path === "/api/session/")
            data = {
                user: { id: 1, username: "user", email: "user@test.test" },
                csrf_token: "token",
            };
        else if (path === "/api/tenants/")
            data = paged([{ id: 1, name: "Tenant" }]);
        else if (path.endsWith("/sites/")) data = paged([region]);
        else if (path.endsWith("/plants/")) data = paged([plant]);
        else if (path.endsWith("/forecast/"))
            data = {
                timezone: region.timezone,
                freshness: "missing",
                result: null,
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
        else if (path.endsWith("/production/"))
            data = {
                timezone: region.timezone,
                available_start_date: null,
                available_end_date: null,
                start_date: "2026-01-01",
                end_date: "2026-01-01",
                days: [],
                intervals: [],
                observations: [],
            };
        else if (path.endsWith("/production-source/")) {
            if (route.request().method() === "PUT") {
                expect(route.request().postDataJSON()).toEqual({
                    provider_plant_id: 3154,
                    expected_eic: "40W000005011621Y",
                    meter_basis: "unknown",
                });
                connected = true;
            }
            if (route.request().method() === "DELETE") {
                connected = false;
                await route.fulfill({ status: 204 });
                return;
            }
            data = {
                connected,
                provider_plant_id: connected ? 3154 : null,
                settlement_plant_id: connected ? 55 : null,
                eic: connected ? "40W000005011621Y" : "",
                name: connected ? "G4-BOR-1 GES" : "",
                meter_basis: "unknown",
                last_success_at: null,
                last_error: "",
                pending_jobs: 0,
                archived_days: 1,
                first_settlement_date: connected ? "2025-12-01" : null,
                last_settlement_date: connected ? "2026-01-01" : null,
            };
        } else if (path.endsWith("/reported-production/")) {
            requestedDate = url.searchParams.get("date") ?? "";
            requestedSource = url.searchParams.get("data_source") ?? "auto";
            data = {
                date: requestedDate,
                timezone: "Europe/Istanbul",
                unit: "MWh",
                source_kind: "realtime_generation",
                available_sources: ["realtime_generation"],
                calibration_eligible: true,
                calibration_blocker: null,
                snapshot: {
                    id: 1,
                    configuration_id: 31,
                    collected_at: "2026-01-03T12:00:00Z",
                    available_at: "2026-01-03T12:00:00Z",
                    content_sha256: "x",
                    source_kind: "realtime_generation",
                    normalization_policy: "hour_start",
                    complete: requestedDate === "2026-01-03",
                },
                readings: Array.from({ length: 24 }, (_, hour) => {
                    const period_start = `${requestedDate}T${String(hour).padStart(2, "0")}:00:00+03:00`;
                    const missing =
                        requestedDate !== "2026-01-03" && hour === 8;
                    const energy =
                        requestedDate === "2026-01-03"
                            ? hour
                            : hour === 7
                              ? 1
                              : hour === 9
                                ? 2
                                : missing
                                  ? null
                                  : 0;
                    return {
                        provider_timestamp: missing ? null : period_start,
                        period_start,
                        period_end: new Date(
                            new Date(period_start).getTime() + 3600000,
                        ).toISOString(),
                        reported: !missing,
                        energy_mwh: energy,
                        solar_mwh: energy,
                    };
                }),
            };
            if (requestedSource === "auto" && requestedDate === "2026-01-03") {
                const original = data as {
                    snapshot: Record<string, unknown>;
                    readings: Record<string, unknown>[];
                };
                const realtime = {
                    ...original.snapshot,
                    complete: false,
                    content_sha256: "realtime-digest",
                };
                const settlement = {
                    ...original.snapshot,
                    id: 2,
                    source_kind: "settlement_injection",
                    complete: false,
                    content_sha256: "settlement-digest",
                };
                data = {
                    ...original,
                    source_kind: "mixed",
                    complete: true,
                    snapshot: settlement,
                    source_snapshots: [settlement, realtime],
                    readings: original.readings.map((row, hour) => ({
                        ...row,
                        source_kind:
                            hour < 12
                                ? "settlement_injection"
                                : "realtime_generation",
                        snapshot_id: hour < 12 ? 2 : 1,
                    })),
                };
            }
            if (
                requestedSource === "settlement_injection" &&
                requestedDate === "2026-01-01"
            )
                data = {
                    ...(data as object),
                    source_kind: "settlement_injection",
                    calibration_eligible: false,
                    calibration_blocker: "measurement_basis_mismatch",
                };
            else if (requestedSource === "settlement_injection")
                data = {
                    ...(data as object),
                    source_kind: "settlement_injection",
                    calibration_eligible: false,
                    snapshot: null,
                    readings: [],
                    calibration_blocker: "production_unavailable",
                };
        } else throw new Error(`Unmocked API: ${path}`);
        await route.fulfill({ json: data });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /EPİAŞ region/ }).click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Automatic production" }).click();
    await expect(
        page.getByRole("button", { name: "Latest settlement day" }),
    ).toHaveCount(0);
    await page.getByLabel("EPİAŞ plant ID").fill("3154");
    await page.getByLabel("Plant EIC").fill("40W000005011621Y");
    await page.getByRole("button", { name: "Connect EPİAŞ" }).click();
    await expect(page.getByText("G4-BOR-1 GES")).toBeVisible();
    await expect(
        page.getByText(
            "Archived settlement injection (UEVM): 2025-12-01 to 2026-01-01",
        ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Latest settlement day" }).click();
    await expect(page.getByLabel("Archive date")).toHaveValue("2026-01-01");
    await expect(page.getByLabel("Production source")).toHaveValue(
        "settlement_injection",
    );
    await expect
        .poll(() => [requestedDate, requestedSource])
        .toEqual(["2026-01-01", "settlement_injection"]);
    await expect(
        page.getByText(
            "Settlement injection requires the plant and connection measurement points to be net export.",
        ),
    ).toBeVisible();
    await page
        .getByLabel("Production source")
        .selectOption("realtime_generation");
    await page.getByLabel("Archive date").fill("2026-01-02");
    await expect.poll(() => requestedDate).toBe("2026-01-02");
    await expect(
        page.getByText("Incomplete provider hours — total unavailable"),
    ).toBeVisible();
    const table = page.getByRole("table", {
        name: "EPİAŞ reported hourly production",
    });
    await expect(
        table.getByRole("cell", { name: "1", exact: true }),
    ).toBeVisible();
    await expect(table.getByRole("cell", { name: "Missing" })).toBeVisible();
    await expect(table.getByRole("row")).toHaveCount(4);
    await expect(
        table.getByRole("cell", { name: "07:00–08:00", exact: true }),
    ).toBeVisible();
    await expect(
        table.getByRole("cell", { name: "09:00–10:00", exact: true }),
    ).toBeVisible();
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download CSV", exact: true })
        .click();
    const records = Papa.parse<Record<string, string>>(
        await readFile((await (await download).path())!, "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(records).toHaveLength(24);
    expect(records[0]).toMatchObject({
        energy_mwh: "0",
        source_kind: "realtime_generation",
        period_start: "2026-01-02T00:00:00+03:00",
    });
    expect(records[8]).toMatchObject({
        energy_mwh: "",
        reported: "false",
        provider_timestamp: "",
        period_start: "2026-01-02T08:00:00+03:00",
    });
    await page
        .getByLabel("Production source")
        .selectOption("settlement_injection");
    await expect.poll(() => requestedSource).toBe("settlement_injection");
    await expect(
        page.getByText("No EPİAŞ data has been archived for this day."),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Download CSV", exact: true }),
    ).toHaveCount(0);
    await page
        .getByLabel("Production source")
        .selectOption("realtime_generation");
    await expect.poll(() => requestedSource).toBe("realtime_generation");
    await expect(table.getByRole("cell", { name: "Missing" })).toBeVisible();
    await page.getByLabel("Archive date").fill("2026-01-03");
    await expect(
        page.getByText("Sum of provider-reported hours: 276 MWh"),
    ).toBeVisible();
    await expect(table.getByRole("row")).toHaveCount(24);
    await page.getByLabel("Production source").selectOption("auto");
    await expect(page.getByText(/Combined production/)).toBeVisible();
    await expect(
        page.getByText("Sum of provider-reported hours: 276 MWh"),
    ).toBeVisible();
    await expect(table.getByText("UEVM", { exact: true })).toHaveCount(11);
    await expect(table.getByText("Provisional", { exact: true })).toHaveCount(
        12,
    );
    await expect(
        page.getByText(/Reported production is provisional and may differ/),
    ).toBeVisible();
    await page.getByText("Acquisition details", { exact: true }).click();
    await expect(
        page.getByText("Payload digest: realtime-digest"),
    ).toBeVisible();
    await expect(
        page.getByText("Payload digest: settlement-digest"),
    ).toBeVisible();
    await page
        .getByLabel("Production source")
        .selectOption("realtime_generation");
    await page.getByLabel("Archive date").fill("2026-01-02");
    await expect(
        page.getByText("Incomplete provider hours — total unavailable"),
    ).toBeVisible();
    await expect(
        page.getByText(
            "Reported production is provisional and may differ from net export. Settlement injection (UEVM) is preferred. Eligible for calibration when valid interval coverage is complete.",
        ),
    ).toBeVisible();
    await page
        .getByText("Connection settings and collection status", { exact: true })
        .click();
    await page.getByRole("button", { name: "Disconnect collection" }).click();
    await expect(
        page.getByRole("button", { name: "Connect EPİAŞ" }),
    ).toBeVisible();
    await expect(
        table.getByRole("cell", { name: "1", exact: true }),
    ).toBeVisible();
});
