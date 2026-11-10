import { expect, test } from "@playwright/test";

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
        else if (path.endsWith("/regions/")) data = paged([region]);
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
                eic: connected ? "40W000005011621Y" : "",
                name: connected ? "G4-BOR-1 GES" : "",
                meter_basis: "unknown",
                last_success_at: null,
                last_error: "",
                pending_jobs: 0,
                archived_days: 1,
            };
        } else if (path.endsWith("/reported-production/")) {
            requestedDate = url.searchParams.get("date") ?? "";
            data = {
                date: requestedDate,
                timezone: "Europe/Istanbul",
                unit: "MWh",
                calibration_eligible: false,
                calibration_blocker: "measurement_contract_unverified",
                snapshot: {
                    id: 1,
                    configuration_id: 31,
                    collected_at: "2026-01-03T12:00:00Z",
                    available_at: "2026-01-03T12:00:00Z",
                    content_sha256: "x",
                },
                readings:
                    requestedDate === "2026-01-03"
                        ? Array.from({ length: 24 }, (_, hour) => ({
                              provider_timestamp: `${requestedDate}T${String(hour).padStart(2, "0")}:00:00+03:00`,
                              energy_mwh: hour,
                              solar_mwh: hour,
                          }))
                        : [
                              {
                                  provider_timestamp: `${requestedDate}T00:00:00+03:00`,
                                  energy_mwh: 0,
                                  solar_mwh: 0,
                              },
                              {
                                  provider_timestamp: `${requestedDate}T01:00:00+03:00`,
                                  energy_mwh: null,
                                  solar_mwh: null,
                              },
                          ],
            };
        } else throw new Error(`Unmocked API: ${path}`);
        await route.fulfill({ json: data });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /EPİAŞ region/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /EPİAŞ plant/ }).click();
    await page.getByRole("button", { name: "Automatic production" }).click();
    await page.getByLabel("EPİAŞ plant ID").fill("3154");
    await page.getByLabel("Plant EIC").fill("40W000005011621Y");
    await page.getByRole("button", { name: "Connect EPİAŞ" }).click();
    await expect(page.getByText("G4-BOR-1 GES")).toBeVisible();
    await page.getByLabel("Archive date").fill("2026-01-02");
    await expect.poll(() => requestedDate).toBe("2026-01-02");
    await expect(
        page.getByText("Incomplete provider hours — total unavailable"),
    ).toBeVisible();
    const table = page.getByRole("table", {
        name: "EPİAŞ reported hourly production",
    });
    await expect(
        table.getByRole("cell", { name: "0", exact: true }),
    ).toBeVisible();
    await expect(table.getByRole("cell", { name: "Missing" })).toBeVisible();
    await page.getByLabel("Archive date").fill("2026-01-03");
    await expect(
        page.getByText("Sum of provider-reported hours: 276 MWh"),
    ).toBeVisible();
    await expect(table.getByRole("row")).toHaveCount(25);
    await page.getByLabel("Archive date").fill("2026-01-02");
    await expect(
        page.getByText("Incomplete provider hours — total unavailable"),
    ).toBeVisible();
    await expect(
        page.getByText(
            "Calibration is waiting for confirmation of the time interval convention and measurement point.",
        ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Disconnect collection" }).click();
    await expect(
        page.getByRole("button", { name: "Connect EPİAŞ" }),
    ).toBeVisible();
    await expect(
        table.getByRole("cell", { name: "0", exact: true }),
    ).toBeVisible();
});
