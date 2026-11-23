import { readFileSync } from "node:fs";
import path from "node:path";
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
export type AccountUser = Mock<Schemas["SessionUserOutput"]> &
    Pick<
        Schemas["SessionUserOutput"],
        "id" | "username" | "email" | "has_usable_password"
    >;
export type Account = { user: AccountUser; tag: string } | null;
export type AccountState = { current: Account };
export const firstAccount: NonNullable<Account> = { user, tag: "tag-a" };
export const adminUser = { ...user, can_admin: true, mfa_required: true };
export const accountWith = (
    fields: Partial<AccountUser>,
): NonNullable<Account> => ({ ...firstAccount, user: { ...user, ...fields } });

export function sessionIdentity(account: Account) {
    return {
        user: account?.user ?? null,
        csrf_token: account ? "signed-token" : "anonymous-token",
    };
}

// django-allauth's session view answers from request.user: 200 with the user, or 401 with flows.
function allauthSession(account: Account) {
    if (!account)
        return {
            status: 401,
            data: { flows: [] },
            meta: { is_authenticated: false },
        };
    const { id, username, email, has_usable_password } = account.user;
    return {
        status: 200,
        data: {
            user: {
                id,
                display: username,
                email,
                has_usable_password,
                username,
            },
            methods: [{ method: "password", at: 1767225600, email }],
        },
        meta: { is_authenticated: true },
    };
}

export async function mockApi(
    page: Page,
    {
        anonymous = false,
        empty = false,
        failed = false,
        admin = false,
        account = {
            current: anonymous
                ? null
                : admin
                  ? accountWith(adminUser)
                  : firstAccount,
        },
    }: {
        anonymous?: boolean;
        empty?: boolean;
        failed?: boolean;
        admin?: boolean;
        account?: AccountState;
    } = {},
): Promise<AccountState> {
    let automationEnabled = false;
    let submittedSite: Mock<Schemas["SiteOutput"]> | undefined;
    let submittedPlant: Mock<Schemas["PlantOutput"]> | undefined;
    await page.route("**/api/**", async (route) => {
        const path = new URL(route.request().url()).pathname,
            method = route.request().method();
        let data: unknown;
        if (path === "/api/auth/browser/v1/auth/session" && method === "GET") {
            data = allauthSession(account.current);
        } else if (path === "/api/auth/browser/v1/auth/login") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "anonymous-token",
            );
            account.current ??= admin ? accountWith(adminUser) : firstAccount;
            data = { status: 200, meta: { is_authenticated: true } };
        } else if (
            path === "/api/auth/browser/v1/auth/session" &&
            method === "DELETE"
        ) {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                account.current ? "signed-token" : "anonymous-token",
            );
            account.current = null;
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
            data = answer("SessionOutput", sessionIdentity(account.current));
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
        await route.fulfill({
            status: path.startsWith("/api/auth/")
                ? (data as { status: number }).status
                : 200,
            json: data,
        });
    });
    return account;
}

type Delivery = Schemas["ReminderDeliveryOutput"];
type ReminderAnswer = Schemas["ReminderOutput"];
type ReminderPayload = Schemas["ReminderInput"];
type Role = Schemas["RecipientRoleEnum"];
export type ReminderPerson = {
    membership: number;
    primary: string;
    verified?: string[];
    administrator?: boolean;
    active?: boolean;
    choice?: Role;
};
type TypedChoice = { role: Role | ""; members: number[] };
type TypedRow = TypedChoice & { address: string; ties: number[] };
type ReminderStore = {
    saved: boolean;
    enabled: boolean;
    rows: TypedRow[];
    times: string[];
    language: Schemas["LanguageEnum"];
    include: boolean;
    hide: boolean;
    departed: boolean;
    choices: Map<number, Role>;
    revision: number;
};
type Frozen = "recipients" | "recipient_roles" | "recipient_count";
type DeliveryFixture = Partial<Omit<Delivery, Frozen>> &
    ({ [K in Frozen]?: never } | Pick<Delivery, Frozen>);
export type ReminderOptions = {
    unsaved?: boolean;
    recipients?: string[];
    limit?: number;
    enabled?: boolean;
    deliveries?: DeliveryFixture[];
    dailyLimit?: number;
    members?: (string | ReminderPerson)[];
    includeMembers?: boolean;
    viewer?: (method: string) => boolean;
    hidden?: boolean;
    hiddenByDeparture?: boolean;
    typed?: Record<string, Role | (TypedChoice & { ties?: number[] })>;
    // Non-ASCII spellings with the wire form the server's tests establish for each.
    wire?: Record<string, string>;
};
type Key = (address: string) => string;
type World = {
    persons: ReminderPerson[];
    key: Key;
    wireOf: (address: string) => string | null;
};
type Refusal = {
    status: number;
    code: ErrorCode;
    fields?: string[];
    message?: string;
};
type Member = {
    address: string;
    memberships: number[];
    administrator: boolean;
};
type People = {
    members: Member[];
    active: Set<number>;
    unaddressable: boolean;
    member: (address: string) => Member | undefined;
    holders: (address: string) => number[];
};

const sorted = (ids: Iterable<number>) =>
    [...new Set(ids)].sort((a, b) => a - b);
const sameIds = (left: number[], right: number[]) =>
    sorted(left).join() === sorted(right).join();
const verifiedOf = (person: ReminderPerson) =>
    person.verified ?? [person.primary];
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const ASCII = /^[\x20-\x7e]*$/;
const RECIPIENT_MAX_LENGTH = 254;

function addressing(wire: Record<string, string>) {
    const declared = new Map(
        Object.entries(wire).map(([spelling, form]) => [
            spelling.toLowerCase(),
            form,
        ]),
    );
    const wireOf = (address: string) => {
        const text = address.trim();
        const form = ASCII.test(text)
            ? text
            : (declared.get(text.toLowerCase()) ?? "");
        return form.length <= RECIPIENT_MAX_LENGTH && EMAIL.test(form)
            ? form
            : null;
    };
    // An address without a wire form has a key no wire form has, as on the server.
    const key: Key = (address) =>
        wireOf(address)?.toLowerCase() ?? `\n${address.trim().toLowerCase()}`;
    return { wireOf, key };
}

function combined<T>(choices: T[], hidden: T): T | undefined {
    const distinct = new Map(
        choices.map((choice) => [JSON.stringify(choice), choice]),
    );
    return distinct.size > 1 ? hidden : [...distinct.values()][0];
}

type Stored = TypedChoice & { ties: number[] };

function typedChoices(rows: TypedRow[], key: Key) {
    const grouped = new Map<string, TypedRow[]>();
    for (const row of rows)
        grouped.set(key(row.address), [
            ...(grouped.get(key(row.address)) ?? []),
            row,
        ]);
    return new Map(
        [...grouped].map(([found, same]): [string, Stored] => [
            found,
            {
                ...combined<TypedChoice>(
                    same.map((row) => ({
                        role: row.role,
                        members: sorted(row.members),
                    })),
                    { role: "bcc", members: [] },
                )!,
                ties: sorted(same.flatMap((row) => row.ties)),
            },
        ]),
    );
}

function people(members: (string | ReminderPerson)[]): ReminderPerson[] {
    return members.map((member, index) =>
        typeof member === "string"
            ? {
                  membership: index + 1,
                  primary: member,
                  administrator:
                      member.toLowerCase() === user.email.toLowerCase(),
              }
            : member,
    );
}

function read({ persons, key, wireOf }: World): People {
    const grouped = new Map<string, ReminderPerson[]>();
    const holders = new Map<string, Set<number>>();
    for (const person of [...persons].sort(
        (a, b) => a.membership - b.membership,
    )) {
        const own = key(person.primary);
        if (
            person.active !== false &&
            verifiedOf(person).some((address) => key(address) === own)
        )
            grouped.set(own, [...(grouped.get(own) ?? []), person]);
        for (const address of [person.primary, ...verifiedOf(person)])
            if (address.trim())
                holders.set(
                    key(address),
                    (holders.get(key(address)) ?? new Set()).add(
                        person.membership,
                    ),
                );
    }
    const members = [...grouped.values()].map((found) => ({
        address: found[0]!.primary.trim(),
        memberships: found.map((person) => person.membership),
        administrator: found.some((person) => person.administrator === true),
    }));
    return {
        members,
        active: new Set(
            persons
                .filter((person) => person.active !== false)
                .map((person) => person.membership),
        ),
        unaddressable: persons.some((person) =>
            [person.primary, ...verifiedOf(person)].some(
                (address) => address.trim() && !wireOf(address),
            ),
        ),
        member: (address) =>
            members.find((member) => key(member.address) === key(address)),
        holders: (address) => sorted(holders.get(key(address)) ?? []),
    };
}

const eligible = (row: TypedRow, found: People) =>
    !row.ties.length || row.ties.some((id) => found.active.has(id));

function effective(
    include: boolean,
    rows: TypedRow[],
    found: People,
    key: Key,
) {
    const addresses = new Map<string, string>();
    const add = (address: string) => {
        if (!addresses.has(key(address))) addresses.set(key(address), address);
    };
    for (const row of rows) if (eligible(row, found)) add(row.address);
    if (include) for (const member of found.members) add(member.address);
    return [...addresses.values()];
}

type Candidate = {
    member: Member | undefined;
    chosen: Role | null;
    typed: Role | null;
    counts: boolean;
    tiedHidden: boolean;
};

function candidate(
    address: string,
    store: ReminderStore,
    found: People,
    key: Key,
): Candidate {
    const member = found.member(address);
    const typedRows = typedChoices(store.rows, key);
    const stored = typedRows.get(key(address));
    const typed = stored?.role ? stored : undefined;
    const hiddenTies = new Set(
        [...typedRows.values()].flatMap((row) =>
            row.role === "bcc" ? row.ties : [],
        ),
    );
    return {
        member,
        chosen: member
            ? (combined<Role>(
                  member.memberships.flatMap((id) => {
                      const role = store.choices.get(id);
                      return role ? [role] : [];
                  }),
                  "bcc",
              ) ?? null)
            : null,
        typed: typed ? (typed.role as Role) : null,
        counts: !typed || sameIds(member?.memberships ?? [], typed.members),
        tiedHidden:
            member?.memberships.some((id) => hiddenTies.has(id)) ?? false,
    };
}

function explicitRole(found: Candidate): Role | null {
    if (found.tiedHidden || found.chosen === "bcc" || found.typed === "bcc")
        return "bcc";
    return found.chosen ?? (found.counts ? found.typed : null);
}

function placements(
    addresses: string[],
    store: ReminderStore,
    found: People,
    key: Key,
): Role[] {
    if (store.hide || found.unaddressable) return addresses.map(() => "bcc");
    const placed = addresses.map((address): Role | null => {
        const item = candidate(address, store, found, key);
        const role = explicitRole(item);
        if (role) return role;
        if (store.include && item.member)
            return item.member.administrator ? "to" : null;
        return "bcc";
    });
    const fallback: Role = placed.includes("to") ? "cc" : "to";
    return placed.map((role) => role ?? fallback);
}

const BASE_DELIVERY: Delivery = {
    id: 0,
    recipient_count: 1,
    recipients: [user.email],
    recipient_roles: [{ address: user.email, role: "bcc" }],
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
};

function reminderAnswer(
    store: ReminderStore,
    world: World,
    {
        limit,
        dailyLimit,
        deliveries,
        administrator,
        requested,
    }: {
        limit: number;
        dailyLimit: number;
        deliveries: DeliveryFixture[];
        administrator: boolean;
        requested: Schemas["LanguageEnum"];
    },
): ReminderAnswer {
    const found = read(world);
    const sent = effective(store.include, store.rows, found, world.key);
    const placed = new Map(
        placements(sent, store, found, world.key).map((role, index) => [
            world.key(sent[index]!),
            role,
        ]),
    );
    const placement = (address: string) =>
        placed.get(world.key(address)) ?? "bcc";
    return {
        enabled: store.enabled,
        revision: store.revision,
        state: !store.enabled
            ? "paused_by_user"
            : sent.length > limit
              ? "over_recipient_limit"
              : sent.length
                ? "sending"
                : "no_recipients",
        times: store.times,
        language: store.saved ? store.language : requested,
        include_workspace_members: store.include,
        hide_recipients: store.hide && store.saved,
        hidden_by_departure: administrator ? store.departed : null,
        addresses: administrator
            ? {
                  recipients: store.rows.map((row) => row.address),
                  workspace_recipients: found.members.map(
                      (member) => member.address,
                  ),
                  members: found.members.map((member) => ({
                      memberships: member.memberships,
                      address: member.address,
                      kind: member.administrator ? "administrator" : "member",
                      role: candidate(member.address, store, found, world.key)
                          .chosen,
                      placement: placement(member.address),
                  })),
                  added: store.rows.map((row) => ({
                      address: row.address,
                      role: explicitRole(
                          candidate(row.address, store, found, world.key),
                      ),
                      placement: placement(row.address),
                      eligible: eligible(row, found),
                      memberships: found.member(row.address)?.memberships ?? [],
                  })),
              }
            : null,
        recipient_limit: limit,
        recipient_count: sent.length,
        workspace_member_count: found.members.length,
        recipient_limit_exceeded: sent.length > limit,
        daily_reminder_limit: dailyLimit,
        deliveries: deliveries.map((delivery, index) => {
            const row: Delivery = {
                ...BASE_DELIVERY,
                id: index + 1,
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

function storeOf(options: ReminderOptions, world: World): ReminderStore {
    if (options.unsaved)
        return {
            saved: false,
            enabled: false,
            rows: [],
            times: [],
            language: "en",
            include: false,
            hide: false,
            departed: false,
            choices: new Map(),
            revision: 0,
        };
    const found = read(world);
    const rows = (options.recipients ?? [user.email]).map(
        (address): TypedRow => {
            const choice = options.typed?.[address];
            const typed =
                typeof choice === "string"
                    ? {
                          role: choice,
                          members:
                              choice === "bcc"
                                  ? []
                                  : (found.member(address)?.memberships ?? []),
                      }
                    : (choice ?? { role: "" as const, members: [] });
            return { ties: found.holders(address), address, ...typed };
        },
    );
    return {
        saved: true,
        enabled: options.enabled ?? false,
        rows,
        times: ["08:00"],
        language: "en",
        include: options.includeMembers ?? false,
        hide: options.hidden ?? options.hiddenByDeparture ?? true,
        departed: options.hiddenByDeparture ?? false,
        choices: new Map(
            world.persons.flatMap((person) =>
                person.choice ? [[person.membership, person.choice]] : [],
            ),
        ),
        revision: 0,
    };
}

function defaultReminders() {
    const world = { persons: people([user.email]), ...addressing({}) };
    return reminderAnswer(storeOf({}, world), world, {
        limit: 5,
        dailyLimit: 10,
        deliveries: [],
        administrator: true,
        requested: "en",
    });
}

function requestLanguage(route: Route): Schemas["LanguageEnum"] {
    const tag = route.request().headers()["accept-language"] ?? "";
    return tag.toLowerCase().startsWith("tr") ? "tr" : "en";
}

type Schema = {
    type?: string;
    properties?: Record<string, Schema>;
    required?: string[];
    items?: Schema;
    $ref?: string;
    enum?: unknown[];
    pattern?: string;
    format?: string;
    maxItems?: number;
    maxLength?: number;
    minimum?: number;
    nullable?: boolean;
    oneOf?: Schema[];
};
const published = (
    JSON.parse(
        readFileSync(path.resolve(__dirname, "../../openapi.json"), "utf8"),
    ) as { components: { schemas: Record<string, Schema> } }
).components.schemas;

function resolved(schema: Schema): Schema {
    return schema.$ref
        ? resolved(published[schema.$ref.split("/").at(-1)!]!)
        : schema;
}

function violations(
    value: unknown,
    schema: Schema,
    at: string[] = [],
): string[] {
    const rule = resolved(schema);
    const here = [at.join(".")];
    if (value === null) return rule.nullable ? [] : here;
    if (rule.oneOf)
        return rule.oneOf.some((option) => !violations(value, option).length)
            ? []
            : here;
    if (rule.enum && !rule.enum.includes(value)) return here;
    if (rule.type === "object") {
        if (typeof value !== "object" || Array.isArray(value)) return here;
        const record = value as Record<string, unknown>;
        const known = Object.keys(rule.properties ?? {});
        if (Object.keys(record).some((name) => !known.includes(name)))
            return here;
        return [
            ...(rule.required ?? [])
                .filter((name) => record[name] === undefined)
                .map((name) => [...at, name].join(".")),
            ...Object.entries(rule.properties ?? {}).flatMap(
                ([name, property]) =>
                    record[name] === undefined
                        ? []
                        : violations(record[name], property, [...at, name]),
            ),
        ];
    }
    if (rule.type === "array") {
        if (!Array.isArray(value) || value.length > (rule.maxItems ?? Infinity))
            return here;
        return value.flatMap((item, index) =>
            violations(item, rule.items ?? {}, [...at, String(index)]),
        );
    }
    if (rule.type === "string")
        return typeof value === "string" &&
            value.length <= (rule.maxLength ?? Infinity) &&
            (!rule.pattern || new RegExp(rule.pattern).test(value)) &&
            (rule.format !== "email" || EMAIL.test(value))
            ? []
            : here;
    if (rule.type === "integer")
        return Number.isInteger(value) &&
            (value as number) >= (rule.minimum ?? -Infinity)
            ? []
            : here;
    if (rule.type === "boolean") return typeof value === "boolean" ? [] : here;
    return [];
}

const repeated = (values: string[]) => new Set(values).size !== values.length;
const invalid = (field: string): Refusal => ({
    status: 400,
    code: "invalid_request",
    fields: [field],
});

function refused(
    body: unknown,
    store: ReminderStore,
    world: World,
    {
        limit,
        dailyLimit,
        administrator,
    }: { limit: number; dailyLimit: number; administrator: boolean },
): Refusal | null {
    const shape = violations(body, { $ref: "ReminderInput" });
    if (shape.includes(""))
        return { status: 400, code: "invalid_request", fields: shape };
    const submitted = body as ReminderPayload;
    const named = shape.some((field) => field.startsWith("member_roles"))
        ? []
        : (submitted.member_roles ?? []);
    const fields = [
        ...shape,
        ...(shape.some((field) => field.startsWith("recipients")) ||
        submitted.recipients.every((address) => world.wireOf(address))
            ? []
            : ["recipients"]),
        ...(repeated(
            named.flatMap((entry) => sorted(entry.memberships).map(String)),
        ) || repeated(named.map((entry) => world.key(entry.address)))
            ? ["member_roles"]
            : []),
    ];
    if (fields.length) return { status: 400, code: "invalid_request", fields };
    const typed = submitted.address_roles ?? [];
    const recipients = submitted.recipients.map((address) => address.trim());
    if (
        repeated(typed.map((entry) => world.key(entry.address))) ||
        typed.some(
            (entry) =>
                !recipients.some(
                    (address) =>
                        world.key(address) === world.key(entry.address),
                ),
        )
    )
        return invalid("address_roles");
    if (!administrator) return { status: 403, code: "permission_denied" };
    if (submitted.expected_revision !== store.revision)
        return { status: 409, code: "reminder_schedule_changed" };
    if (repeated(recipients.map(world.key)))
        return {
            status: 400,
            code: "duplicate_recipients",
            fields: ["recipients"],
        };
    if (
        submitted.enabled &&
        !recipients.length &&
        !submitted.include_workspace_members
    )
        return {
            status: 400,
            code: "recipient_required",
            fields: ["recipients"],
        };
    if (repeated(submitted.times))
        return {
            status: 400,
            code: "duplicate_reminder_times",
            fields: ["times"],
        };
    if (submitted.enabled && !submitted.times.length)
        return {
            status: 400,
            code: "reminder_time_required",
            fields: ["times"],
        };
    if (
        submitted.times.length > dailyLimit &&
        submitted.times.length > store.times.length
    )
        return {
            status: 400,
            code: "too_many_reminder_times",
            fields: ["times"],
            message: `This plant allows at most ${dailyLimit} delivery times a day.`,
        };
    const found = read(world);
    if (
        ![...typed, ...named].every((entry) =>
            sameIds(
                entry.memberships,
                found.member(entry.address)?.memberships ?? [],
            ),
        )
    )
        return { status: 409, code: "reminder_schedule_changed" };
    const next = stored(submitted, store, world);
    const count = effective(next.include, next.rows, found, world.key).length;
    const current = effective(
        store.include,
        store.rows,
        found,
        world.key,
    ).length;
    const unchangedPause =
        !submitted.enabled &&
        submitted.include_workspace_members === store.include &&
        submitted.language === store.language &&
        recipients.join("\n") ===
            store.rows.map((row) => row.address).join("\n") &&
        [...submitted.times].sort().join() === store.times.join();
    if (count > limit && !(count < current || unchangedPause))
        return {
            status: 400,
            code: "recipient_limit_exceeded",
            fields: ["recipients"],
            message: `This plant allows at most ${limit} distinct recipient addresses in total.`,
        };
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

function refusalBody(refusal: Refusal) {
    const message = refusal.message ?? messages[refusal.code] ?? "Refused.";
    const fields = (refusal.fields ?? []).filter(Boolean);
    return errorBody(refusal.code, message, {
        ...(fields.length && {
            fields: Object.fromEntries(
                fields.map((field) => [
                    field,
                    refusal.code === "invalid_request"
                        ? "Enter a valid value."
                        : message,
                ]),
            ),
        }),
    });
}

function stored(
    payload: ReminderPayload,
    before: ReminderStore,
    world: World,
): ReminderStore {
    const found = read(world);
    const key = world.key;
    const rows = payload.recipients.map((text): TypedRow => {
        const address = text.trim();
        const kept = before.rows.filter(
            (row) => key(row.address) === key(address),
        );
        const ties = sorted([
            ...kept.flatMap((row) => row.ties),
            ...found.holders(address),
        ]);
        const entry = payload.address_roles?.find(
            (item) => key(item.address) === key(address),
        );
        if (!entry) {
            const choice = typedChoices(kept, key).get(key(address));
            return {
                address,
                role: choice?.role ?? "",
                members: choice?.role ? choice.members : [],
                ties,
            };
        }
        return {
            address,
            role: entry.role ?? "",
            members:
                entry.role === "to" || entry.role === "cc"
                    ? sorted(entry.memberships)
                    : [],
            ties,
        };
    });
    const choices = new Map(before.choices);
    for (const entry of payload.member_roles ?? [])
        for (const id of entry.memberships)
            if (entry.role) choices.set(id, entry.role);
            else choices.delete(id);
    return {
        saved: true,
        enabled: payload.enabled,
        rows,
        times: [...payload.times].sort(),
        language: payload.language,
        include: payload.include_workspace_members,
        hide: payload.hide_recipients ?? true,
        departed: false,
        choices,
        revision: before.revision,
    };
}

const signature = (store: ReminderStore) =>
    JSON.stringify([
        store.saved,
        store.enabled,
        store.rows.map((row) => [row.address, row.role, row.members, row.ties]),
        store.times,
        store.language,
        store.include,
        store.hide,
        store.departed,
        [...store.choices].sort(([a], [b]) => a - b),
    ]);

export async function reminderApi(page: Page, options: ReminderOptions = {}) {
    await mockApi(page);
    const limit = options.limit ?? 5;
    const dailyLimit = options.dailyLimit ?? 10;
    const persons = people(options.members ?? [user.email]);
    const world = { persons, ...addressing(options.wire ?? {}) };
    let store = storeOf(options, world);
    const deliveries = options.deliveries ?? (options.unsaved ? [] : [{}]);
    const viewer = options.viewer ?? (() => false);
    const later: { method: string; released: Promise<void> }[] = [];
    const answer = (value: ReminderStore, route: Route) =>
        reminderAnswer(value, world, {
            limit,
            dailyLimit,
            deliveries,
            administrator: !viewer(route.request().method()),
            requested: requestLanguage(route),
        });
    const write = async (route: Route) => {
        const body: unknown = route.request().postDataJSON();
        const refusal = refused(body, store, world, {
            limit,
            dailyLimit,
            administrator: !viewer(route.request().method()),
        });
        if (refusal) {
            await route.fulfill({
                status: refusal.status,
                json: refusalBody(refusal),
            });
            return null;
        }
        const next = stored(body as ReminderPayload, store, world);
        return signature(next) === signature(store)
            ? store
            : { ...next, revision: store.revision + 1 };
    };
    await page.route("**/reminders/preview/", async (route) => {
        const preview = await write(route);
        if (preview) await route.fulfill({ json: answer(preview, route) });
    });
    await page.route("**/reminders/", async (route) => {
        const method = route.request().method();
        if (method === "PUT") {
            const next = await write(route);
            if (!next) return;
            store = next;
        }
        const json = answer(store, route);
        const gate = later.findIndex((item) => item.method === method);
        if (gate >= 0) await later.splice(gate, 1)[0]!.released;
        await route.fulfill({ json });
    });
    return {
        persons,
        deliveries,
        answerLater: (method: "GET" | "PUT") => {
            let release!: () => void;
            const released = new Promise<void>(
                (resolve) => (release = resolve),
            );
            later.push({ method, released });
            return release;
        },
    };
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
