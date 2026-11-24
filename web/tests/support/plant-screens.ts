import type { Route } from "@playwright/test";
import { type Page } from "./test";
import { errorBody } from "./errors";
import {
    answer,
    mockApi,
    paged,
    tenantRow,
    user,
    type Mock,
} from "./workspace";
import { nativeProductionHistory } from "./native-production";
import type { components } from "../../src/lib/schema";

type Schemas = components["schemas"];
type Row = Partial<Parameters<typeof nativeProductionHistory>[2][number]>;
type Language = "en" | "tr";

export const now = new Date("2026-11-10T12:00:00Z");

const reading = {
    energy_mwh: 1,
    quality: "valid",
    source: "meter",
    source_kind: "manual",
    provisional: false,
    aggregated: false,
} as const;

type Role = Schemas["WorkspaceRoleEnum"];

export async function signIn(
    page: Page,
    {
        language = "en",
        role = "admin",
    }: { language?: Language; role?: Role } = {},
) {
    let current = language;
    let currentRole = role;
    const session = () => ({
        user: { ...user, language_preference: current },
        language: current,
        csrf_token: "signed-token",
    });
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({ json: session() }),
    );
    await page.route("**/api/session/language/", (route) => {
        current = (route.request().postDataJSON() as { language: Language })
            .language;
        return route.fulfill({ json: session() });
    });
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({
                    id: 1,
                    name: "Sunfield Energy",
                    role: currentRole,
                }),
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({ json: paged([]) }),
    );
    return {
        language: () => current,
        changeRole: (next: Role) => {
            currentRole = next;
        },
    };
}

function productionDay(date: string, rows: Row[]) {
    return nativeProductionHistory(
        date,
        "Europe/Istanbul",
        rows.map((row, index) => {
            const minutes = 8 * 60 + index * 30;
            const clock = (value: number) =>
                `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
            return {
                starts_at: `${date}T${clock(minutes)}:00+03:00`,
                ends_at: `${date}T${clock(minutes + 30)}:00+03:00`,
                effective: reading,
                ...row,
            };
        }),
    );
}

export async function serveProduction(page: Page, rows: Row[]) {
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({ json: productionDay(date, rows) });
    });
}

export async function openPlant(page: Page, section?: string) {
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    if (section)
        await page.getByRole("button", { name: section, exact: true }).click();
}

export async function loseConnection(page: Page) {
    let lost = true;
    await page.route("**/api/**", (route) =>
        lost ? route.abort("internetdisconnected") : route.fallback(),
    );
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    return async () => {
        lost = false;
        await page.evaluate(() => window.dispatchEvent(new Event("online")));
    };
}

export async function focusWindow(page: Page) {
    await page.evaluate(() =>
        document.dispatchEvent(
            new Event("visibilitychange", { bubbles: true }),
        ),
    );
}

type Outage = Schemas["IncidentListOutput"];

export const activeOutage: Outage = {
    id: 51,
    kind: "inverter",
    title: "Inverter disconnected",
    status: "active",
    revision: 1,
    starts_at: "2026-11-09T08:00:00Z",
    ends_at: null,
    available_dc_mw: 0,
    available_inverter_mw: 0,
    available_export_mw: 0,
    declared_configuration_id: 31,
    notes: "",
    created_at: "2026-11-09T08:00:00Z",
    updated_at: "2026-11-09T08:00:00Z",
    pending_request_id: null,
};

export async function serveOutages(page: Page, outages: Outage[]) {
    await page.route("**/plants/21/outages/", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  json: answer("IncidentPage", {
                      ...paged(outages),
                      can_propose: false,
                      proposal_unavailable_reason: "admin_edits_directly",
                  }),
              })
            : route.fallback(),
    );
}

// "drop" never reaches the server; the lost answers come after the server applied the write.
type Gateway = 502 | 504 | 520 | 521 | 522 | 523 | 524;
type Lost = "abort" | "cut" | "maintenance" | Gateway;
export type Answering =
    "answer" | Lost | "drop" | "busy" | "throttled" | "refuse";

export function answers(...list: Answering[]) {
    let sent = 0;
    return () => list[sent++] ?? "answer";
}

function refusedEarly(route: Route, answering: Answering) {
    if (answering === "drop") return route.abort("failed");
    if (answering === "busy")
        return route.fulfill({
            status: 429,
            json: errorBody("session_busy", "The session is busy.", {
                retry_after: 0,
            }),
        });
    if (answering === "throttled")
        return route.fulfill({
            status: 429,
            json: errorBody("throttled", "Too many requests.", {
                retry_after: 0,
            }),
        });
    if (answering === "refuse")
        return route.fulfill({
            status: 400,
            json: errorBody("invalid_request", "The request is invalid."),
        });
    return null;
}

function fulfil(route: Route, answering: Answering, json: unknown) {
    if (answering === "abort") return route.abort("failed");
    if (answering === "cut")
        return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: "{",
        });
    if (answering === "maintenance")
        return route.fulfill({
            status: 503,
            contentType: "application/json",
            headers: { "Retry-After": "60" },
            body: '{"code": "maintenance", "message": "Pyracast is being updated. Try again in a minute; your data is safe.", "retry_after": 60}',
        });
    if (typeof answering === "number")
        return route.fulfill({
            status: answering,
            contentType: "text/plain",
            body: "Bad Gateway",
        });
    return route.fulfill({ json });
}

const conflict = () =>
    errorBody(
        "production_revision_changed",
        "Production changed. Reload the current values and review them.",
    );

type ChangeRow = Schemas["ProductionChangeRow"];
type Revision = {
    id: number;
    supersedes: number | null;
    energy: number;
    quality: string;
    source: string;
    reverted: boolean;
    by: "person" | "operator";
};

// The server's replay rule: an expected revision already superseded by the very change asked for was applied.
function replays(
    change: ChangeRow,
    head: Revision | undefined,
    energy: number,
) {
    if (!head || head.supersedes !== change.expected_revision_id) return false;
    if (change.action === "revert") return head.reverted;
    return (
        !head.reverted &&
        head.energy === energy &&
        head.quality === (change.quality ?? "valid") &&
        head.source === (change.source ?? "operator")
    );
}

// The 08:00 interval of an administrator; a repeat of an applied change answers as its first application did.
export async function serveDirectProduction(page: Page, next: () => Answering) {
    const revisions: Revision[] = [];
    const requests: unknown[] = [];
    const head = () => revisions.at(-1);
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        const current = head();
        return route.fulfill({
            json: productionDay(date, [
                {
                    can_edit: true,
                    can_revert: !!current && !current.reverted,
                    revision_id: current?.id ?? null,
                    effective:
                        current && !current.reverted
                            ? { ...reading, energy_mwh: current.energy }
                            : reading,
                },
            ]),
        });
    });
    await page.route("**/production/changes/", (route) => {
        const body = route.request().postDataJSON() as { changes: ChangeRow[] };
        requests.push(body);
        const answering = next();
        const early = refusedEarly(route, answering);
        if (early) return early;
        const [change] = body.changes;
        if (!change) throw new Error("A production change has no row.");
        const current = head();
        const energy = change.energy ?? current?.energy ?? 0;
        if ((current?.id ?? null) === change.expected_revision_id)
            revisions.push({
                id: revisions.length + 1,
                supersedes: current?.id ?? null,
                energy,
                quality: change.quality ?? "valid",
                source: change.source ?? "operator",
                reverted: change.action === "revert",
                by: "person",
            });
        else if (!replays(change, current, energy))
            return route.fulfill({ status: 409, json: conflict() });
        return fulfil(route, answering, {
            created: 1,
            existing: 0,
            count: 1,
            ids: [head()!.id],
        });
    });
    return {
        requests,
        applied: () => revisions.filter((row) => row.by === "person").length,
        energy: () => head()?.energy,
        changeByOperator(energy: number) {
            revisions.push({
                id: revisions.length + 1,
                supersedes: head()?.id ?? null,
                energy,
                quality: "valid",
                source: "operator",
                reverted: false,
                by: "operator",
            });
        },
    };
}

type Observation = {
    starts_at: string;
    ends_at: string;
    energy: number;
    quality?: ChangeRow["quality"];
    source?: string;
};
type Head = { id: number; supersedes: number | null; energy: number };

// Heads in the file's unit; a confirmed batch applies whole, is replayed whole or is refused whole.
export async function serveImport(
    page: Page,
    next: () => Answering,
    heads: Record<string, number>,
) {
    let ids = 0;
    const current = new Map<string, Head>(
        Object.entries(heads).map(([start, energy]) => [
            start,
            { id: ++ids, supersedes: null, energy },
        ]),
    );
    const requests: unknown[] = [];
    let applied = 0;
    await page.route("**/production/preview/", (route) => {
        const body = route.request().postDataJSON() as {
            observations: Observation[];
        };
        return route.fulfill({
            json: answer("ProductionPreviewOutput", {
                warnings: [],
                changes: body.observations.map((row) => ({
                    ...row,
                    action: "set",
                    expected_revision_id:
                        current.get(row.starts_at)?.id ?? null,
                })),
                preview: body.observations.map((row) => {
                    const head = current.get(row.starts_at);
                    return {
                        starts_at: row.starts_at,
                        ends_at: row.ends_at,
                        current_energy_mwh: head ? head.energy / 1000 : null,
                        new_energy_mwh: row.energy / 1000,
                        corrected: false,
                    };
                }),
            }),
        });
    });
    await page.route("**/production/changes/", (route) => {
        const body = route.request().postDataJSON() as {
            changes: (Observation & { expected_revision_id: number | null })[];
        };
        requests.push(body);
        const answering = next();
        const early = refusedEarly(route, answering);
        if (early) return early;
        const rows = body.changes.map((change) => {
            const head = current.get(change.starts_at);
            const expected = (head?.id ?? null) === change.expected_revision_id;
            const replayed =
                !expected &&
                head?.supersedes === change.expected_revision_id &&
                head.energy === change.energy;
            return {
                change,
                head,
                expected,
                replayed,
                unchanged: expected && head?.energy === change.energy,
            };
        });
        if (
            rows.some((row) => !row.expected && !row.replayed) ||
            (rows.some((row) => row.replayed) &&
                rows.some((row) => row.expected && !row.unchanged))
        )
            return route.fulfill({ status: 409, json: conflict() });
        for (const row of rows)
            if (row.expected && !row.unchanged) {
                current.set(row.change.starts_at, {
                    id: ++ids,
                    supersedes: row.head?.id ?? null,
                    energy: row.change.energy,
                });
                applied++;
            }
        const unchanged = rows.filter((row) => row.unchanged).length;
        return fulfil(route, answering, {
            created: rows.length - unchanged,
            existing: unchanged,
            count: rows.length,
            ids: rows.map((row) => current.get(row.change.starts_at)!.id),
        });
    });
    return { requests, applied: () => applied };
}

type Status = Schemas["ChangeRequestStatusEnum"];
type Payload = Record<string, unknown>;

// A repeat under a key answers with its first request as it now is; other content under the key is refused.
function proposalStore() {
    const requests: Payload[] = [];
    const stored = new Map<string, { id: number; payload: Payload }>();
    const store = {
        status: "pending" as Status,
        requests,
        created: () => stored.size,
        pendingFor: (match: (payload: Payload) => boolean) =>
            store.status === "pending"
                ? ([...stored.values()].find((request) =>
                      match(request.payload),
                  )?.id ?? null)
                : null,
        async answer(route: Route, next: () => Answering) {
            const body = route.request().postDataJSON() as Payload;
            requests.push(body);
            const answering = next();
            const early = refusedEarly(route, answering);
            if (early) return early;
            const { idempotency_key: key, ...payload } = body;
            if (typeof key !== "string")
                return route.fulfill({
                    status: 400,
                    json: errorBody("invalid_request", "A key is required."),
                });
            const earlier = stored.get(key);
            if (
                earlier &&
                JSON.stringify(earlier.payload) !== JSON.stringify(payload)
            )
                return route.fulfill({
                    status: 409,
                    json: errorBody(
                        "idempotency_key_reused",
                        "Use a new request key for changed input.",
                    ),
                });
            const request = earlier ?? { id: stored.size + 1, payload };
            stored.set(key, request);
            return fulfil(route, answering, {
                id: request.id,
                kind: "production",
                status: store.status,
            });
        },
    };
    return store;
}

// The 08:00 interval of a member; it shows the member's pending request once one exists.
export async function serveProposals(page: Page, next: () => Answering) {
    const proposals = proposalStore();
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: productionDay(date, [
                {
                    can_edit: false,
                    can_propose: true,
                    pending_request_id: proposals.pendingFor(() => true),
                },
            ]),
        });
    });
    await page.route("**/change-requests/production/", (route) =>
        proposals.answer(route, next),
    );
    return proposals;
}

type OutageWrite = Partial<Outage> & {
    idempotency_key?: string;
    expected_revision?: number;
};

const outagePage = (outages: Outage[], member: boolean) =>
    answer("IncidentPage", {
        ...paged(outages),
        can_propose: member,
        proposal_unavailable_reason: member ? null : "admin_edits_directly",
    });

const VALUES = [
    "kind",
    "title",
    "notes",
    "starts_at",
    "ends_at",
    "available_dc_mw",
    "available_inverter_mw",
    "available_export_mw",
] as const;

// A keyed create repeated with the values of its first application answers with that outage as it now is.
export async function serveOutageWrites(
    page: Page,
    next: () => Answering,
    initial: Outage[] = [],
) {
    let outages = [...initial];
    let applied = 0;
    const created = new Map<string, { id: number; values: OutageWrite }>();
    const writes: { url: string; body: OutageWrite }[] = [];
    await page.route("**/plants/21/outages/**", (route) => {
        const request = route.request();
        if (request.method() === "GET")
            return route.fulfill({ json: outagePage(outages, false) });
        const url = new URL(request.url()).pathname;
        const body = request.postDataJSON() as OutageWrite;
        writes.push({ url, body });
        const answering = next();
        const early = refusedEarly(route, answering);
        if (early) return early;
        const {
            idempotency_key: key,
            expected_revision: expected,
            ...values
        } = body;
        const target = outages.find((row) =>
            url.includes(`/outages/${row.id}/`),
        );
        if (!target) {
            const earlier = key === undefined ? undefined : created.get(key);
            if (earlier) {
                const same = VALUES.every(
                    (field) =>
                        JSON.stringify(earlier.values[field] ?? null) ===
                        JSON.stringify(values[field] ?? null),
                );
                if (!same)
                    return route.fulfill({
                        status: 409,
                        json: errorBody(
                            "idempotency_key_reused",
                            "Use a new request key for changed input.",
                        ),
                    });
                const now = outages.find((row) => row.id === earlier.id)!;
                return fulfil(route, answering, now);
            }
            const outage: Outage = {
                ...activeOutage,
                ...values,
                id: 61 + created.size,
                revision: 1,
                status: values.ends_at ? "resolved" : "active",
            };
            if (key !== undefined) created.set(key, { id: outage.id, values });
            outages = [outage, ...outages];
            applied++;
            return fulfil(route, answering, outage);
        }
        if (expected !== target.revision)
            return route.fulfill({
                status: 409,
                json: answer("IncidentConflict", {
                    ...errorBody(
                        "incident_revision_changed",
                        "The outage changed. Review its current values and save again.",
                    ),
                    current: target,
                }),
            });
        const changed = { ...target, ...values };
        const saved: Outage = {
            ...changed,
            revision: target.revision + 1,
            status: url.endsWith("/cancel/")
                ? "cancelled"
                : changed.ends_at
                  ? "resolved"
                  : "active",
        };
        outages = outages.map((row) => (row.id === saved.id ? saved : row));
        applied++;
        return fulfil(route, answering, saved);
    });
    return {
        writes,
        outages: () => outages,
        applied: () => applied,
        changeTitle(title: string) {
            outages = outages.map((row) => ({
                ...row,
                title,
                revision: row.revision + 1,
            }));
        },
    };
}

// The active outage of a member's plant; it shows the member's pending request once a proposal about it exists.
export async function serveOutageProposals(
    page: Page,
    next: () => Answering,
    direct: () => boolean = () => false,
) {
    const proposals = proposalStore();
    const writes: { url: string; body: unknown }[] = [];
    await page.route("**/plants/21/outages/**", (route) => {
        const request = route.request();
        if (request.method() !== "GET") {
            writes.push({
                url: new URL(request.url()).pathname,
                body: request.postDataJSON(),
            });
            return route.fulfill({
                json: { ...activeOutage, revision: 2, status: "resolved" },
            });
        }
        return route.fulfill({
            json: outagePage(
                [
                    {
                        ...activeOutage,
                        pending_request_id: proposals.pendingFor(
                            (payload) =>
                                payload.incident_id === activeOutage.id,
                        ),
                    },
                ],
                !direct(),
            ),
        });
    });
    await page.route("**/change-requests/incidents/", (route) =>
        proposals.answer(route, next),
    );
    return Object.assign(proposals, { direct: writes });
}

const pendingRequest = {
    id: 1,
    kind: "production",
    status: "pending",
    plant_id: 21,
    region_id: 11,
    plant_name: "East array",
    configuration_id: 31,
    timezone: "Europe/Istanbul",
    item_count: 0,
    period_start: "2026-11-09T08:00:00+03:00",
    period_end: "2026-11-09T08:30:00+03:00",
    incident_action: null,
    incident_title: null,
    proposed_by: { id: 2, name: "Member" },
    created_at: "2026-11-09T12:00:00Z",
    reason: "Verified meter reading",
    reviewed_by: null,
    closed_at: null,
    decision_reason: "",
    former_member: false,
    can_review: true,
    can_withdraw: false,
    unread: false,
    conflict: null,
    review_fingerprint: "context-1",
    incident: null,
    production_items: [],
} satisfies Mock<Schemas["ChangeRequestOutput"]>;

// A decision applies once; a repeat of the same outcome answers with the decided request.
export async function reviewRequest(
    page: Page,
    next: () => Answering,
    language: Language = "en",
) {
    const labels = {
        en: {
            inbox: "Inbox",
            details: "Review details",
            approve: "Approve and apply",
            refresh: "Refresh details",
        },
        tr: {
            inbox: "Gelen kutusu",
            details: "Ayrıntıları incele",
            approve: "Onayla ve uygula",
            refresh: "Ayrıntıları yenile",
        },
    }[language];
    await page.clock.install({ time: now });
    await signIn(page, { language });
    const request = {
        ...pendingRequest,
        status: "pending" as Status,
        can_review: true,
        decision_reason: "",
    };
    const decisions: unknown[] = [];
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({
            json: paged(request.status === "pending" ? [request] : []),
        }),
    );
    await page.route("**/change-requests/1/", (route) =>
        route.fulfill({ json: request }),
    );
    await page.route(/change-requests\/1\/(approve|reject)\/$/, (route) => {
        const outcome = route.request().url().includes("/approve/")
            ? "approved"
            : "rejected";
        const body = route.request().postDataJSON() as {
            review_fingerprint?: string;
            reason?: string;
        };
        decisions.push(body);
        const answering = next();
        const early = refusedEarly(route, answering);
        if (early) return early;
        if (request.status === "pending") {
            if (
                outcome === "approved" &&
                body.review_fingerprint !== request.review_fingerprint
            )
                return route.fulfill({
                    status: 409,
                    json: errorBody(
                        "review_context_changed",
                        "The submission changed. Review it again.",
                    ),
                });
            Object.assign(request, {
                status: outcome,
                can_review: false,
                decision_reason: body.reason ?? "",
            });
        } else if (request.status !== outcome)
            return route.fulfill({
                status: 409,
                json: errorBody(
                    "request_already_closed",
                    "This request is already closed.",
                ),
            });
        return fulfil(route, answering, request);
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: labels.inbox, exact: true }).click();
    await page
        .getByRole("button", { name: labels.details, exact: true })
        .click();
    const detail = page.locator(".inbox-detail");
    return {
        request,
        decisions,
        detail,
        approve: detail.getByRole("button", { name: labels.approve }),
        refresh: detail.getByRole("button", { name: labels.refresh }),
    };
}
