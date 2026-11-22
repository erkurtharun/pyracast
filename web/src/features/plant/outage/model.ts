import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
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

type Phase =
    { status: "idle" } | { status: "sending" } | { status: "held"; hold: Hold };

type Target =
    | { kind: "create" }
    | { kind: "update" | "resolve" | "cancel"; outage: Outage };

// `outage` is the version the workflow sends against; `form` counts restarts of its form after a discarded stale change.
export type Workflow = Target & {
    phase: Phase;
    attempt: { payload: string; key: string } | null;
    stale: Stale | null;
    form: number;
};

// The one state of the outage screen: at most one open workflow, and whether a proposal was just sent.
export type OutageScreen = { workflow: Workflow | null; submitted: boolean };

export const INITIAL_OUTAGES: OutageScreen = {
    workflow: null,
    submitted: false,
};

export type OutageEvent =
    | { type: "open"; target: Target; live: Live }
    | { type: "close" }
    | { type: "discard" }
    | { type: "send"; payload: string; key: string }
    // The answer and the reload that follows it have both finished.
    | { type: "sent"; proposed: boolean }
    | { type: "sendFailed"; current: OutageRow | null; direct: boolean }
    | { type: "rowsLoaded"; live: Live };

export type OutageControls = {
    create: boolean;
    acts: boolean;
    offered: ReadonlySet<number>;
    send: boolean;
    editable: boolean;
    close: boolean;
    hold: Hold | null;
};

const atLimit = (live: Live) =>
    live.offer?.proposal_unavailable_reason === "pending_request_limit";

const sending = (workflow: Workflow | null) =>
    workflow?.phase.status === "sending";

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
                submitted: false,
                workflow: {
                    ...target,
                    phase: { status: "idle" },
                    attempt: null,
                    stale: null,
                    form: 0,
                },
            };
        }
        case "close":
            if (!workflow || sending(workflow)) return screen;
            return { ...screen, workflow: null };
        case "discard":
            if (!workflow?.stale || sending(workflow)) return screen;
            // A held workflow cannot send, so discarding its change leaves nothing to start again.
            if (workflow.phase.status === "held")
                return { ...screen, workflow: null };
            return {
                ...screen,
                workflow: { ...workflow, stale: null, form: workflow.form + 1 },
            };
        case "send": {
            if (workflow?.phase.status !== "idle") return screen;
            const attempt =
                workflow.attempt?.payload === event.payload
                    ? workflow.attempt
                    : { payload: event.payload, key: event.key };
            return {
                submitted: false,
                workflow: {
                    ...workflow,
                    phase: { status: "sending" },
                    attempt,
                },
            };
        }
        case "sent":
            if (!sending(workflow)) return screen;
            return { workflow: null, submitted: event.proposed };
        case "sendFailed": {
            if (workflow?.phase.status !== "sending") return screen;
            const { current } = event;
            // Only a conflict about the outage the workflow sends against moves it to that outage's current version.
            if (
                workflow.kind === "create" ||
                current?.id !== workflow.outage.id
            )
                return {
                    ...screen,
                    workflow: { ...workflow, phase: { status: "idle" } },
                };
            const hold =
                endingOf(workflow.kind, current) ??
                (!event.direct && current.pending_request_id != null
                    ? "held"
                    : null);
            return {
                ...screen,
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

function proposalBody(change: OutageChange) {
    const target =
        change.action === "create"
            ? {}
            : {
                  incident_id: change.outage.id,
                  expected_revision: change.outage.revision,
              };
    return {
        action: change.action,
        reason: change.reason,
        ...target,
        ...(change.action === "create" || change.action === "update"
            ? { values: change.values }
            : {}),
        ...(change.action === "resolve" ? { ends_at: change.ends_at } : {}),
    };
}

export function attemptPayload(change: OutageChange, direct: boolean) {
    return JSON.stringify(direct ? change : proposalBody(change));
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

export function alreadySaved(
    change: OutageChange,
    current: Outage | null,
    direct: boolean,
): boolean {
    if (!direct || change.action !== "update" || !current) return false;
    const { values } = change;
    return VALUE_FIELDS.every(
        (field) =>
            comparable(
                field,
                values[field] ?? (field === "notes" ? "" : null),
            ) === comparable(field, current[field]),
    );
}

export async function submitOutageChange(
    scope: PlantScope,
    change: OutageChange,
    { key, direct }: { key: string; direct: boolean },
) {
    if (!direct) {
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/change-requests/incidents/",
            {
                path: scope,
                body: { ...proposalBody(change), idempotency_key: key },
            },
        );
        return;
    }
    if (change.action === "create") {
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/",
            { path: scope, body: { ...change.values, idempotency_key: key } },
        );
        return;
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
