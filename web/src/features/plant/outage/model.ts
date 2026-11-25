import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { outstandingAfter, serverTrim } from "@/lib/idempotency";
import type { components } from "@/lib/schema";

export type Outage = components["schemas"]["IncidentOutput"];
type OutageInput = components["schemas"]["IncidentInput"];
export type OutageRow = components["schemas"]["IncidentListOutput"];

export type OutageChange =
    | { action: "create"; values: OutageInput; reason: string }
    | { action: "update"; outage: Outage; values: OutageInput; reason: string }
    | { action: "resolve"; outage: Outage; ends_at: string; reason: string }
    | { action: "cancel"; outage: Outage; reason: string };

export type Stale = { before: Outage; current: Outage };

type Offer = {
    can_propose: boolean;
    proposal_unavailable_reason: string | null;
    results: readonly OutageRow[];
};

// What the screen reads besides its own state: the list, whether the viewer writes directly, whether a configuration exists.
export type Live = {
    offer: Offer | undefined;
    direct: boolean;
    configured: boolean;
};

// Why an open workflow cannot send: a member's outage holds their own pending request or they reached the limit, or
// anyone's outage was cancelled, resolved (for a resolution) or left the list. A workflow stays held until closed.
export type Hold = "held" | "pending_request_limit" | Ending;

export type Ending = "cancelled" | "resolved" | "removed";

// `payload` names the request apart from the revision it is sent against; `change` is what it sends.
type Attempt = { payload: string; key: string; change: OutageChange };

type Phase =
    | { status: "idle" }
    | { status: "sending"; sent: Attempt }
    | { status: "saved" }
    | { status: "held"; hold: Hold };

type Target =
    | { kind: "create" }
    | { kind: "update" | "resolve" | "cancel"; outage: Outage };

// `outage` is the version the workflow sends against; `form` counts restarts of its form after a discarded stale change.
export type Workflow = Target & {
    phase: Phase;
    stale: Stale | null;
    form: number;
};

type Status = components["schemas"]["ChangeRequestStatusEnum"];

// The one state of the outage screen: at most one open workflow, the status a proposal was just answered with, and
// the write whose answer was lost, until an answer to it or a closed form ends it.
export type OutageScreen = {
    workflow: Workflow | null;
    receipt: Status | null;
    attempt: Attempt | null;
};

export const INITIAL_OUTAGES: OutageScreen = {
    workflow: null,
    receipt: null,
    attempt: null,
};

export type OutageEvent =
    | { type: "open"; target: Target; live: Live }
    | { type: "close" }
    | { type: "discard" }
    | { type: "send"; payload: string; key: string; change: OutageChange }
    | { type: "answered" }
    // The answer and the reload that follows it have both finished.
    | { type: "sent"; receipt: Status | null }
    | SendFailed
    | { type: "rowsLoaded"; live: Live };

type SendFailed = {
    type: "sendFailed";
    change: OutageChange;
    current: OutageRow | null;
    error: unknown;
    direct: boolean;
};

export type OutageControls = {
    create: boolean;
    acts: boolean;
    offered: ReadonlySet<number>;
    send: boolean;
    editable: boolean;
    close: boolean;
    hold: Hold | null;
    pending: "unanswered" | "confirmed" | null;
};

const atLimit = (live: Live) =>
    live.offer?.proposal_unavailable_reason === "pending_request_limit";

const sending = (workflow: Workflow | null) =>
    workflow?.phase.status === "sending" || workflow?.phase.status === "saved";

const targetOf = (workflow: Workflow | null) =>
    workflow && workflow.kind !== "create" ? workflow.outage.id : null;

export const isEnding = (hold: Hold | null): hold is Ending =>
    hold === "cancelled" || hold === "resolved" || hold === "removed";

// An outage that is gone or cancelled takes no change at all, and one no longer active takes no resolution.
function endingOf(
    kind: Target["kind"],
    row: Outage | undefined,
): Ending | null {
    if (!row) return "removed";
    if (row.status === "cancelled") return "cancelled";
    return kind === "resolve" && row.status !== "active" ? "resolved" : null;
}

// An ending holds anyone; only a member is held by their own request or the limit.
function holdOf(workflow: Workflow, live: Live): Hold | null {
    const id = targetOf(workflow);
    const row = live.offer?.results.find((row) => row.id === id);
    const ending =
        workflow.kind === "create" ? null : endingOf(workflow.kind, row);
    if (ending || live.direct) return ending;
    if (row?.pending_request_id != null) return "held";
    return atLimit(live) ? "pending_request_limit" : null;
}

export function outageControls(
    screen: OutageScreen,
    live: Live,
): OutageControls {
    const { workflow } = screen;
    const busy = sending(workflow);
    const open = targetOf(workflow);
    const offered = new Set(
        (live.offer?.results ?? [])
            .filter(
                (row) =>
                    row.status !== "cancelled" &&
                    row.id !== open &&
                    (live.direct ||
                        (!atLimit(live) && row.pending_request_id == null)),
            )
            .map((row) => row.id),
    );
    const free = !!workflow && workflow.phase.status !== "held";
    return {
        create: live.direct ? live.configured : !!live.offer?.can_propose,
        acts: !busy,
        offered,
        send: free && !busy,
        editable: free,
        close: !!workflow && !busy,
        hold: workflow?.phase.status === "held" ? workflow.phase.hold : null,
        pending: busy
            ? workflow?.phase.status === "saved"
                ? "confirmed"
                : "unanswered"
            : null,
    };
}

function isOpen(workflow: Workflow | null, target: Target) {
    if (!workflow || workflow.kind !== target.kind) return false;
    return target.kind === "create" || targetOf(workflow) === target.outage.id;
}

export function reduceOutages(
    screen: OutageScreen,
    event: OutageEvent,
): OutageScreen {
    const { workflow } = screen;
    switch (event.type) {
        case "open": {
            const derived = outageControls(screen, event.live);
            const { target } = event;
            const allowed =
                target.kind === "create"
                    ? derived.create
                    : derived.offered.has(target.outage.id) &&
                      !endingOf(target.kind, target.outage);
            if (!derived.acts || !allowed || isOpen(workflow, target))
                return screen;
            return {
                ...screen,
                receipt: null,
                workflow: {
                    ...target,
                    phase: { status: "idle" },
                    stale: null,
                    form: 0,
                },
            };
        }
        case "close":
            if (!workflow || sending(workflow)) return screen;
            return { ...screen, workflow: null, attempt: null };
        case "discard":
            if (!workflow?.stale || sending(workflow)) return screen;
            // A held workflow cannot send, so discarding its change leaves nothing to start again.
            if (workflow.phase.status === "held")
                return { ...screen, workflow: null, attempt: null };
            return {
                ...screen,
                workflow: { ...workflow, stale: null, form: workflow.form + 1 },
            };
        case "send": {
            if (workflow?.phase.status !== "idle") return screen;
            const { payload, key, change } = event;
            const sent =
                screen.attempt?.payload === payload
                    ? screen.attempt
                    : { payload, key, change };
            return {
                ...screen,
                receipt: null,
                workflow: { ...workflow, phase: { status: "sending", sent } },
            };
        }
        case "sent": {
            if (!sending(workflow)) return screen;
            const { attempt } = reduceOutages(screen, { type: "answered" });
            return { workflow: null, receipt: event.receipt, attempt };
        }
        case "answered":
        case "sendFailed": {
            if (workflow?.phase.status !== "sending") return screen;
            const { sent } = workflow.phase;
            const attempt = outstandingAfter(
                screen.attempt,
                sent,
                event.type === "sendFailed" ? event.error : undefined,
            );
            if (
                event.type === "answered" ||
                (screen.attempt?.payload === sent.payload &&
                    lostAnswerApplied(event))
            )
                return {
                    ...screen,
                    attempt,
                    workflow: { ...workflow, phase: { status: "saved" } },
                };
            const { current } = event;
            // Only a conflict about the outage the workflow sends against moves it to that outage's current version.
            if (
                workflow.kind === "create" ||
                current?.id !== workflow.outage.id
            )
                return {
                    ...screen,
                    attempt,
                    workflow: { ...workflow, phase: { status: "idle" } },
                };
            const hold =
                endingOf(workflow.kind, current) ??
                (!event.direct && current.pending_request_id != null
                    ? "held"
                    : null);
            return {
                ...screen,
                attempt,
                workflow: {
                    ...workflow,
                    outage: current,
                    stale: {
                        before: workflow.stale?.before ?? workflow.outage,
                        current,
                    },
                    phase: hold ? { status: "held", hold } : { status: "idle" },
                },
            };
        }
        case "rowsLoaded": {
            if (!workflow || sending(workflow)) return screen;
            const hold = holdOf(workflow, event.live);
            // Only an ending replaces a hold, and nothing replaces an ending.
            if (
                !hold ||
                (workflow.phase.status === "held" &&
                    (isEnding(workflow.phase.hold) || !isEnding(hold)))
            )
                return screen;
            return {
                ...screen,
                workflow: { ...workflow, phase: { status: "held", hold } },
            };
        }
    }
}

// The server strips the reason before it compares a proposal with an earlier one.
function contentOf(change: OutageChange) {
    return {
        action: change.action,
        reason: serverTrim(change.reason),
        ...(change.action === "create"
            ? {}
            : { incident_id: change.outage.id }),
        ...(change.action === "create" || change.action === "update"
            ? { values: change.values }
            : {}),
        ...(change.action === "resolve" ? { ends_at: change.ends_at } : {}),
    };
}

function proposalBody(change: OutageChange) {
    return {
        ...contentOf(change),
        ...(change.action === "create"
            ? {}
            : { expected_revision: change.outage.revision }),
    };
}

// The configuration the server validates a change against: the plant's current one for a new or ongoing outage,
// the outage's own once it has an end, none for a resolution or a cancellation.
function configurationOf(change: OutageChange, current: number | null) {
    if (change.action === "create") return current;
    if (change.action !== "update") return null;
    return change.values.ends_at == null
        ? current
        : change.outage.declared_configuration_id;
}

// The request the input asks for under the viewer's access and the plant's configuration, whatever revision it meets.
export function attemptPayload(
    change: OutageChange,
    direct: boolean,
    configuration: number | null,
) {
    const { reason, ...content } = contentOf(change);
    return JSON.stringify({
        direct,
        configuration: configurationOf(change, configuration),
        ...content,
        ...(direct ? {} : { reason }),
    });
}

const VALUE_FIELDS = [
    "kind",
    "title",
    "starts_at",
    "ends_at",
    "available_dc_mw",
    "available_inverter_mw",
    "available_export_mw",
    "notes",
] as const satisfies readonly (keyof OutageInput)[];

const comparable = (field: string, value: unknown) =>
    field.endsWith("_at") && typeof value === "string"
        ? Date.parse(value)
        : value;

function lostAnswerApplied({ change, current, direct }: SendFailed): boolean {
    if (
        !direct ||
        change.action !== "update" ||
        current?.id !== change.outage.id
    )
        return false;
    const { outage, values } = change;
    return (
        current.revision === outage.revision + 1 &&
        current.status !== "cancelled" &&
        VALUE_FIELDS.every(
            (field) =>
                comparable(
                    field,
                    values[field] ?? (field === "notes" ? "" : null),
                ) === comparable(field, current[field]),
        )
    );
}

export async function submitOutageChange(
    scope: PlantScope,
    change: OutageChange,
    { key, direct }: { key: string; direct: boolean },
) {
    if (!direct)
        return api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/change-requests/incidents/",
            {
                path: scope,
                body: { ...proposalBody(change), idempotency_key: key },
            },
        );
    if (change.action === "create") {
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/",
            { path: scope, body: { ...change.values, idempotency_key: key } },
        );
        return null;
    }
    const path = { ...scope, incident_id: change.outage.id };
    const expected_revision = change.outage.revision;
    if (change.action === "update")
        await api.put(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/{incident_id}/",
            {
                path,
                body: { ...change.values, expected_revision },
            },
        );
    else if (change.action === "resolve")
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/{incident_id}/resolve/",
            {
                path,
                body: { expected_revision, ends_at: change.ends_at },
            },
        );
    else
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/{incident_id}/cancel/",
            {
                path,
                body: { expected_revision },
            },
        );
    return null;
}

function isOutage(value: unknown): value is OutageRow {
    return (
        !!value &&
        typeof value === "object" &&
        "id" in value &&
        typeof value.id === "number" &&
        "revision" in value &&
        typeof value.revision === "number"
    );
}

// The outage as it now stands, with the member's own pending request, from a conflict answer about this outage.
export function staleVersion(error: unknown, id: number): OutageRow | null {
    if (
        !(error instanceof ApiError) ||
        error.code !== "incident_revision_changed" ||
        !error.body ||
        typeof error.body !== "object" ||
        !("current" in error.body)
    )
        return null;
    const current = error.body.current;
    return isOutage(current) && current.id === id ? current : null;
}

const OUTAGE_FIELDS = [
    "status",
    ...VALUE_FIELDS,
] as const satisfies readonly (keyof Outage)[];

export type OutageField = (typeof OUTAGE_FIELDS)[number];

export function changedFields(before: Outage, after: Outage): OutageField[] {
    return OUTAGE_FIELDS.filter((field) => before[field] !== after[field]);
}

export function capacityImpact(
    outage?: Outage,
): "total" | "reduced" | "unknown" {
    if (!outage) return "total";
    const values = [
        outage.available_dc_mw,
        outage.available_inverter_mw,
        outage.available_export_mw,
    ];
    if (values.every((value) => value === null)) return "unknown";
    return values.every((value) => value === 0) ? "total" : "reduced";
}
