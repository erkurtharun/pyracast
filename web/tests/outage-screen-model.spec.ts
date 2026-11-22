import { expect, test } from "@playwright/test";
import {
    attemptPayload,
    INITIAL_OUTAGES,
    outageControls,
    reduceOutages,
    type Live,
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

const failed = (step: Step, current: OutageRow | null) =>
    run(step, { type: "sendFailed", current, direct: step.live.direct });

const events = {
    openCreate: open("create"),
    openUpdate: open("update"),
    openResolve: open("resolve"),
    openCancel: open("cancel"),
    openOther: open("update", 5),
    close: (step: Step) => run(step, { type: "close" }),
    discard: (step: Step) => run(step, { type: "discard" }),
    send: (step: Step) =>
        run(step, { type: "send", payload: "first", key: "key-1" }),
    sent: (step: Step) =>
        run(step, { type: "sent", proposed: !step.live.direct }),
    fail: (step: Step) => failed(step, null),
    // A conflict answer carries the outage as it now stands, with the member's own pending request when there is one.
    conflict: (step: Step) =>
        failed(step, outage(4, { revision: 3, title: "Changed" })),
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
        screen.submitted ? "+sent" : "",
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
        sent: "none+sent",
        fail: "create/idle",
        conflict: "create/idle",
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
        sent: "none+sent",
        fail: "update4/idle",
        conflict: "update4/idle+stale",
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
        sent: "none+sent",
        fail: "resolve4/idle",
        conflict: "resolve4/idle+stale",
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
        sent: "none+sent",
        fail: "cancel4/idle",
        conflict: "cancel4/idle+stale",
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
        sent: "direct:none",
        fail: "direct:update4/idle",
        conflict: "direct:update4/idle+stale",
        conflictHeld: "direct:update4/idle+stale",
        conflictCancelled: "direct:update4/held:cancelled+stale",
        demote: "update4/sending=",
    },
    "update4/sending+stale": {
        ...same("update4/sending+stale"),
        ...unchanged("update4/sending+stale"),
        sent: "none+sent",
        fail: "update4/idle+stale",
        conflict: "update4/idle+stale",
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
        "sent",
        "fail",
        "conflict",
        "conflictHeld",
        "conflictCancelled",
    ]);
    for (const [name, state] of Object.entries(states)) {
        const { workflow } = state.screen;
        if (workflow?.phase.status !== "sending") continue;
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
};
const open4 = {
    ...quiet,
    offered: [5],
    send: true,
    editable: true,
    close: true,
};
const sending4 = { ...open4, acts: false, send: false, close: false };
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

test("the idempotency key belongs to the open workflow's attempt", () => {
    const key = (step: Step) => step.screen.workflow?.attempt?.key;
    const sending = states["update4/sending"];
    expect(key(sending)).toBe("key-1");
    const again = run(events.fail(sending), {
        type: "send",
        payload: "first",
        key: "key-2",
    });
    expect(key(again)).toBe("key-1");
    const changed = run(events.fail(again), {
        type: "send",
        payload: "second",
        key: "key-3",
    });
    expect(key(changed)).toBe("key-3");
    for (const event of [
        events.openCreate,
        events.openOther,
        events.close,
        events.send,
    ])
        expect(key(event(changed))).toBe("key-3");
    const reopened = events.openUpdate(events.close(events.fail(changed)));
    expect(reopened.screen.workflow?.attempt).toBeNull();
    expect(key(events.send(reopened))).toBe("key-1");
});

test("a direct creation keeps its key while the values sent stay the same, also after an unconfirmed send, and an edit gets a new key", () => {
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
            payload: attemptPayload(change, true),
            key,
        });
    const key = (step: Step) => step.screen.workflow?.attempt?.key;
    const first = send(
        events.openCreate(directStart),
        creation("Inverter offline"),
        "key-1",
    );
    const again = send(
        events.fail(first),
        creation("Inverter offline"),
        "key-2",
    );
    expect(key(again)).toBe("key-1");
    const edited = send(
        events.fail(again),
        creation("Inverter tripped"),
        "key-3",
    );
    expect(key(edited)).toBe("key-3");
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
