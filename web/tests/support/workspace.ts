import { expect, type Page } from "@playwright/test";

export const region = {
    id: 11,
    name: "Antalya solar park",
    latitude: 36.9,
    longitude: 30.7,
    timezone: "Europe/Istanbul",
    surface: "horizontal",
    revision: 1,
    status: "approved",
};
export const plant = {
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
export const user = {
    id: 1,
    username: "solar.user",
    email: "solar@example.test",
};
export const paged = (results: unknown[]) => ({
    results,
    count: results.length,
    next: null,
    previous: null,
});
export async function mockApi(
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
