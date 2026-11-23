import { expect, type Page } from "@playwright/test";
import { errorBody } from "./errors";

import type {
    AddressRole,
    MemberRole,
    ReminderAnswer,
    Role,
} from "../../src/features/plant/reminder-addressing";
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
    reviewed_at: "2026-01-01T09:00:00Z",
    review_reason: null,
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
    parameters: {
        dc_capacity_mw: 5,
        inverter_capacity_mw: 4,
        export_limit_mw: null,
        measurement_basis: "net_export",
        mounting_type: "unknown",
        surface_tilt_degrees: null,
        surface_azimuth_degrees: null,
    },
    parameters_revision: 1,
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
                        parameters: {
                            ...plant.parameters,
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
            data = reminderAnswer(reminderStore({}), people([user.email]), {
                limit: 5,
                dailyLimit: 10,
                deliveries: [],
                administrator: true,
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
        else if (path.endsWith("/trash/regions/"))
            data = answer("PaginatedTrashedRegionOutputList", paged([]));
        else if (path === "/api/invitations/preview/") data = invitationPreview;
        else throw new Error(`Unmocked API: ${method} ${path}`);
        await route.fulfill({ json: data });
    });
}

type Delivery = Schemas["ReminderDeliveryOutput"];
type TypedChoice = { role: Role; members: number[] };
export type ReminderPerson = {
    membership: number;
    address: string;
    administrator?: boolean;
    active?: boolean;
    choice?: Role | null;
};
type ReminderStore = {
    enabled: boolean;
    recipients: string[];
    times: string[];
    language: Schemas["LanguageEnum"];
    include_workspace_members: boolean;
    hide_recipients: boolean;
    typed: Map<string, TypedChoice>;
    revision: number;
};
type ReminderPayload = Omit<Schemas["ReminderInput"], "address_roles"> & {
    address_roles?: AddressRole[];
    member_roles?: MemberRole[];
};
export type ReminderOptions = {
    recipients?: string[];
    limit?: number;
    enabled?: boolean;
    deliveries?: Partial<Delivery>[];
    dailyLimit?: number;
    members?: (string | ReminderPerson)[];
    includeMembers?: boolean;
    viewer?: () => boolean;
    hidden?: boolean;
    typed?: Record<string, Role | TypedChoice>;
};

const keyOf = (address: string) => address.trim().toLowerCase();
const sameIds = (left: number[], right: number[]) =>
    left.join() === [...right].sort((a, b) => a - b).join();

function people(members: (string | ReminderPerson)[]): ReminderPerson[] {
    return members.map((member, index) =>
        typeof member === "string"
            ? {
                  membership: index + 1,
                  address: member,
                  administrator: keyOf(member) === keyOf(user.email),
              }
            : member,
    );
}

function reminderStore(
    options: ReminderOptions,
    persons: ReminderPerson[] = [],
): ReminderStore {
    const recipients = options.recipients ?? [user.email];
    return {
        enabled: options.enabled ?? false,
        recipients,
        times: ["08:00"],
        language: "en",
        include_workspace_members: options.includeMembers ?? false,
        hide_recipients: options.hidden ?? false,
        typed: new Map(
            Object.entries(options.typed ?? {}).map(([address, choice]) => [
                keyOf(address),
                typeof choice === "string"
                    ? {
                          role: choice,
                          members: holders(persons, address).map(
                              (person) => person.membership,
                          ),
                      }
                    : choice,
            ]),
        ),
        revision: 0,
    };
}

function holders(persons: ReminderPerson[], address: string) {
    return persons
        .filter(
            (person) =>
                person.active !== false &&
                keyOf(person.address) === keyOf(address),
        )
        .sort((a, b) => a.membership - b.membership);
}

function memberChoice(members: ReminderPerson[]): Role | null {
    const chosen = new Set(
        members.flatMap((member) => (member.choice ? [member.choice] : [])),
    );
    if (chosen.has("bcc") || chosen.size > 1) return "bcc";
    return [...chosen][0] ?? null;
}

function typedChoice(
    store: ReminderStore,
    address: string,
    members: ReminderPerson[],
): Role | null {
    const typed = store.typed.get(keyOf(address));
    if (!typed) return null;
    if (typed.role === "bcc") return "bcc";
    return sameIds(
        typed.members,
        members.map((member) => member.membership),
    )
        ? typed.role
        : null;
}

function explicitRole(
    store: ReminderStore,
    persons: ReminderPerson[],
    address: string,
): Role | null {
    const members = holders(persons, address);
    const c = memberChoice(members);
    const t = store.recipients.some((item) => keyOf(item) === keyOf(address))
        ? typedChoice(store, address, members)
        : null;
    if (c === "bcc" || t === "bcc") return "bcc";
    return c ?? t;
}

function eligible(persons: ReminderPerson[], address: string) {
    const tied = persons.filter(
        (person) => keyOf(person.address) === keyOf(address),
    );
    return !tied.length || tied.some((person) => person.active !== false);
}

function workspaceMembers(persons: ReminderPerson[]) {
    const groups = new Map<string, ReminderPerson[]>();
    for (const person of [...persons].sort(
        (a, b) => a.membership - b.membership,
    ))
        if (person.active !== false)
            groups.set(keyOf(person.address), [
                ...(groups.get(keyOf(person.address)) ?? []),
                person,
            ]);
    return [...groups.values()];
}

function effective(store: ReminderStore, persons: ReminderPerson[]) {
    return [
        ...new Map(
            [
                ...store.recipients.filter((address) =>
                    eligible(persons, address),
                ),
                ...(store.include_workspace_members
                    ? workspaceMembers(persons).map(
                          (group) => group[0]!.address,
                      )
                    : []),
            ].map((address) => [keyOf(address), address]),
        ).values(),
    ];
}

function placements(store: ReminderStore, persons: ReminderPerson[]): Role[] {
    const list = effective(store, persons);
    if (store.hide_recipients) return list.map(() => "bcc");
    const placed = list.map((address) => {
        const members = holders(persons, address);
        const role = explicitRole(store, persons, address);
        if (role) return role;
        if (!store.include_workspace_members || !members.length) return "bcc";
        return members.some((member) => member.administrator) ? "to" : null;
    });
    const fallback: Role = placed.includes("to") ? "cc" : "to";
    return placed.map((role) => role ?? fallback);
}

function reminderAnswer(
    store: ReminderStore,
    persons: ReminderPerson[],
    {
        limit,
        dailyLimit,
        deliveries,
        administrator,
    }: {
        limit: number;
        dailyLimit: number;
        deliveries: Partial<Delivery>[];
        administrator: boolean;
    },
): ReminderAnswer {
    const list = effective(store, persons);
    const roles = placements(store, persons);
    const groups = workspaceMembers(persons);
    return {
        enabled: store.enabled,
        revision: store.revision,
        state: !store.enabled
            ? "paused_by_user"
            : list.length > limit
              ? "over_recipient_limit"
              : list.length
                ? "sending"
                : "no_recipients",
        times: store.times,
        language: store.language,
        include_workspace_members: store.include_workspace_members,
        hide_recipients: store.hide_recipients,
        addresses: administrator
            ? {
                  recipients: store.recipients,
                  workspace_recipients: groups.map(
                      (group) => group[0]!.address,
                  ),
                  members: groups.map((group) => ({
                      membership: group[0]!.membership,
                      address: group[0]!.address,
                      kind: group.some((member) => member.administrator)
                          ? "administrator"
                          : "member",
                      role: memberChoice(group),
                  })),
                  added: store.recipients.map((address) => ({
                      address,
                      role: explicitRole(store, persons, address),
                      eligible: eligible(persons, address),
                      memberships: holders(persons, address).map(
                          (member) => member.membership,
                      ),
                  })),
              }
            : null,
        recipient_limit: limit,
        recipient_count: list.length,
        workspace_member_count: groups.length,
        recipient_limit_exceeded: list.length > limit,
        daily_reminder_limit: dailyLimit,
        deliveries: deliveries.map((delivery, index) => {
            const row: Delivery = {
                id: index + 1,
                recipient_count: list.length,
                recipients: administrator ? list : null,
                recipient_roles: null,
                scheduled_for: "2026-01-01T08:00:00Z",
                collected_at: null,
                source_age_seconds: null,
                stale: false,
                sent_at: "2026-01-01T08:00:00Z",
                status: "sent",
                late: false,
                last_error: "",
                forecast_status: null,
                forecast_complete: null,
                ...delivery,
            };
            const reached = row.status === "sent" || row.status === "uncertain";
            return "recipient_roles" in delivery
                ? row
                : {
                      ...row,
                      recipient_roles:
                          row.recipients && reached
                              ? row.recipients.map((address, at) => ({
                                    address,
                                    role: roles[at]!,
                                }))
                              : null,
                  };
        }),
    };
}

const repeated = (values: string[]) =>
    new Set(values.map(keyOf)).size !== values.length;

// The server's refusals, in its order of checks.
function refused(
    submitted: ReminderPayload,
    stored: ReminderStore,
    count: number,
    current: number,
    limit: number,
) {
    if (repeated(submitted.recipients))
        return {
            code: "duplicate_recipients",
            field: "recipients",
            message: "Recipient addresses must be distinct (case-insensitive).",
        } as const;
    if (
        submitted.enabled &&
        !submitted.recipients.length &&
        !submitted.include_workspace_members
    )
        return {
            code: "recipient_required",
            field: "recipients",
            message: "Add a recipient or include the workspace members.",
        } as const;
    if (repeated(submitted.times))
        return {
            code: "duplicate_reminder_times",
            field: "times",
            message: "Use each delivery time once.",
        } as const;
    if (submitted.enabled && !submitted.times.length)
        return {
            code: "reminder_time_required",
            field: "times",
            message: "Add at least one delivery time to turn reminders on.",
        } as const;
    const typed = submitted.address_roles ?? [];
    if (
        repeated(typed.map((entry) => entry.address)) ||
        typed.some(
            (entry) =>
                !submitted.recipients.some(
                    (address) => keyOf(address) === keyOf(entry.address),
                ),
        )
    )
        return {
            code: "invalid_request",
            field: "address_roles",
            message: "Name each typed address once.",
        } as const;
    const named = submitted.member_roles ?? [];
    if (
        repeated(named.map((entry) => String(entry.membership))) ||
        repeated(named.map((entry) => entry.address))
    )
        return {
            code: "invalid_request",
            field: "member_roles",
            message: "Name each member once.",
        } as const;
    const unchangedPause =
        !submitted.enabled &&
        submitted.include_workspace_members ===
            stored.include_workspace_members &&
        submitted.language === stored.language &&
        submitted.recipients.join() === stored.recipients.join() &&
        [...submitted.times].sort().join() === [...stored.times].sort().join();
    if (count > limit && !(count < current || unchangedPause))
        return {
            code: "recipient_limit_exceeded",
            field: "recipients",
            message: `This plant allows at most ${limit} distinct recipient addresses in total.`,
        } as const;
    return null;
}

function stored(
    payload: ReminderPayload,
    before: ReminderStore,
    persons: ReminderPerson[],
): ReminderStore {
    const typed = new Map<string, TypedChoice>();
    for (const address of payload.recipients) {
        const members = holders(persons, address).map(
            (member) => member.membership,
        );
        if (!payload.address_roles) {
            const kept = before.typed.get(keyOf(address));
            if (kept) typed.set(keyOf(address), kept);
            continue;
        }
        const entry = payload.address_roles.find(
            (item) => keyOf(item.address) === keyOf(address),
        );
        if (entry?.role === "bcc")
            typed.set(keyOf(address), { role: "bcc", members: [] });
        else if (entry && sameIds(members, entry.memberships))
            typed.set(keyOf(address), { role: entry.role, members });
    }
    for (const entry of payload.member_roles ?? [])
        for (const member of holders(persons, entry.address))
            member.choice = entry.role;
    return {
        enabled: payload.enabled,
        recipients: payload.recipients,
        times: [...payload.times].sort(),
        language: payload.language,
        include_workspace_members: payload.include_workspace_members,
        hide_recipients: payload.hide_recipients ?? true,
        typed,
        revision: before.revision + 1,
    };
}

const changed = () =>
    errorBody(
        "reminder_schedule_changed",
        "The email reminders changed. Reload before saving.",
    );

export async function reminderApi(page: Page, options: ReminderOptions = {}) {
    await mockApi(page);
    const limit = options.limit ?? 5;
    const persons = people(options.members ?? [user.email]);
    let store = reminderStore(options, persons);
    await page.route("**/reminders/", async (route) => {
        if (route.request().method() === "PUT") {
            const payload = route.request().postDataJSON() as ReminderPayload;
            expect(["en", "tr"]).toContain(payload.language);
            if (payload.expected_revision !== store.revision)
                return route.fulfill({ status: 409, json: changed() });
            const refusal = refused(
                payload,
                store,
                effective({ ...store, ...payload }, persons).length,
                effective(store, persons).length,
                limit,
            );
            if (refusal)
                return route.fulfill({
                    status: 400,
                    json: errorBody(refusal.code, refusal.message, {
                        fields: { [refusal.field]: refusal.message },
                    }),
                });
            if (
                (payload.member_roles ?? []).some(
                    (entry) =>
                        !holders(persons, entry.address).some(
                            (member) => member.membership === entry.membership,
                        ),
                )
            )
                return route.fulfill({ status: 409, json: changed() });
            store = stored(payload, store, persons);
        }
        await route.fulfill({
            json: reminderAnswer(store, persons, {
                limit,
                dailyLimit: options.dailyLimit ?? 10,
                deliveries: options.deliveries ?? [{}],
                administrator: !(options.viewer ?? (() => false))(),
            }),
        });
    });
    return { persons };
}

export async function openReminders(page: Page) {
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
        .getByRole("button", { name: "Email reminders", exact: true })
        .click();
}
