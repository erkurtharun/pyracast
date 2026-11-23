import { expect, type Page, type Route } from "@playwright/test";
import { errorBody } from "./errors";
import type { ErrorCode } from "../../src/lib/api/errors";

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
        else if (path.endsWith("/reminders/")) data = defaultReminders();
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
type ReminderAnswer = Schemas["ReminderOutput"];
type ReminderPayload = Schemas["ReminderInput"];
type Role = Schemas["RecipientRoleEnum"];
export type ReminderPerson = {
    membership: number;
    address: string;
    administrator?: boolean;
    active?: boolean;
    verified?: boolean;
    choice?: Role;
};
type TypedChoice = { role: Role | ""; members: number[]; ties?: number[] };
type TypedRecord = { role: Role | ""; members: number[]; ties: number[] };
type ReminderStore = {
    enabled: boolean;
    recipients: string[];
    times: string[];
    language: Schemas["LanguageEnum"];
    include: boolean;
    hide: boolean;
    departed: boolean;
    typed: Map<string, TypedRecord>;
    choices: Map<number, Role>;
    revision: number;
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
    hiddenByDeparture?: boolean;
    typed?: Record<string, Role | TypedChoice>;
    // Spellings that are one mailbox, as the server's mailbox key folds them.
    aliases?: string[][];
};
type World = { persons: ReminderPerson[]; key: (address: string) => string };
type Refusal = { status: number; code: ErrorCode; field?: string };

const MAX_ITEMS = 32767;
const sorted = (ids: number[]) => [...ids].sort((a, b) => a - b);
const sameIds = (left: number[], right: number[]) =>
    sorted(left).join() === sorted(right).join();

function mailboxKey(aliases: string[][]) {
    return (address: string) => {
        const folded = address.trim().toLowerCase();
        const group = aliases.find((spellings) =>
            spellings.some((spelling) => spelling.toLowerCase() === folded),
        );
        return group ? group[0]!.toLowerCase() : folded;
    };
}

function people(members: (string | ReminderPerson)[]): ReminderPerson[] {
    return members.map((member, index) =>
        typeof member === "string"
            ? {
                  membership: index + 1,
                  address: member,
                  administrator:
                      member.toLowerCase() === user.email.toLowerCase(),
              }
            : member,
    );
}

function membersOf(world: World, address: string) {
    return world.persons
        .filter(
            (person) =>
                person.active !== false &&
                person.verified !== false &&
                world.key(person.address) === world.key(address),
        )
        .sort((a, b) => a.membership - b.membership);
}

function holders(world: World, address: string) {
    return sorted(
        world.persons
            .filter(
                (person) =>
                    person.verified !== false &&
                    world.key(person.address) === world.key(address),
            )
            .map((person) => person.membership),
    );
}

function groups(world: World) {
    const grouped = new Map<string, ReminderPerson[]>();
    for (const person of world.persons)
        if (person.active !== false && person.verified !== false) {
            const key = world.key(person.address);
            grouped.set(key, [...(grouped.get(key) ?? []), person]);
        }
    return [...grouped.values()]
        .map((group) => group.sort((a, b) => a.membership - b.membership))
        .sort((a, b) => a[0]!.membership - b[0]!.membership);
}

function storeOf(options: ReminderOptions, world: World): ReminderStore {
    const recipients = options.recipients ?? [user.email];
    const typed = new Map<string, TypedRecord>();
    for (const address of recipients)
        typed.set(world.key(address), {
            role: "",
            members: [],
            ties: holders(world, address),
        });
    for (const [address, choice] of Object.entries(options.typed ?? {}))
        typed.set(
            world.key(address),
            typeof choice === "string"
                ? {
                      role: choice,
                      members:
                          choice === "bcc"
                              ? []
                              : membersOf(world, address).map(
                                    (person) => person.membership,
                                ),
                      ties: holders(world, address),
                  }
                : { ties: holders(world, address), ...choice },
        );
    return {
        enabled: options.enabled ?? false,
        recipients,
        times: ["08:00"],
        language: "en",
        include: options.includeMembers ?? false,
        hide: options.hidden ?? options.hiddenByDeparture ?? true,
        departed: options.hiddenByDeparture ?? false,
        typed,
        choices: new Map(
            world.persons.flatMap((person) =>
                person.choice ? [[person.membership, person.choice]] : [],
            ),
        ),
        revision: 0,
    };
}

function addressing(store: ReminderStore, world: World) {
    const typedOf = (address: string) =>
        store.recipients.some((item) => world.key(item) === world.key(address))
            ? store.typed.get(world.key(address))
            : undefined;
    const eligible = (address: string) => {
        const ties = typedOf(address)?.ties ?? [];
        return (
            !ties.length ||
            ties.some(
                (id) =>
                    world.persons.find((person) => person.membership === id)
                        ?.active !== false,
            )
        );
    };
    const memberChoice = (members: ReminderPerson[]) => {
        const chosen = new Set(
            members.flatMap((member) => {
                const role = store.choices.get(member.membership);
                return role ? [role] : [];
            }),
        );
        if (chosen.has("bcc") || chosen.size > 1) return "bcc" as const;
        return [...chosen][0] ?? null;
    };
    const explicit = (address: string): Role | null => {
        const members = membersOf(world, address);
        const c = memberChoice(members);
        const row = typedOf(address);
        const t =
            row?.role === "bcc"
                ? "bcc"
                : row?.role &&
                    sameIds(
                        row.members,
                        members.map((member) => member.membership),
                    )
                  ? row.role
                  : null;
        const tiedHidden = [...store.typed.values()].some(
            (other) =>
                other.role === "bcc" &&
                members.some((member) =>
                    other.ties.includes(member.membership),
                ),
        );
        if (c === "bcc" || t === "bcc" || tiedHidden) return "bcc";
        return c ?? t;
    };
    const list = [
        ...new Map(
            [
                ...store.recipients.filter(eligible),
                ...(store.include
                    ? groups(world).map((group) => group[0]!.address)
                    : []),
            ].map((address) => [world.key(address), address]),
        ).values(),
    ];
    const first = list.map((address) => {
        if (store.hide) return "bcc";
        const role = explicit(address);
        if (role) return role;
        const members = membersOf(world, address);
        if (!store.include || !members.length) return "bcc";
        return members.some((member) => member.administrator) ? "to" : null;
    });
    const fallback: Role = first.includes("to") ? "cc" : "to";
    const placed = new Map(
        list.map((address, index) => [
            world.key(address),
            first[index] ?? fallback,
        ]),
    );
    const placement = (address: string): Role =>
        placed.get(world.key(address)) ??
        (store.hide ? "bcc" : (explicit(address) ?? "bcc"));
    return { list, eligible, explicit, memberChoice, placement };
}

function deliveriesOf(
    store: ReminderStore,
    world: World,
    rows: Partial<Delivery>[],
): Partial<Delivery>[] {
    const { list, placement } = addressing(store, world);
    return rows.map((delivery) => ({
        recipient_count: list.length,
        recipients: list,
        recipient_roles: list.map((address) => ({
            address,
            role: placement(address),
        })),
        ...delivery,
    }));
}

function reminderAnswer(
    store: ReminderStore,
    world: World,
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
    const { list, eligible, explicit, memberChoice, placement } = addressing(
        store,
        world,
    );
    const grouped = groups(world);
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
        include_workspace_members: store.include,
        hide_recipients: store.hide,
        hidden_by_departure: administrator ? store.departed : null,
        addresses: administrator
            ? {
                  recipients: store.recipients,
                  workspace_recipients: grouped.map(
                      (group) => group[0]!.address,
                  ),
                  members: grouped.map((group) => ({
                      memberships: group.map((member) => member.membership),
                      address: group[0]!.address,
                      kind: group.some((member) => member.administrator)
                          ? "administrator"
                          : "member",
                      role: memberChoice(group),
                      placement: placement(group[0]!.address),
                  })),
                  added: store.recipients.map((address) => ({
                      address,
                      role: explicit(address),
                      placement: eligible(address) ? placement(address) : "bcc",
                      eligible: eligible(address),
                      memberships: membersOf(world, address).map(
                          (member) => member.membership,
                      ),
                  })),
              }
            : null,
        recipient_limit: limit,
        recipient_count: list.length,
        workspace_member_count: grouped.length,
        recipient_limit_exceeded: list.length > limit,
        daily_reminder_limit: dailyLimit,
        deliveries: deliveries.map((delivery, index) => {
            const row: Delivery = {
                id: index + 1,
                recipient_count: 0,
                recipients: [],
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
            return {
                ...row,
                recipients: administrator ? row.recipients : null,
                recipient_roles:
                    administrator && row.recipients && reached
                        ? row.recipient_roles
                        : null,
            };
        }),
    };
}

function defaultReminders() {
    const world = { persons: people([user.email]), key: mailboxKey([]) };
    const store = storeOf({}, world);
    return reminderAnswer(store, world, {
        limit: 5,
        dailyLimit: 10,
        deliveries: [],
        administrator: true,
    });
}

const repeated = (values: string[]) => new Set(values).size !== values.length;
const isEmail = (value: unknown) =>
    typeof value === "string" &&
    value.length <= 254 &&
    /^[^@\s]+@[^@\s]+$/.test(value);
const isMemberships = (value: unknown) =>
    Array.isArray(value) &&
    value.length <= MAX_ITEMS &&
    value.every((id) => Number.isInteger(id) && id >= 1);
const isRole = (value: unknown) =>
    value === null || value === "to" || value === "cc" || value === "bcc";

function invalidEntries(value: unknown) {
    return (
        value !== undefined &&
        (!Array.isArray(value) ||
            value.length > MAX_ITEMS ||
            value.some(
                (entry: Record<string, unknown>) =>
                    !entry ||
                    !isEmail(entry.address) ||
                    !("role" in entry) ||
                    !isRole(entry.role) ||
                    !isMemberships(entry.memberships),
            ))
    );
}

function refused(
    submitted: ReminderPayload,
    store: ReminderStore,
    world: World,
    limit: number,
): Refusal | null {
    const invalid = (field: string): Refusal => ({
        status: 400,
        code: "invalid_request",
        field,
    });
    if (!submitted.recipients.every(isEmail)) return invalid("recipients");
    if (invalidEntries(submitted.address_roles))
        return invalid("address_roles");
    if (invalidEntries(submitted.member_roles)) return invalid("member_roles");
    const typed = submitted.address_roles ?? [];
    const recipientKeys = submitted.recipients.map(world.key);
    if (
        repeated(typed.map((entry) => world.key(entry.address))) ||
        typed.some((entry) => !recipientKeys.includes(world.key(entry.address)))
    )
        return invalid("address_roles");
    const named = submitted.member_roles ?? [];
    if (
        repeated(named.flatMap((entry) => entry.memberships.map(String))) ||
        repeated(named.map((entry) => world.key(entry.address)))
    )
        return invalid("member_roles");
    if (submitted.expected_revision !== store.revision)
        return { status: 409, code: "reminder_schedule_changed" };
    if (repeated(recipientKeys))
        return {
            status: 400,
            code: "duplicate_recipients",
            field: "recipients",
        };
    if (
        submitted.enabled &&
        !submitted.recipients.length &&
        !submitted.include_workspace_members
    )
        return { status: 400, code: "recipient_required", field: "recipients" };
    if (repeated(submitted.times))
        return {
            status: 400,
            code: "duplicate_reminder_times",
            field: "times",
        };
    if (submitted.enabled && !submitted.times.length)
        return { status: 400, code: "reminder_time_required", field: "times" };
    const count = addressing(stored(submitted, store, world), world).list
        .length;
    const current = addressing(store, world).list.length;
    const unchangedPause =
        !submitted.enabled &&
        submitted.include_workspace_members === store.include &&
        submitted.language === store.language &&
        submitted.recipients.join() === store.recipients.join() &&
        [...submitted.times].sort().join() === [...store.times].sort().join();
    if (count > limit && !(count < current || unchangedPause))
        return {
            status: 400,
            code: "recipient_limit_exceeded",
            field: "recipients",
        };
    const exact = (memberships: number[], address: string) =>
        sameIds(
            memberships,
            membersOf(world, address).map((member) => member.membership),
        );
    if (
        ![...typed, ...named].every((entry) =>
            exact(entry.memberships, entry.address),
        )
    )
        return { status: 409, code: "reminder_schedule_changed" };
    return null;
}

const messages: Partial<Record<ErrorCode, string>> = {
    invalid_request: "The request input was refused.",
    reminder_schedule_changed:
        "The email reminders changed. Reload before saving.",
    duplicate_recipients:
        "Recipient addresses must be distinct (case-insensitive).",
    recipient_required: "Add a recipient or include the workspace members.",
    duplicate_reminder_times: "Use each delivery time once.",
    reminder_time_required:
        "Add at least one delivery time to turn reminders on.",
    permission_denied: "You do not have permission to perform this action.",
};

function refusalBody(refusal: Refusal, limit: number) {
    const message =
        refusal.code === "recipient_limit_exceeded"
            ? `This plant allows at most ${limit} distinct recipient addresses in total.`
            : (messages[refusal.code] ?? "Refused.");
    return errorBody(refusal.code, message, {
        ...(refusal.field && { fields: { [refusal.field]: message } }),
    });
}

function stored(
    payload: ReminderPayload,
    before: ReminderStore,
    world: World,
): ReminderStore {
    const typed = new Map<string, TypedRecord>();
    for (const address of payload.recipients) {
        const key = world.key(address);
        const entry = payload.address_roles?.find(
            (item) => world.key(item.address) === key,
        );
        const ties = holders(world, address);
        const kept = before.recipients.some((item) => world.key(item) === key)
            ? before.typed.get(key)
            : undefined;
        if (!entry)
            typed.set(
                key,
                kept ? { ...kept, ties } : { role: "", members: [], ties },
            );
        else if (entry.role === "to" || entry.role === "cc")
            typed.set(key, {
                role: entry.role,
                members: entry.memberships,
                ties,
            });
        else typed.set(key, { role: entry.role ?? "", members: [], ties });
    }
    const choices = new Map(before.choices);
    for (const entry of payload.member_roles ?? [])
        for (const member of membersOf(world, entry.address))
            if (entry.role) choices.set(member.membership, entry.role);
            else choices.delete(member.membership);
    return {
        enabled: payload.enabled,
        recipients: payload.recipients,
        times: [...payload.times].sort(),
        language: payload.language,
        include: payload.include_workspace_members,
        hide: payload.hide_recipients ?? true,
        departed: false,
        typed,
        choices,
        revision: before.revision,
    };
}

const signature = (store: ReminderStore) =>
    JSON.stringify({
        ...store,
        revision: 0,
        typed: [...store.typed].sort(),
        choices: [...store.choices].sort(),
    });

export async function reminderApi(page: Page, options: ReminderOptions = {}) {
    await mockApi(page);
    const limit = options.limit ?? 5;
    const persons = people(options.members ?? [user.email]);
    const world = { persons, key: mailboxKey(options.aliases ?? []) };
    let store = storeOf(options, world);
    const deliveries = deliveriesOf(store, world, options.deliveries ?? [{}]);
    const viewer = options.viewer ?? (() => false);
    const answer = (value: ReminderStore) =>
        reminderAnswer(value, world, {
            limit,
            dailyLimit: options.dailyLimit ?? 10,
            deliveries,
            administrator: !viewer(),
        });
    const write = async (route: Route) => {
        const payload = route.request().postDataJSON() as ReminderPayload;
        expect(["en", "tr"]).toContain(payload.language);
        const refusal: Refusal | null = viewer()
            ? { status: 403, code: "permission_denied" }
            : refused(payload, store, world, limit);
        if (refusal) {
            await route.fulfill({
                status: refusal.status,
                json: refusalBody(refusal, limit),
            });
            return null;
        }
        const next = stored(payload, store, world);
        return signature(next) === signature(store)
            ? store
            : { ...next, revision: store.revision + 1 };
    };
    await page.route("**/reminders/preview/", async (route) => {
        const preview = await write(route);
        if (preview) await route.fulfill({ json: answer(preview) });
    });
    await page.route("**/reminders/", async (route) => {
        if (route.request().method() === "PUT") {
            const next = await write(route);
            if (!next) return;
            store = next;
        }
        await route.fulfill({ json: answer(store) });
    });
    return { persons, deliveries };
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
