import { expect, type Page } from "@playwright/test";
import { errorBody } from "./errors";

import type { components } from "../../src/lib/schema";

type TenantRow = components["schemas"]["TenantOutput"];

export const tenantRow = (
    tenant: Pick<TenantRow, "id" | "name" | "role">,
    counts: Partial<Omit<TenantRow, "id" | "name" | "role">> = {},
): TenantRow => ({
    pending_review_count: tenant.role === "admin" ? 0 : null,
    unread_outcome_count: 0,
    ...tenant,
    ...counts,
});

export const region: components["schemas"]["RegionOutput"] = {
    id: 11,
    name: "Antalya solar park",
    latitude: 36.9,
    longitude: 30.7,
    timezone: "Europe/Istanbul",
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
    can_admin: false,
    mfa_required: false,
    has_usable_password: true,
};
export const paged = (results: unknown[]) => ({
    results,
    count: results.length,
    next: null,
    previous: null,
});
export async function mockApi(
    page: Page,
    { anonymous = false, empty = false, failed = false, admin = false } = {},
) {
    let signedIn = !anonymous;
    let automationEnabled = false;
    let submittedSite: unknown;
    let submittedPlant: unknown;
    await page.route("**/api/**", async (route) => {
        const path = new URL(route.request().url()).pathname,
            method = route.request().method();
        let data: unknown;
        if (path === "/api/auth/browser/v1/auth/session" && method === "GET") {
            data = {
                status: 401,
                data: { flows: [] },
                meta: { is_authenticated: false },
            };
        } else if (path === "/api/auth/browser/v1/auth/login") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "anonymous-token",
            );
            signedIn = true;
            data = { status: 200, meta: { is_authenticated: true } };
        } else if (
            path === "/api/auth/browser/v1/auth/session" &&
            method === "DELETE"
        ) {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            signedIn = false;
            data = { status: 401, meta: { is_authenticated: false } };
        } else if (path === "/api/auth/browser/v1/config") {
            data = { status: 200, data: { socialaccount: { providers: [] } } };
        } else if (path === "/api/auth/browser/v1/account/providers") {
            data = { status: 200, data: [] };
        } else if (path === "/api/auth/browser/v1/account/authenticators") {
            data = { status: 200, data: admin ? [{ type: "totp" }] : [] };
        } else if (path === "/api/session/") {
            data = {
                user: signedIn
                    ? { ...user, can_admin: admin, mfa_required: admin }
                    : null,
                csrf_token: signedIn ? "signed-token" : "anonymous-token",
            };
        } else if (path === "/api/tenants/")
            data = paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
            ]);
        else if (/\/api\/tenants\/\d+\/change-requests\/$/.test(path)) {
            data = paged([]);
        } else if (
            path === "/api/tenants/1/regions/" ||
            path === "/api/tenants/1/sites/"
        ) {
            if (failed) {
                await route.fulfill({
                    status: 503,
                    json: errorBody(
                        "busy",
                        "Forecast service is temporarily unavailable.",
                    ),
                });
                return;
            }
            if (method === "POST") {
                const created = {
                    ...region,
                    ...route.request().postDataJSON(),
                    status: "pending",
                    plant: {
                        ...plant,
                        ...route.request().postDataJSON(),
                        current_configuration: null,
                        submission: {
                            ...plant.current_configuration,
                            ...route.request().postDataJSON(),
                        },
                    },
                };
                submittedSite = created;
                submittedPlant = created.plant;
                data = created;
            } else
                data = paged(
                    submittedSite
                        ? [submittedSite]
                        : empty
                          ? []
                          : [{ ...region, plant }],
                );
        } else if (path.endsWith("/plants/"))
            data = method === "POST" ? plant : paged([submittedPlant ?? plant]);
        else if (path.endsWith("/production-source/"))
            data = {
                connected: false,
                name: "",
                archived_days: 0,
                last_error: "",
            };
        else if (path.endsWith("/production/preview/")) {
            const body = route.request().postDataJSON();
            data = {
                warnings: [],
                changes: body.observations.map(
                    (row: Record<string, unknown>) => ({
                        ...row,
                        action: "set",
                        expected_revision_id: null,
                    }),
                ),
                preview: body.observations.map(
                    (row: {
                        starts_at: string;
                        ends_at: string;
                        energy: number;
                    }) => ({
                        starts_at: row.starts_at,
                        ends_at: row.ends_at,
                        current_energy_mwh: null,
                        new_energy_mwh:
                            row.energy / (body.unit === "kWh" ? 1000 : 1),
                        corrected: false,
                    }),
                ),
            };
        } else if (path.endsWith("/production/changes/")) {
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
        else if (path.endsWith("/calibration/automation/")) {
            if (method === "PATCH")
                automationEnabled = route.request().postDataJSON().enabled;
            data = {
                enabled: automationEnabled,
                authorized: automationEnabled,
                configuration_id: automationEnabled ? 31 : null,
                next_check_at: null,
                last_attempt: null,
            };
        } else if (path.endsWith("/calibrations/"))
            data =
                method === "POST"
                    ? {
                          id: 41,
                          status: "pending",
                          plant: {
                              ...plant,
                              ...route.request().postDataJSON(),
                              current_configuration: null,
                              submission: {
                                  ...plant.current_configuration,
                                  ...route.request().postDataJSON(),
                              },
                          },
                          assessment: "pending",
                          factor: null,
                          result: {},
                          dataset_sha256: "",
                      }
                    : paged([]);
        else if (path.endsWith("/reminders/"))
            data = {
                enabled: false,
                language: "en",
                recipients: [user.email],
                include_workspace_members: false,
                workspace_recipients: [user.email],
                recipient_limit: 5,
                recipient_count: 1,
                recipient_slots_remaining: 4,
                recipient_limit_exceeded: false,
                times: ["08:00"],
                effective_recipients: [user.email],
                timezone: region.timezone,
                quota: { daily_reminder_limit: 10, queries_per_email: 0 },
                deliveries: [],
            };
        else if (path.endsWith("/location-change-requests/")) data = paged([]);
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
