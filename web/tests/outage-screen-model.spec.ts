import { expect, test } from "@playwright/test";
import {
    INITIAL_OUTAGES,
    outageControls,
    reduceOutages,
    type Live,
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
    rowsHeld: (step: Step) => loaded(step, listed(4)),
    rowsLimit: (step: Step) =>
        loaded(step, listed(null, "pending_request_limit")),
    rowsFree: (step: Step) => loaded(step, listed()),
} satisfies Record<string, (step: Step) => Step>;

type EventName = keyof typeof events;

function phase({ screen, live }: Step) {
    const { workflow } = screen;
    const shown = [
        ...(live.offer?.results
            .filter((row) => row.pending_request_id != null)
            .map((row) => `${row.id}held`) ?? []),
        ...(live.offer?.proposal_unavailable_reason === "pending_request_limit"
            ? ["limit"]
            : []),
    ];
    const status = !workflow
        ? "none"
        : [
              workflow.kind,
              workflow.kind === "create" ? "" : workflow.outage.id,
              "/",
              workflow.phase.status,
              workflow.phase.status === "held"
                  ? workflow.phase.hold === "held"
                      ? ":own"
                      : ":limit"
                  : "",
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
} satisfies Record<string, Step>;

type StateName = keyof typeof states;

const same = (name: StateName) =>
    Object.fromEntries(
        Object.keys(events).map((event) => [event, name]),
    ) as Record<EventName, string>;

// Every state against every event: the class of the next state.
const expected: Record<StateName, Record<EventName, string>> = {
    none: {
        ...same("none"),
        openCreate: "create/idle",
        openUpdate: "update4/idle",
        openResolve: "resolve4/idle",
        openCancel: "cancel4/idle",
        openOther: "update5/idle",
        rowsHeld: "none|4held",
        rowsLimit: "none|limit",
    },
    "none+sent": {
        ...same("none+sent"),
        openCreate: "create/idle",
        openUpdate: "update4/idle",
        openResolve: "resolve4/idle",
        openCancel: "cancel4/idle",
        openOther: "update5/idle",
        rowsHeld: "none+sent|4held",
        rowsLimit: "none+sent|limit",
    },
    "none|4held": {
        ...same("none|4held"),
        openCreate: "create/idle|4held",
        openOther: "update5/idle|4held",
        rowsLimit: "none|limit",
        rowsFree: "none",
    },
    "none|limit": {
        ...same("none|limit"),
        rowsHeld: "none|4held",
        rowsFree: "none",
    },
    "create/idle": {
        ...same("create/idle"),
        openUpdate: "update4/idle",
        openResolve: "resolve4/idle",
        openCancel: "cancel4/idle",
        openOther: "update5/idle",
        close: "none",
        send: "create/sending",
        rowsHeld: "create/idle|4held",
        rowsLimit: "create/held:limit|limit",
    },
    "create/sending": {
        ...same("create/sending"),
        sent: "none+sent",
        fail: "create/idle",
        conflict: "create/idle",
        conflictHeld: "create/idle",
        rowsHeld: "create/sending|4held",
        rowsLimit: "create/sending|limit",
    },
    "create/held:limit|limit": {
        ...same("create/held:limit|limit"),
        close: "none|limit",
        rowsHeld: "create/held:limit|4held",
        rowsFree: "create/held:limit",
    },
    "update4/idle": {
        ...same("update4/idle"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        send: "update4/sending",
        rowsHeld: "update4/held:own|4held",
        rowsLimit: "update4/held:limit|limit",
    },
    "update4/idle+stale": {
        ...same("update4/idle+stale"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        discard: "update4/idle",
        send: "update4/sending+stale",
        rowsHeld: "update4/held:own+stale|4held",
        rowsLimit: "update4/held:limit+stale|limit",
    },
    "update4/sending": {
        ...same("update4/sending"),
        sent: "none+sent",
        fail: "update4/idle",
        conflict: "update4/idle+stale",
        conflictHeld: "update4/held:own+stale",
        rowsHeld: "update4/sending|4held",
        rowsLimit: "update4/sending|limit",
    },
    "update4/held:own|4held": {
        ...same("update4/held:own|4held"),
        openCreate: "create/idle|4held",
        openOther: "update5/idle|4held",
        close: "none|4held",
        rowsLimit: "update4/held:own|limit",
        rowsFree: "update4/held:own",
    },
    "update4/held:own+stale": {
        ...same("update4/held:own+stale"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        discard: "update4/held:own",
        rowsHeld: "update4/held:own+stale|4held",
        rowsLimit: "update4/held:own+stale|limit",
    },
    "update4/held:limit|limit": {
        ...same("update4/held:limit|limit"),
        close: "none|limit",
        rowsHeld: "update4/held:limit|4held",
        rowsFree: "update4/held:limit",
    },
    "resolve4/idle": {
        ...same("resolve4/idle"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        send: "resolve4/sending",
        rowsHeld: "resolve4/held:own|4held",
        rowsLimit: "resolve4/held:limit|limit",
    },
    "resolve4/sending": {
        ...same("resolve4/sending"),
        sent: "none+sent",
        fail: "resolve4/idle",
        conflict: "resolve4/idle+stale",
        conflictHeld: "resolve4/held:own+stale",
        rowsHeld: "resolve4/sending|4held",
        rowsLimit: "resolve4/sending|limit",
    },
    "resolve4/held:own|4held": {
        ...same("resolve4/held:own|4held"),
        openCreate: "create/idle|4held",
        openOther: "update5/idle|4held",
        close: "none|4held",
        rowsLimit: "resolve4/held:own|limit",
        rowsFree: "resolve4/held:own",
    },
    "cancel4/idle": {
        ...same("cancel4/idle"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        send: "cancel4/sending",
        rowsHeld: "cancel4/held:own|4held",
        rowsLimit: "cancel4/held:limit|limit",
    },
    "cancel4/idle+stale": {
        ...same("cancel4/idle+stale"),
        openCreate: "create/idle",
        openOther: "update5/idle",
        close: "none",
        discard: "cancel4/idle",
        send: "cancel4/sending+stale",
        rowsHeld: "cancel4/held:own+stale|4held",
        rowsLimit: "cancel4/held:limit+stale|limit",
    },
    "cancel4/sending": {
        ...same("cancel4/sending"),
        sent: "none+sent",
        fail: "cancel4/idle",
        conflict: "cancel4/idle+stale",
        conflictHeld: "cancel4/held:own+stale",
        rowsHeld: "cancel4/sending|4held",
        rowsLimit: "cancel4/sending|limit",
    },
    "cancel4/held:own|4held": {
        ...same("cancel4/held:own|4held"),
        openCreate: "create/idle|4held",
        openOther: "update5/idle|4held",
        close: "none|4held",
        rowsLimit: "cancel4/held:own|limit",
        rowsFree: "cancel4/held:own",
    },
    "update5/idle": {
        ...same("update5/idle"),
        openCreate: "create/idle",
        openUpdate: "update4/idle",
        openResolve: "resolve4/idle",
        openCancel: "cancel4/idle",
        close: "none",
        send: "update5/sending",
        rowsHeld: "update5/idle|4held",
        rowsLimit: "update5/held:limit|limit",
    },
    "direct:none": {
        ...same("direct:none"),
        openCreate: "direct:create/idle",
        openUpdate: "direct:update4/idle",
        openResolve: "direct:resolve4/idle",
        openCancel: "direct:cancel4/idle",
        openOther: "direct:update5/idle",
        rowsHeld: "direct:none|4held",
    },
    "direct:none|4held": {
        ...same("direct:none|4held"),
        openCreate: "direct:create/idle|4held",
        openUpdate: "direct:update4/idle|4held",
        openResolve: "direct:resolve4/idle|4held",
        openCancel: "direct:cancel4/idle|4held",
        openOther: "direct:update5/idle|4held",
        rowsLimit: "direct:none",
        rowsFree: "direct:none",
    },
    "direct:update4/idle": {
        ...same("direct:update4/idle"),
        openCreate: "direct:create/idle",
        openOther: "direct:update5/idle",
        close: "direct:none",
        send: "direct:update4/sending",
        rowsHeld: "direct:update4/idle|4held",
    },
    "direct:update4/sending": {
        ...same("direct:update4/sending"),
        sent: "direct:none",
        fail: "direct:update4/idle",
        conflict: "direct:update4/idle+stale",
        conflictHeld: "direct:update4/idle+stale",
        rowsHeld: "direct:update4/sending|4held",
    },
};

test("every state of the outage screen meets every event as the table says", () => {
    for (const [name, state] of Object.entries(states))
        expect(phase(state), `fixture ${name}`).toBe(name);
    const actual = Object.fromEntries(
        Object.entries(states).map(([name, state]) => [
            name,
            Object.fromEntries(
                Object.entries(events).map(([event, run]) => [
                    event,
                    phase(run(state)),
                ]),
            ),
        ]),
    );
    expect(actual).toEqual(expected);
});

test("no event other than its answer replaces, closes or changes a workflow that is sending", () => {
    const answers = new Set(["sent", "fail", "conflict", "conflictHeld"]);
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
const held4 = { ...open4, send: false, editable: false, hold: "held" };
const atLimit = {
    ...quiet,
    create: false,
    offered: [],
    editable: false,
    close: true,
    hold: "pending_request_limit",
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
    expect(outageControls(INITIAL_OUTAGES, member(undefined)).create).toBe(
        false,
    );
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
    const stale = states["cancel4/idle+stale"];
    expect(stale.screen.workflow?.form).toBe(0);
    expect(events.discard(stale).screen.workflow?.form).toBe(1);
    expect(events.discard(states["cancel4/idle"]).screen).toBe(
        states["cancel4/idle"].screen,
    );
});
