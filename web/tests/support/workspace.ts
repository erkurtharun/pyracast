import { expect, type Page } from "@playwright/test";
import { errorBody } from "./errors";

import type { components } from "../../src/lib/schema";

type Schemas = components["schemas"];
type TenantRow = Schemas["TenantOutput"];
// A mocked answer may leave fields out; every field it sets has the type the API declares.
export type Mock<T> = T extends readonly (infer U)[]
    ? Mock<U>[]
    : T extends object
      ? { [K in keyof T]?: Mock<T[K]> }
      : T;
export const answer = <K extends keyof Schemas>(
    _schema: K,
    value: Mock<Schemas[K]>,
) => value;

export const tenantRow = (
    tenant: Pick<TenantRow, "id" | "name" | "role">,
    counts: Partial<Omit<TenantRow, "id" | "name" | "role">> = {},
): TenantRow => ({
    pending_review_count: tenant.role === "admin" ? 0 : null,
    unread_outcome_count: 0,
    ...tenant,
    ...counts,
});

export const region: Schemas["RegionOutput"] = {
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
        created_at: "2026-01-01T00:00:00Z",
        dc_capacity_mw: 5,
        inverter_capacity_mw: 4,
        export_limit_mw: null,
        measurement_basis: "net_export",
    },
} satisfies Mock<Schemas["PlantOutput"]>;
export const user = {
    id: 1,
    username: "solar.user",
    email: "solar@example.test",
    can_admin: false,
    mfa_required: false,
    has_usable_password: true,
} satisfies Mock<Schemas["SessionUserOutput"]>;
export const invitationPreview: Schemas["InvitationPreviewOutput"] = {
    workspace: "Sunfield Energy",
    role: "viewer",
    inviter: "owner@example.test",
    email: user.email,
    expires_at: "2026-12-01T00:00:00Z",
};
export const signedInSession = {
    id: 1,
    ip: "203.0.113.7",
    user_agent:
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    created_at: 1767225600,
    last_seen_at: 1767229200,
    is_current: true,
};
export const paged = <T>(results: T[]) => ({
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
    let submittedSite: Mock<Schemas["SiteOutput"]> | undefined;
    let submittedPlant: Mock<Schemas["PlantOutput"]> | undefined;
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
                signedIn ? "signed-token" : "anonymous-token",
            );
            signedIn = false;
            data = { status: 401, meta: { is_authenticated: false } };
        } else if (path === "/api/auth/browser/v1/config") {
            data = { status: 200, data: { socialaccount: { providers: [] } } };
        } else if (path === "/api/auth/browser/v1/account/providers") {
            data = { status: 200, data: [] };
        } else if (path === "/api/auth/browser/v1/account/authenticators") {
            data = { status: 200, data: admin ? [{ type: "totp" }] : [] };
        } else if (path === "/api/auth/browser/v1/account/email") {
            data = {
                status: 200,
                data: [{ email: user.email, primary: true, verified: true }],
            };
        } else if (path === "/api/auth/browser/v1/auth/sessions") {
            data = { status: 200, data: [signedInSession] };
        } else if (path === "/api/session/") {
            data = answer("SessionOutput", {
                user: signedIn
                    ? { ...user, can_admin: admin, mfa_required: admin }
                    : null,
                csrf_token: signedIn ? "signed-token" : "anonymous-token",
            });
        } else if (path === "/api/tenants/")
            data = answer(
                "PaginatedTenantOutputList",
                paged([
                    tenantRow({
                        id: 1,
                        name: "Sunfield Energy",
                        role: "admin",
                    }),
                ]),
            );
        else if (/\/api\/tenants\/\d+\/change-requests\/$/.test(path)) {
            data = answer("PaginatedChangeRequestSummaryOutputList", paged([]));
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
                    reminder: null,
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
                data = answer("SiteOutput", created);
            } else
                data = answer(
                    "PaginatedSiteOutputList",
                    paged(
                        submittedSite
                            ? [submittedSite]
                            : empty
                              ? []
                              : [{ ...region, plant, reminder: null }],
                    ),
                );
        } else if (path.endsWith("/plants/"))
            data =
                method === "POST"
                    ? plant
                    : answer(
                          "PaginatedPlantOutputList",
                          paged([submittedPlant ?? plant]),
                      );
        else if (path.endsWith("/production-source/"))
            data = answer("EpiasConnectionOutput", {
                connected: false,
                name: "",
                archived_days: 0,
                last_error: "",
            });
        else if (path.endsWith("/production/preview/")) {
            const body = route.request().postDataJSON() as {
                unit: string;
                observations: {
                    starts_at: string;
                    ends_at: string;
                    energy: number;
                }[];
            };
            data = answer("ProductionPreviewOutput", {
                warnings: [],
                changes: body.observations.map((row) => ({
                    ...row,
                    action: "set",
                    expected_revision_id: null,
                })),
                preview: body.observations.map((row) => ({
                    starts_at: row.starts_at,
                    ends_at: row.ends_at,
                    current_energy_mwh: null,
                    new_energy_mwh:
                        row.energy / (body.unit === "kWh" ? 1000 : 1),
                    corrected: false,
                })),
            });
        } else if (path.endsWith("/production/changes/")) {
            expect(route.request().postDataJSON()).toMatchObject({
                configuration_id: 31,
                unit: "kWh",
            });
            data = answer("ObservationImportOutput", {
                created: 1,
                existing: 0,
                count: 1,
                ids: [1],
            });
        } else if (path.endsWith("/production/"))
            data = answer("ProductionHistoryOutput", {
                timezone: region.timezone,
                available_start_date: null,
                available_end_date: null,
                start_date: "2026-01-01",
                end_date: "2026-01-07",
                days: [],
                intervals: [],
            });
        else if (path.endsWith("/calibration/automation/")) {
            if (method === "PATCH")
                automationEnabled = route.request().postDataJSON().enabled;
            data = answer("CalibrationAutomationOutput", {
                enabled: automationEnabled,
                authorized: automationEnabled,
                configuration_id: 31,
                configuration_current: true,
                next_check_at: null,
                disabled_cause: null,
                current_calibration: null,
                last_attempt: null,
            });
        } else if (path.endsWith("/calibrations/"))
            data =
                method === "POST"
                    ? answer("CalibrationOutput", {
                          id: 41,
                          configuration_id: 31,
                          status: "pending",
                          assessment: "",
                          factor: null,
                          result: {},
                          dataset_sha256: "",
                      })
                    : answer(
                          "PaginatedCalibrationSummaryOutputList",
                          paged([]),
                      );
        else if (path.endsWith("/reminders/"))
            data = answer("ReminderOutput", {
                enabled: false,
                state: "paused_by_user",
                revision: 0,
                language: "en",
                include_workspace_members: false,
                addresses: {
                    recipients: [user.email],
                    workspace_recipients: [user.email],
                },
                recipient_limit: 5,
                recipient_count: 1,
                workspace_member_count: 1,
                recipient_limit_exceeded: false,
                times: ["08:00"],
                daily_reminder_limit: 10,
                deliveries: [],
            });
        else if (path.endsWith("/location-change-requests/"))
            data = answer("PaginatedLocationChangeOutputList", paged([]));
        else if (path.endsWith("/performance/"))
            data = answer("PerformanceOutput", {
                start_date: "2026-01-01",
                end_date: "2026-01-01",
                matched_days: 0,
                requested_days: 1,
                metrics: null,
                baseline_metrics: null,
                days: [],
            });
        else if (path.endsWith("/forecast/"))
            data = path.includes("/plants/")
                ? answer("PlantForecastOutput", {
                      date: "2026-01-01",
                      timezone: region.timezone,
                      freshness: "missing",
                      result: null,
                      configuration_id: 31,
                      unavailable_reason: "forecast_unavailable",
                  })
                : answer("ForecastOutput", {
                      date: "2026-01-01",
                      timezone: region.timezone,
                      freshness: "missing",
                      result: null,
                      collected_at: null,
                      issued_at: null,
                  });
        else if (path === "/api/invitations/preview/") data = invitationPreview;
        else throw new Error(`Unmocked API: ${method} ${path}`);
        await route.fulfill({ json: data });
    });
}
