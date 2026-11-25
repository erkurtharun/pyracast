import { expect, test } from "@playwright/test";
import { ApiError, UnconfirmedError } from "../src/lib/api/errors";
import {
    attemptPayload,
    INITIAL_OUTAGES,
    outageControls,
    reduceOutages,
    type Live,
    type Outage,
    type OutageChange,
    type OutageControls,
    type OutageEvent,
    type OutageRow,
    type OutageScreen,
} from "../src/features/plant/outage/model";

function outage(id: number, fields: Partial<OutageRow> = {}): OutageRow {
    return {
        id,
        revision: 2,
        kind: "inverter",
        title: `Outage ${id}`,
        notes: "",
        starts_at: "2026-11-09T08:00:00Z",
        ends_at: null,
        available_dc_mw: null,
        available_inverter_mw: null,
        available_export_mw: null,
        status: "active",
        declared_configuration_id: 31,
        created_at: "2026-11-09T08:10:00Z",
        updated_at: "2026-11-09T08:10:00Z",
        pending_request_id: null,
        ...fields,
    };
}

// The list a member reads: outages 4 and 5, one of them holding the member's own request, or the limit reached.
const listed = (held: number | null = null, reason: string | null = null) => ({
    can_propose: reason === null,
    proposal_unavailable_reason: reason,
    results: [4, 5].map((id) =>
        outage(id, { pending_request_id: id === held ? 9 : null }),
    ),
});
// The same list after someone else cancelled or resolved outage 4, or once outage 4 has left it.
const endedFour = (fields: Partial<OutageRow>) => ({
    ...listed(),
    results: [outage(4, fields), outage(5)],
});
const withoutFour = () => ({ ...listed(), results: [outage(5)] });
const member = (offer = listed()): Live => ({
    offer,
    direct: false,
    configured: true,
});
const admin = (offer = listed()): Live => ({
    offer: {
        ...offer,
        can_propose: false,
        proposal_unavailable_reason: "admin_edits_directly",
    },
    direct: true,
    configured: true,
});

type Step = { screen: OutageScreen; live: Live };

function run(step: Step, ...events: OutageEvent[]): Step {
    return {
        live: step.live,
        screen: events.reduce(reduceOutages, step.screen),
    };
}

// The list reloads: the screen sees the rows it now shows.
function loaded(step: Step, offer: ReturnType<typeof listed>): Step {
    const live = step.live.direct ? admin(offer) : member(offer);
    return {
        live,
        screen: reduceOutages(step.screen, { type: "rowsLoaded", live }),
    };
}

// The member's access changes while the list stays as it was read.
const access = (step: Step, direct: boolean): Step => {
    const live = { ...step.live, direct };
    return {
        live,
        screen: reduceOutages(step.screen, { type: "rowsLoaded", live }),
    };
};

const rowOf = (step: Step, id: number) =>
    step.live.offer?.results.find((row) => row.id === id) ?? outage(id);

const open =
    (kind: "create" | "update" | "resolve" | "cancel", id = 4) =>
    (step: Step) =>
        run(step, {
            type: "open",
            target:
                kind === "create"
                    ? { kind }
                    : { kind, outage: rowOf(step, id) },
            live: step.live,
        });

type Values = Extract<OutageChange, { action: "update" }>["values"];

const valuesOf = (row: Outage): Values => ({
    kind: row.kind,
    title: row.title,
    notes: row.notes,
    starts_at: row.starts_at,
    ends_at: row.ends_at,
    available_dc_mw: row.available_dc_mw,
    available_inverter_mw: row.available_inverter_mw,
    available_export_mw: row.available_export_mw,
});

function sentChange({ screen }: Step): OutageChange {
    const { workflow } = screen;
    if (!workflow || workflow.kind === "create")
        return { action: "create", values: valuesOf(outage(4)), reason: "" };
    const target = workflow.outage;
    if (workflow.kind === "update")
        return {
            action: "update",
            outage: target,
            values: valuesOf(target),
            reason: "",
        };
    if (workflow.kind === "resolve")
        return {
            action: "resolve",
            outage: target,
            ends_at: "2026-11-10T10:00:00Z",
            reason: "",
        };
    return { action: "cancel", outage: target, reason: "" };
}

const refused = () => new ApiError("Refused.", { status: 409 });
const lostAnswer = () => new UnconfirmedError("Not confirmed.");
const throttled = () =>
    new ApiError("Too many requests.", { status: 429, code: "throttled" });

const failed = (
    step: Step,
    current: OutageRow | null,
    error: unknown = refused(),
) =>
    run(step, {
        type: "sendFailed",
        change: sentChange(step),
        current,
        error,
        direct: step.live.direct,
    });

const events = {
    openCreate: open("create"),
    openUpdate: open("update"),
    openResolve: open("resolve"),
    openCancel: open("cancel"),
    openOther: open("update", 5),
    close: (step: Step) => run(step, { type: "close" }),
    discard: (step: Step) => run(step, { type: "discard" }),
    send: (step: Step) =>
        run(step, {
            type: "send",
            payload: "first",
            key: "key-1",
            change: sentChange(step),
        }),
    answered: (step: Step) => run(step, { type: "answered" }),
    sent: (step: Step) =>
        run(step, {
            type: "sent",
            receipt: step.live.direct ? null : "pending",
        }),
    fail: (step: Step) => failed(step, null),
    lost: (step: Step) => failed(step, null, lostAnswer()),
    // A conflict answer carries the outage as it now stands, with the member's own pending request when there is one.
    conflict: (step: Step) =>
        failed(step, outage(4, { revision: 3, title: "Changed" })),
    conflictSame: (step: Step) => failed(step, outage(4, { revision: 3 })),
    conflictHeld: (step: Step) =>
        failed(step, outage(4, { revision: 3, pending_request_id: 9 })),
    conflictCancelled: (step: Step) =>
        failed(step, outage(4, { revision: 3, status: "cancelled" })),
    rowsHeld: (step: Step) => loaded(step, listed(4)),
    rowsLimit: (step: Step) =>
        loaded(step, listed(null, "pending_request_limit")),
    rowsFree: (step: Step) => loaded(step, listed()),
    rowsCancelled: (step: Step) =>
        loaded(step, endedFour({ revision: 3, status: "cancelled" })),
    rowsResolved: (step: Step) =>
        loaded(
            step,
            endedFour({
                revision: 3,
                status: "resolved",
                ends_at: "2026-11-10T10:00:00Z",
            }),
        ),
    rowsRemoved: (step: Step) => loaded(step, withoutFour()),
    demote: (step: Step) => access(step, false),
    promote: (step: Step) => access(step, true),
} satisfies Record<string, (step: Step) => Step>;

type EventName = keyof typeof events;

function phase({ screen, live }: Step) {
    const { workflow } = screen;
    const rows = live.offer?.results ?? [];
    const shown = [
        ...rows
            .filter((row) => row.pending_request_id != null)
            .map((row) => `${row.id}held`),
        ...rows
            .filter((row) => row.status !== "active")
            .map((row) => `${row.id}${row.status}`),
        ...(rows.some((row) => row.id === 4) ? [] : ["4gone"]),
        ...(live.offer?.proposal_unavailable_reason === "pending_request_limit"
            ? ["limit"]
            : []),
    ];
    const hold = (phase: NonNullable<typeof workflow>["phase"]) =>
        phase.status !== "held"
            ? ""
            : phase.hold === "held"
              ? ":own"
              : phase.hold === "pending_request_limit"
                ? ":limit"
                : `:${phase.hold}`;
    const status = !workflow
        ? "none"
        : [
              workflow.kind,
              workflow.kind === "create" ? "" : workflow.outage.id,
              "/",
              workflow.phase.status,
              hold(workflow.phase),
          ].join("");
    return [
        live.direct ? "direct:" : "",
        status,
        workflow?.stale ? "+stale" : "",
        workflow?.attempt ? "+lost" : "",
        screen.receipt ? "+sent" : "",
        shown.length ? `|${shown.join(",")}` : "",
    ].join("");
}

const start: Step = { screen: INITIAL_OUTAGES, live: member() };
const directStart: Step = { screen: INITIAL_OUTAGES, live: admin() };

const states = {
    none: start,
    "none+sent": events.sent(events.send(events.openUpdate(start))),
    "none|4held": { screen: INITIAL_OUTAGES, live: member(listed(4)) },
    "none|limit": {
        screen: INITIAL_OUTAGES,
        live: member(listed(null, "pending_request_limit")),
    },
    "create/idle": events.openCreate(start),
    "create/sending": events.send(events.openCreate(start)),
    "create/held:limit|limit": events.rowsLimit(events.openCreate(start)),
    "update4/idle": events.openUpdate(start),
    "update4/idle+stale": events.conflict(
        events.send(events.openUpdate(start)),
    ),
    "update4/sending": events.send(events.openUpdate(start)),
    "update4/sending+stale": events.send(
        events.conflict(events.send(events.openUpdate(start))),
    ),
    "update4/held:own|4held": events.rowsHeld(events.openUpdate(start)),
    "update4/held:own+stale": events.conflictHeld(
        events.send(events.openUpdate(start)),
    ),
    "update4/held:limit|limit": events.rowsLimit(events.openUpdate(start)),
    "resolve4/idle": events.openResolve(start),
    "resolve4/sending": events.send(events.openResolve(start)),
    "resolve4/held:own|4held": events.rowsHeld(events.openResolve(start)),
    "cancel4/idle": events.openCancel(start),
    "cancel4/idle+stale": events.conflict(
        events.send(events.openCancel(start)),
    ),
    "cancel4/sending": events.send(events.openCancel(start)),
    "cancel4/held:own|4held": events.rowsHeld(events.openCancel(start)),
    "update5/idle": events.openOther(start),
    "direct:none": directStart,
    "direct:none|4held": { screen: INITIAL_OUTAGES, live: admin(listed(4)) },
    "direct:update4/idle": events.openUpdate(directStart),
    "direct:update4/sending": events.send(events.openUpdate(directStart)),
    "direct:update4/sending+lost": events.send(
        events.lost(events.send(events.openUpdate(directStart))),
    ),
    "direct:update4/saved": events.conflictSame(
        events.send(events.lost(events.send(events.openUpdate(directStart)))),
    ),
    "none|4resolved": {
        screen: INITIAL_OUTAGES,
        live: member(
            endedFour({ status: "resolved", ends_at: "2026-11-10T10:00:00Z" }),
        ),
    },
    "resolve4/held:cancelled|4cancelled": events.rowsCancelled(
        events.openResolve(start),
    ),
    "resolve4/held:resolved|4resolved": events.rowsResolved(
        events.openResolve(start),
    ),
    "cancel4/held:cancelled|4cancelled": events.rowsCancelled(
        events.openCancel(start),
    ),
    "cancel4/held:removed|4gone": events.rowsRemoved(events.openCancel(start)),
    "cancel4/held:cancelled+stale": events.conflictCancelled(
        events.send(events.openCancel(start)),
    ),
    "direct:update4/idle|4held": events.openUpdate({
        screen: INITIAL_OUTAGES,
        live: admin(listed(4)),
    }),
} satisfies Record<string, Step>;

type StateName = keyof typeof states;

// A refused event returns the very same screen, marked "=".
const same = (name: StateName) =>
    Object.fromEntries(
        Object.keys(events).map((event) => [event, `${name}=`]),
    ) as Record<EventName, string>;

// A reload that leaves the screen as it was: the same state beside each list.
const unchanged = (base: string, direct = false) => ({
    rowsHeld: `${base}|4held=`,
    rowsLimit: direct ? `${base}=` : `${base}|limit=`,
    rowsFree: `${base}=`,
    rowsCancelled: `${base}|4cancelled=`,
    rowsResolved: `${base}|4resolved=`,
    rowsRemoved: `${base}|4gone=`,
});

// Every state against every event: the class of the next state.
const expected: Record<StateName, Record<EventName, string>> = {
    none: {
        ...same("none"),
        ...unchanged("none"),
        openCreate: "create/idle",
        openUpdate: "update4/idle",
        openResolve: "resolve4/idle",
        openCancel: "cancel4/idle",
        openOther: "update5/idle",
        promote: "direct:none=",
    },
    "none+sent": {
        ...same("none+sent"),
        ...unchanged("none+sent"),
        openCreate: "create/idle",
        openUpdate: "update4/idle",
        openResolve: "resolve4/idle",
        openCancel: "cancel4/idle",
        openOther: "update5/idle",
        promote: "direct:none+sent=",
    },
    "none|4held": {
        ...same("none|4held"),
        ...unchanged("none"),
        openCreate: "create/idle|4held",
        openOther: "update5/idle|4held",
        promote: "direct:none|4held=",
    },
    "none|limit": {
        ...same("none|limit"),
        ...unchanged("none"),
        promote: "direct:none|limit=",
    },
    "create/idle": {
        ...same("create/idle"),
        ...unchanged("create/idle"),
        openUpdate: "update4/idle",
        openResolve: "resolve4/idle",
        openCancel: "cancel4/idle",
        openOther: "update5/idle",
        close: "none",
        send: "create/sending",
        rowsLimit: "create/held:limit|limit",
        promote: "direct:create/idle=",
    },
    "create/sending": {
        ...same("create/sending"),
        ...unchanged("create/sending"),
        answered: "create/saved",
        sent: "none+sent",
        fail: "create/idle",
        lost: "create/idle+lost",
        conflict: "create/idle",
        conflictSame: "create/idle",
        conflictHeld: "create/idle",
        conflictCancelled: "create/idle",
        promote: "direct:create/sending=",
    },
    "create/held:limit|limit": {
        ...same("create/held:limit|limit"),
        ...unchanged("create/held:limit"),
        close: "none|limit",
        promote: "direct:create/held:limit|limit=",
    },
    "update4/idle": {
        ...same("update4/idle"),
        ...unchanged("update4/idle"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        send: "update4/sending",
        rowsHeld: "update4/held:own|4held",
        rowsLimit: "update4/held:limit|limit",
        rowsCancelled: "update4/held:cancelled|4cancelled",
        rowsRemoved: "update4/held:removed|4gone",
        promote: "direct:update4/idle=",
    },
    "update4/idle+stale": {
        ...same("update4/idle+stale"),
        ...unchanged("update4/idle+stale"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        discard: "update4/idle",
        send: "update4/sending+stale",
        rowsHeld: "update4/held:own+stale|4held",
        rowsLimit: "update4/held:limit+stale|limit",
        rowsCancelled: "update4/held:cancelled+stale|4cancelled",
        rowsRemoved: "update4/held:removed+stale|4gone",
        promote: "direct:update4/idle+stale=",
    },
    "update4/sending": {
        ...same("update4/sending"),
        ...unchanged("update4/sending"),
        answered: "update4/saved",
        sent: "none+sent",
        fail: "update4/idle",
        lost: "update4/idle+lost",
        conflict: "update4/idle+stale",
        conflictSame: "update4/idle+stale",
        conflictHeld: "update4/held:own+stale",
        conflictCancelled: "update4/held:cancelled+stale",
        promote: "direct:update4/sending=",
    },
    "update4/held:own|4held": {
        ...same("update4/held:own|4held"),
        ...unchanged("update4/held:own"),
        openCreate: "create/idle|4held",
        openOther: "update5/idle|4held",
        close: "none|4held",
        rowsCancelled: "update4/held:cancelled|4cancelled",
        rowsRemoved: "update4/held:removed|4gone",
        promote: "direct:update4/held:own|4held=",
    },
    "update4/held:own+stale": {
        ...same("update4/held:own+stale"),
        ...unchanged("update4/held:own+stale"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        discard: "none",
        rowsCancelled: "update4/held:cancelled+stale|4cancelled",
        rowsRemoved: "update4/held:removed+stale|4gone",
        promote: "direct:update4/held:own+stale=",
    },
    "update4/held:limit|limit": {
        ...same("update4/held:limit|limit"),
        ...unchanged("update4/held:limit"),
        close: "none|limit",
        rowsCancelled: "update4/held:cancelled|4cancelled",
        rowsRemoved: "update4/held:removed|4gone",
        promote: "direct:update4/held:limit|limit=",
    },
    "resolve4/idle": {
        ...same("resolve4/idle"),
        ...unchanged("resolve4/idle"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        send: "resolve4/sending",
        rowsHeld: "resolve4/held:own|4held",
        rowsLimit: "resolve4/held:limit|limit",
        rowsCancelled: "resolve4/held:cancelled|4cancelled",
        rowsResolved: "resolve4/held:resolved|4resolved",
        rowsRemoved: "resolve4/held:removed|4gone",
        promote: "direct:resolve4/idle=",
    },
    "resolve4/sending": {
        ...same("resolve4/sending"),
        ...unchanged("resolve4/sending"),
        answered: "resolve4/saved",
        sent: "none+sent",
        fail: "resolve4/idle",
        lost: "resolve4/idle+lost",
        conflict: "resolve4/idle+stale",
        conflictSame: "resolve4/idle+stale",
        conflictHeld: "resolve4/held:own+stale",
        conflictCancelled: "resolve4/held:cancelled+stale",
        promote: "direct:resolve4/sending=",
    },
    "resolve4/held:own|4held": {
        ...same("resolve4/held:own|4held"),
        ...unchanged("resolve4/held:own"),
        openCreate: "create/idle|4held",
        openOther: "update5/idle|4held",
        close: "none|4held",
        rowsCancelled: "resolve4/held:cancelled|4cancelled",
        rowsResolved: "resolve4/held:resolved|4resolved",
        rowsRemoved: "resolve4/held:removed|4gone",
        promote: "direct:resolve4/held:own|4held=",
    },
    "cancel4/idle": {
        ...same("cancel4/idle"),
        ...unchanged("cancel4/idle"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        send: "cancel4/sending",
        rowsHeld: "cancel4/held:own|4held",
        rowsLimit: "cancel4/held:limit|limit",
        rowsCancelled: "cancel4/held:cancelled|4cancelled",
        rowsRemoved: "cancel4/held:removed|4gone",
        promote: "direct:cancel4/idle=",
    },
    "cancel4/idle+stale": {
        ...same("cancel4/idle+stale"),
        ...unchanged("cancel4/idle+stale"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        discard: "cancel4/idle",
        send: "cancel4/sending+stale",
        rowsHeld: "cancel4/held:own+stale|4held",
        rowsLimit: "cancel4/held:limit+stale|limit",
        rowsCancelled: "cancel4/held:cancelled+stale|4cancelled",
        rowsRemoved: "cancel4/held:removed+stale|4gone",
        promote: "direct:cancel4/idle+stale=",
    },
    "cancel4/sending": {
        ...same("cancel4/sending"),
        ...unchanged("cancel4/sending"),
        answered: "cancel4/saved",
        sent: "none+sent",
        fail: "cancel4/idle",
        lost: "cancel4/idle+lost",
        conflict: "cancel4/idle+stale",
        conflictSame: "cancel4/idle+stale",
        conflictHeld: "cancel4/held:own+stale",
        conflictCancelled: "cancel4/held:cancelled+stale",
        promote: "direct:cancel4/sending=",
    },
    "cancel4/held:own|4held": {
        ...same("cancel4/held:own|4held"),
        ...unchanged("cancel4/held:own"),
        openCreate: "create/idle|4held",
        openOther: "update5/idle|4held",
        close: "none|4held",
        rowsCancelled: "cancel4/held:cancelled|4cancelled",
        rowsRemoved: "cancel4/held:removed|4gone",
        promote: "direct:cancel4/held:own|4held=",
    },
    "update5/idle": {
        ...same("update5/idle"),
        ...unchanged("update5/idle"),
        openCreate: "create/idle",
        openUpdate: "update4/idle",
        openResolve: "resolve4/idle",
        openCancel: "cancel4/idle",
        close: "none",
        send: "update5/sending",
        rowsLimit: "update5/held:limit|limit",
        promote: "direct:update5/idle=",
    },
    "direct:none": {
        ...same("direct:none"),
        ...unchanged("direct:none", true),
        openCreate: "direct:create/idle",
        openUpdate: "direct:update4/idle",
        openResolve: "direct:resolve4/idle",
        openCancel: "direct:cancel4/idle",
        openOther: "direct:update5/idle",
        demote: "none=",
    },
    "direct:none|4held": {
        ...same("direct:none|4held"),
        ...unchanged("direct:none", true),
        openCreate: "direct:create/idle|4held",
        openUpdate: "direct:update4/idle|4held",
        openResolve: "direct:resolve4/idle|4held",
        openCancel: "direct:cancel4/idle|4held",
        openOther: "direct:update5/idle|4held",
        demote: "none|4held=",
    },
    "direct:update4/idle": {
        ...same("direct:update4/idle"),
        ...unchanged("direct:update4/idle", true),
        openCreate: "direct:create/idle",
        openOther: "direct:update5/idle",
        close: "direct:none",
        send: "direct:update4/sending",
        rowsCancelled: "direct:update4/held:cancelled|4cancelled",
        rowsRemoved: "direct:update4/held:removed|4gone",
        demote: "update4/idle=",
    },
    "direct:update4/sending": {
        ...same("direct:update4/sending"),
        ...unchanged("direct:update4/sending", true),
        answered: "direct:update4/saved",
        sent: "direct:none",
        fail: "direct:update4/idle",
        lost: "direct:update4/idle+lost",
        conflict: "direct:update4/idle+stale",
        conflictSame: "direct:update4/idle+stale",
        conflictHeld: "direct:update4/idle+stale",
        conflictCancelled: "direct:update4/held:cancelled+stale",
        demote: "update4/sending=",
    },
    "direct:update4/sending+lost": {
        ...same("direct:update4/sending+lost"),
        ...unchanged("direct:update4/sending+lost", true),
        answered: "direct:update4/saved",
        sent: "direct:none",
        fail: "direct:update4/idle",
        lost: "direct:update4/idle+lost",
        conflict: "direct:update4/idle+stale",
        conflictSame: "direct:update4/saved",
        conflictHeld: "direct:update4/saved",
        conflictCancelled: "direct:update4/held:cancelled+stale",
        demote: "update4/sending+lost=",
    },
    "direct:update4/saved": {
        ...same("direct:update4/saved"),
        ...unchanged("direct:update4/saved", true),
        sent: "direct:none",
        demote: "update4/saved=",
    },
    "update4/sending+stale": {
        ...same("update4/sending+stale"),
        ...unchanged("update4/sending+stale"),
        answered: "update4/saved+stale",
        sent: "none+sent",
        fail: "update4/idle+stale",
        lost: "update4/idle+stale+lost",
        conflict: "update4/idle+stale",
        conflictSame: "update4/idle+stale",
        conflictHeld: "update4/held:own+stale",
        conflictCancelled: "update4/held:cancelled+stale",
        promote: "direct:update4/sending+stale=",
    },
    // A resolved outage takes no resolution, but still a change or a cancellation.
    "none|4resolved": {
        ...same("none|4resolved"),
        ...unchanged("none"),
        openCreate: "create/idle|4resolved",
        openUpdate: "update4/idle|4resolved",
        openCancel: "cancel4/idle|4resolved",
        openOther: "update5/idle|4resolved",
        promote: "direct:none|4resolved=",
    },
    // A workflow whose outage ended stays held so until closed; a later list changes nothing.
    "resolve4/held:cancelled|4cancelled": {
        ...same("resolve4/held:cancelled|4cancelled"),
        ...unchanged("resolve4/held:cancelled"),
        openCreate: "create/idle|4cancelled",
        openOther: "update5/idle|4cancelled",
        close: "none|4cancelled",
        promote: "direct:resolve4/held:cancelled|4cancelled=",
    },
    "resolve4/held:resolved|4resolved": {
        ...same("resolve4/held:resolved|4resolved"),
        ...unchanged("resolve4/held:resolved"),
        openCreate: "create/idle|4resolved",
        openOther: "update5/idle|4resolved",
        close: "none|4resolved",
        promote: "direct:resolve4/held:resolved|4resolved=",
    },
    "cancel4/held:cancelled|4cancelled": {
        ...same("cancel4/held:cancelled|4cancelled"),
        ...unchanged("cancel4/held:cancelled"),
        openCreate: "create/idle|4cancelled",
        openOther: "update5/idle|4cancelled",
        close: "none|4cancelled",
        promote: "direct:cancel4/held:cancelled|4cancelled=",
    },
    "cancel4/held:removed|4gone": {
        ...same("cancel4/held:removed|4gone"),
        ...unchanged("cancel4/held:removed"),
        openCreate: "create/idle|4gone",
        openOther: "update5/idle|4gone",
        close: "none|4gone",
        promote: "direct:cancel4/held:removed|4gone=",
    },
    // Discarding the change of a workflow that can no longer send closes it.
    "cancel4/held:cancelled+stale": {
        ...same("cancel4/held:cancelled+stale"),
        ...unchanged("cancel4/held:cancelled+stale"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        discard: "none",
        promote: "direct:cancel4/held:cancelled+stale=",
    },
    // An administrator who loses the right to write directly is held by the request they already have.
    "direct:update4/idle|4held": {
        ...same("direct:update4/idle|4held"),
        ...unchanged("direct:update4/idle", true),
        openCreate: "direct:create/idle|4held",
        openOther: "direct:update5/idle|4held",
        close: "direct:none|4held",
        send: "direct:update4/sending|4held",
        rowsCancelled: "direct:update4/held:cancelled|4cancelled",
        rowsRemoved: "direct:update4/held:removed|4gone",
        demote: "update4/held:own|4held",
    },
};

test("every state of the outage screen meets every event as the table says", () => {
    for (const [name, state] of Object.entries(states))
        expect(phase(state), `fixture ${name}`).toBe(name);
    const actual = Object.fromEntries(
        Object.entries(states).map(([name, state]) => [
            name,
            Object.fromEntries(
                Object.entries(events).map(([event, run]) => {
                    const next = run(state);
                    const kept = next.screen === state.screen ? "=" : "";
                    return [event, `${phase(next)}${kept}`];
                }),
            ),
        ]),
    );
    expect(actual).toEqual(expected);
});

test("no event other than its answer replaces, closes or changes a workflow that is sending", () => {
    const answers = new Set([
        "answered",
        "sent",
        "fail",
        "lost",
        "conflict",
        "conflictSame",
        "conflictHeld",
        "conflictCancelled",
    ]);
    for (const [name, state] of Object.entries(states)) {
        const { workflow } = state.screen;
        if (
            workflow?.phase.status !== "sending" &&
            workflow?.phase.status !== "saved"
        )
            continue;
        for (const [event, run] of Object.entries(events))
            if (!answers.has(event))
                expect(run(state).screen.workflow, `${name} × ${event}`).toBe(
                    workflow,
                );
    }
});

function enabled({ screen, live }: Step) {
    const derived = outageControls(screen, live);
    return {
        create: derived.create,
        acts: derived.acts,
        offered: [...derived.offered],
        send: derived.send,
        editable: derived.editable,
        close: derived.close,
        hold: derived.hold,
        pending: derived.pending,
    };
}

const quiet = {
    create: true,
    acts: true,
    offered: [4, 5],
    send: false,
    editable: false,
    close: false,
    hold: null,
    pending: null,
};
const open4 = {
    ...quiet,
    offered: [5],
    send: true,
    editable: true,
    close: true,
};
const sending4 = {
    ...open4,
    acts: false,
    send: false,
    close: false,
    pending: "unanswered" as const,
};
const heldBy = (hold: NonNullable<OutageControls["hold"]>) => ({
    ...open4,
    send: false,
    editable: false,
    hold,
});
const held4 = heldBy("held");
const atLimit = {
    ...quiet,
    create: false,
    offered: [],
    editable: false,
    close: true,
    hold: "pending_request_limit" as const,
};

test("every state of the outage screen enables only the controls whose event it accepts", () => {
    const table: Record<StateName, ReturnType<typeof enabled>> = {
        none: quiet,
        "none+sent": quiet,
        "none|4held": { ...quiet, offered: [5] },
        "none|limit": { ...quiet, create: false, offered: [] },
        "create/idle": { ...open4, offered: [4, 5] },
        "create/sending": { ...sending4, offered: [4, 5] },
        "create/held:limit|limit": atLimit,
        "update4/idle": open4,
        "update4/idle+stale": open4,
        "update4/sending": sending4,
        "update4/sending+stale": sending4,
        "update4/held:own|4held": held4,
        "update4/held:own+stale": held4,
        "update4/held:limit|limit": atLimit,
        "resolve4/idle": open4,
        "resolve4/sending": sending4,
        "resolve4/held:own|4held": held4,
        "cancel4/idle": open4,
        "cancel4/idle+stale": open4,
        "cancel4/sending": sending4,
        "cancel4/held:own|4held": held4,
        "update5/idle": { ...open4, offered: [4] },
        "direct:none": quiet,
        "direct:none|4held": quiet,
        "direct:update4/idle": open4,
        "direct:update4/sending": sending4,
        "direct:update4/sending+lost": sending4,
        "direct:update4/saved": { ...sending4, pending: "confirmed" },
        "none|4resolved": quiet,
        "resolve4/held:cancelled|4cancelled": heldBy("cancelled"),
        "resolve4/held:resolved|4resolved": heldBy("resolved"),
        "cancel4/held:cancelled|4cancelled": heldBy("cancelled"),
        "cancel4/held:removed|4gone": heldBy("removed"),
        "cancel4/held:cancelled+stale": heldBy("cancelled"),
        "direct:update4/idle|4held": open4,
    };
    expect(
        Object.fromEntries(
            Object.entries(states).map(([name, state]) => [
                name,
                enabled(state),
            ]),
        ),
    ).toEqual(table);
});

test("a cancelled outage offers no proposal, and an administrator is offered every other outage at the limit", () => {
    const withCancelled = {
        ...listed(),
        results: [outage(4), outage(5, { status: "cancelled" })],
    };
    expect([
        ...outageControls(INITIAL_OUTAGES, member(withCancelled)).offered,
    ]).toEqual([4]);
    expect([
        ...outageControls(INITIAL_OUTAGES, admin(withCancelled)).offered,
    ]).toEqual([4]);
    const direct = outageControls(INITIAL_OUTAGES, {
        offer: listed(4, "pending_request_limit"),
        direct: true,
        configured: true,
    });
    expect([direct.create, [...direct.offered]]).toEqual([true, [4, 5]]);
    expect(
        outageControls(INITIAL_OUTAGES, { ...admin(), configured: false })
            .create,
    ).toBe(false);
    const unread = { offer: undefined, direct: false, configured: true };
    expect(outageControls(INITIAL_OUTAGES, unread).create).toBe(false);
});

const sentKey = ({ screen }: Step) =>
    screen.workflow?.phase.status === "sending"
        ? screen.workflow.phase.sent.key
        : undefined;
const sendAs = (payload: string, key: string) => (step: Step) =>
    run(step, { type: "send", payload, key, change: sentChange(step) });

test("a send repeats the workflow's write whose answer was lost with its key, and any other send is a new request", () => {
    const first = sendAs("first", "key-1")(states["update4/idle"]);
    expect(sentKey(first)).toBe("key-1");
    const afterRefusal = sendAs("first", "key-2")(events.fail(first));
    expect(sentKey(afterRefusal)).toBe("key-2");
    const lost = events.lost(afterRefusal);
    expect(lost.screen.workflow?.attempt?.key).toBe("key-2");
    expect(sentKey(sendAs("first", "key-3")(lost))).toBe("key-2");
    const other = sendAs("second", "key-4")(lost);
    expect(sentKey(other)).toBe("key-4");
    const otherRefused = events.fail(other);
    expect(sentKey(sendAs("first", "key-5")(otherRefused))).toBe("key-2");
    const busy = failed(sendAs("first", "key-6")(lost), null, throttled());
    expect(sentKey(sendAs("first", "key-7")(busy))).toBe("key-2");
    const repeatRefused = events.fail(sendAs("first", "key-8")(lost));
    expect(repeatRefused.screen.workflow?.attempt).toBeNull();
    expect(sentKey(sendAs("first", "key-9")(repeatRefused))).toBe("key-9");
    const otherLost = events.lost(other);
    expect(otherLost.screen.workflow?.attempt?.key).toBe("key-4");
    expect(sentKey(sendAs("first", "key-10")(otherLost))).toBe("key-10");
    const reopened = events.openUpdate(events.close(otherRefused));
    expect(reopened.screen.workflow?.attempt).toBeNull();
    expect(sentKey(sendAs("first", "key-11")(reopened))).toBe("key-11");
});

test("a direct creation whose answer was lost keeps its key while the values sent stay the same, and an edit gets a new key", () => {
    const creation = (title: string): OutageChange => ({
        action: "create",
        values: {
            kind: "inverter",
            title,
            notes: "",
            starts_at: "2026-11-09T08:00:00Z",
            ends_at: null,
            available_dc_mw: 0,
            available_inverter_mw: 0,
            available_export_mw: 0,
        },
        reason: "",
    });
    const send = (step: Step, change: OutageChange, key: string) =>
        run(step, {
            type: "send",
            payload: attemptPayload(change, true, 31),
            key,
            change,
        });
    const first = send(
        events.openCreate(directStart),
        creation("Inverter offline"),
        "key-1",
    );
    const again = send(
        events.lost(first),
        creation("Inverter offline"),
        "key-2",
    );
    expect(sentKey(again)).toBe("key-1");
    const edited = send(
        events.lost(again),
        creation("Inverter tripped"),
        "key-3",
    );
    expect(sentKey(edited)).toBe("key-3");
});

test("a direct edit refused as stale ends as saved only when an unanswered try of the same change left exactly that write", () => {
    const sent: Values = {
        kind: "grid",
        title: "Grid trip",
        notes: "Feeder 2",
        starts_at: "2026-11-09T08:00:00.000Z",
        ends_at: null,
        available_dc_mw: 2.5,
        available_inverter_mw: null,
        available_export_mw: 0,
    };
    const written = outage(4, {
        ...sent,
        revision: 3,
        starts_at: "2026-11-09T11:00:00+03:00",
    });
    const edit = (values: Partial<Values> = {}): OutageChange => ({
        action: "update",
        outage: outage(4),
        values: { ...sent, ...values },
        reason: "",
    });
    const send = (step: Step, change: OutageChange) =>
        run(step, {
            type: "send",
            payload: attemptPayload(change, step.live.direct, 31),
            key: "key",
            change,
        });
    const refuse = (
        step: Step,
        change: OutageChange,
        current: OutageRow | null,
        error: unknown = refused(),
    ) =>
        run(step, {
            type: "sendFailed",
            change,
            current,
            error,
            direct: step.live.direct,
        });
    const lostOnce = (opened: Step, change = edit()) =>
        send(refuse(send(opened, change), change, null, lostAnswer()), change);
    const ending = (step: Step) => step.screen.workflow?.phase;
    const opened = events.openUpdate(directStart);

    const first = refuse(send(opened, edit()), edit(), written);
    expect(first.screen.workflow).toMatchObject({
        phase: { status: "idle" },
        stale: { current: written },
    });

    const saved = refuse(lostOnce(opened), edit(), written);
    expect(ending(saved)).toEqual({ status: "saved" });
    expect(events.sent(saved).screen).toEqual({
        workflow: null,
        receipt: null,
    });

    const throttledRepeat = refuse(lostOnce(opened), edit(), null, throttled());
    expect(
        ending(refuse(send(throttledRepeat, edit()), edit(), written)),
    ).toEqual({ status: "saved" });
    for (const status of [200, 502, 503]) {
        const lost = new UnconfirmedError("Not confirmed.", { status });
        const repeated = send(
            refuse(send(opened, edit()), edit(), null, lost),
            edit(),
        );
        expect(ending(refuse(repeated, edit(), written))).toEqual({
            status: "saved",
        });
    }

    const cancelled = refuse(lostOnce(opened), edit(), {
        ...written,
        status: "cancelled",
    });
    expect(cancelled.screen.workflow).toMatchObject({
        phase: { status: "held", hold: "cancelled" },
        stale: { current: { status: "cancelled" } },
    });

    const ended = edit({ ends_at: "2026-11-09T10:30:00.000Z" });
    expect(
        ending(
            refuse(lostOnce(opened, ended), ended, {
                ...written,
                ends_at: "2026-11-09T13:30:00+03:00",
                status: "resolved",
            }),
        ),
    ).toEqual({ status: "saved" });

    expect(
        ending(refuse(lostOnce(opened), edit(), { ...written, revision: 4 })),
    ).toEqual({ status: "idle" });

    const refusedFirst = refuse(send(opened, edit()), edit(), null);
    expect(ending(refuse(send(refusedFirst, edit()), edit(), written))).toEqual(
        {
            status: "idle",
        },
    );

    const answeredAfterLoss = refuse(lostOnce(opened), edit(), null);
    expect(
        ending(refuse(send(answeredAfterLoss, edit()), edit(), written)),
    ).toEqual({ status: "idle" });

    const changed = edit({ title: "Grid tripped" });
    const afterLoss = refuse(send(opened, edit()), edit(), null, lostAnswer());
    expect(
        ending(
            refuse(send(afterLoss, changed), changed, {
                ...written,
                title: "Grid tripped",
            }),
        ),
    ).toEqual({ status: "idle" });

    const others: Partial<OutageRow>[] = [
        { kind: "other" },
        { title: "Grid trips" },
        { notes: "" },
        { starts_at: "2026-11-09T08:00:01Z" },
        { ends_at: "2026-11-09T10:30:00Z" },
        { available_dc_mw: 2 },
        { available_inverter_mw: 0 },
        { available_export_mw: null },
        { id: 5 },
    ];
    for (const other of others)
        expect(
            ending(refuse(lostOnce(opened), edit(), { ...written, ...other })),
            JSON.stringify(other),
        ).toEqual({ status: "idle" });

    const member = events.openUpdate(start);
    expect(ending(refuse(lostOnce(member), edit(), written))).toEqual({
        status: "idle",
    });

    const cancellation: OutageChange = {
        action: "cancel",
        outage: outage(4),
        reason: "",
    };
    expect(
        ending(
            refuse(
                lostOnce(events.openCancel(directStart), cancellation),
                cancellation,
                written,
            ),
        ),
    ).toEqual({ status: "idle" });
});

test("a conflict moves the workflow to the current version and keeps the version it was opened on", () => {
    const first = states["update4/idle+stale"].screen.workflow;
    expect(first).toMatchObject({
        outage: { revision: 3, title: "Changed" },
        stale: {
            before: { revision: 2, title: "Outage 4" },
            current: { revision: 3, title: "Changed" },
        },
        phase: { status: "idle" },
    });
    const second = failed(events.send(states["update4/idle+stale"]), {
        ...outage(4),
        revision: 4,
        title: "Changed again",
    }).screen.workflow;
    expect(second?.stale).toMatchObject({
        before: { revision: 2 },
        current: { revision: 4 },
    });
    const elsewhere = failed(
        states["update4/sending"],
        outage(5, { revision: 3 }),
    ).screen.workflow;
    expect(elsewhere).toMatchObject({
        outage: { id: 4, revision: 2 },
        stale: null,
        phase: { status: "idle" },
    });
});

test("a workflow whose outage closed meanwhile cannot send, and discarding its stale change starts its form again", () => {
    const closedBy = (step: Step, status: OutageRow["status"]) =>
        outageControls(
            failed(events.send(step), outage(4, { revision: 3, status }))
                .screen,
            step.live,
        );
    expect(closedBy(states["cancel4/idle"], "cancelled")).toMatchObject({
        send: false,
        editable: false,
        close: true,
    });
    expect(closedBy(states["resolve4/idle"], "resolved")).toMatchObject({
        send: false,
        editable: false,
    });
    expect(closedBy(states["update4/idle"], "resolved")).toMatchObject({
        send: true,
        editable: true,
    });
    expect(closedBy(states["update4/idle"], "cancelled").send).toBe(false);
    const cancelled = failed(
        events.send(states["cancel4/idle"]),
        outage(4, { revision: 3, status: "cancelled" }),
    );
    expect(events.send(cancelled).screen).toBe(cancelled.screen);
    const stale = states["cancel4/idle+stale"];
    expect(stale.screen.workflow?.form).toBe(0);
    expect(events.discard(stale).screen.workflow?.form).toBe(1);
    expect(events.discard(states["cancel4/idle"]).screen).toBe(
        states["cancel4/idle"].screen,
    );
});

test("restoring the input of an outage edit whose answer was lost sends that edit against its own revision after another edit was refused as stale", () => {
    const opened = events.openUpdate(directStart);
    const workflowOutage = (step: Step) =>
        step.screen.workflow && step.screen.workflow.kind !== "create"
            ? step.screen.workflow.outage
            : outage(4);
    const edit = (step: Step, title: string): OutageChange => {
        const target = workflowOutage(step);
        return {
            action: "update",
            outage: target,
            values: { ...valuesOf(target), title },
            reason: "",
        };
    };
    const send = (step: Step, change: OutageChange, key: string) =>
        run(step, {
            type: "send",
            payload: attemptPayload(change, true, 31),
            key,
            change,
        });
    const sentOf = (step: Step) =>
        step.screen.workflow?.phase.status === "sending"
            ? step.screen.workflow.phase.sent
            : undefined;
    const fail = (step: Step, current: OutageRow | null, error: unknown) =>
        run(step, {
            type: "sendFailed",
            change: sentOf(step)!.change,
            current,
            error,
            direct: true,
        });
    const first = send(opened, edit(opened, "A"), "key-a");
    const lost = fail(first, null, lostAnswer());
    const applied = outage(4, { revision: 3, title: "A" });
    const other = send(lost, edit(lost, "B"), "key-b");
    const stale = fail(other, applied, refused());
    expect(workflowOutage(stale).revision).toBe(3);
    const again = send(stale, edit(stale, "A"), "key-c");
    expect(sentOf(again)).toMatchObject({
        key: "key-a",
        change: { outage: { revision: 2 }, values: { title: "A" } },
    });
    expect(fail(again, applied, refused()).screen.workflow?.phase).toEqual({
        status: "saved",
    });
    const fresh = send(stale, edit(stale, "C"), "key-d");
    expect(sentOf(fresh)).toMatchObject({
        key: "key-d",
        change: { outage: { revision: 3 } },
    });
});

test("an outage request is named by its trimmed reason, the viewer's access and the plant's configuration", () => {
    const create = (reason: string): OutageChange => ({
        action: "create",
        values: valuesOf(outage(4)),
        reason,
    });
    const named = attemptPayload(create("Meter checked"), false, 31);
    expect(attemptPayload(create(" Meter checked  "), false, 31)).toBe(named);
    expect(attemptPayload(create("Meter checked"), false, 32)).not.toBe(named);
    expect(attemptPayload(create("Meter checked"), true, 31)).not.toBe(named);
    expect(attemptPayload(create("Meter"), false, 31)).not.toBe(named);
    const update = (revision: number): OutageChange => ({
        action: "update",
        outage: outage(4, { revision }),
        values: valuesOf(outage(4)),
        reason: "",
    });
    expect(attemptPayload(update(2), false, 31)).toBe(
        attemptPayload(update(3), false, 31),
    );
});

test("an outage request is named by the configuration the server checks it against and by its reason as the server strips it", () => {
    const update = (ends_at: string | null): OutageChange => ({
        action: "update",
        outage: outage(4, { ends_at, declared_configuration_id: 31 }),
        values: { ...valuesOf(outage(4)), ends_at },
        reason: "",
    });
    const ended = "2026-11-09T10:00:00Z";
    expect(attemptPayload(update(ended), true, 32)).toBe(
        attemptPayload(update(ended), true, 31),
    );
    expect(attemptPayload(update(null), true, 32)).not.toBe(
        attemptPayload(update(null), true, 31),
    );
    const resolve: OutageChange = {
        action: "resolve",
        outage: outage(4),
        ends_at: ended,
        reason: "Restarted",
    };
    expect(attemptPayload(resolve, false, 32)).toBe(
        attemptPayload(resolve, false, 31),
    );
    const create = (reason: string): OutageChange => ({
        action: "create",
        values: valuesOf(outage(4)),
        reason,
    });
    const named = attemptPayload(create("Meter checked"), false, 31);
    expect(
        attemptPayload(create("\u0085Meter checked\u001f　"), false, 31),
    ).toBe(named);
    expect(attemptPayload(create("Meter checked﻿"), false, 31)).not.toBe(named);
});
