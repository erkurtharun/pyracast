import { expect, test } from "@playwright/test";
import {
    intervalKey,
    type Editor,
    type History,
    type Interval,
} from "../src/features/production/history/model";
import {
    controls,
    initialScreen,
    locked,
    reconcileEditor,
    reduceScreen,
    type Screen,
    type ScreenEvent,
} from "../src/features/production/history/screen";

const DAY = "2026-11-09";
const OTHER = "2026-11-08";

function row(day: string, hour: number, fields: Partial<Interval> = {}) {
    const clock = (value: number) => String(value).padStart(2, "0");
    return {
        starts_at: `${day}T${clock(hour)}:00:00Z`,
        ends_at: `${day}T${clock(hour + 1)}:00:00Z`,
        observation: null,
        previous_configuration_observations: [],
        revision_id: null,
        effective: {
            energy_mwh: 1,
            quality: "valid",
            source: "meter",
            source_kind: "manual",
            provisional: false,
            aggregated: false,
        },
        provider_originals: [],
        can_edit: false,
        can_revert: false,
        edit_unavailable_reason: "workspace_readonly",
        can_propose: true,
        can_propose_revert: false,
        proposal_unavailable_reason: null,
        pending_request_id: null,
        ...fields,
    } satisfies Interval;
}

const dayRows = (day: string, fields: Partial<Interval> = {}) =>
    [8, 9, 10].map((hour) => row(day, hour, fields));
const adminRows = (day: string) =>
    dayRows(day, {
        can_edit: true,
        can_propose: false,
        edit_unavailable_reason: null,
    });
const history = (intervals: Interval[]) => ({ intervals }) as History;
const EIGHT = intervalKey(row(DAY, 8));

type Step = { screen: Screen; rows: Interval[] | undefined };

function apply(step: Step, ...events: ScreenEvent[]): Step {
    return {
        rows: step.rows,
        screen: events.reduce(reduceScreen, step.screen),
    };
}

const shownRow = (step: Step, index: number) =>
    step.rows?.[index] ?? row(step.screen.day, 8 + index);
const editorRow = (step: Step) =>
    step.screen.editor.status === "closed"
        ? shownRow(step, 1)
        : (step.rows?.find(
              (row) =>
                  step.screen.editor.status !== "closed" &&
                  intervalKey(row) === step.screen.editor.key,
          ) ?? shownRow(step, 1));
const other = (step: Step) => (step.screen.day === DAY ? OTHER : DAY);

// The shown day's rows reload: its first two intervals change, the screen sees them as rows of that day.
function reloaded(step: Step, fields: Partial<Interval>): Step {
    const rows = step.rows?.map((row, index) =>
        index < 2 ? { ...row, ...fields } : row,
    );
    return rows
        ? {
              rows,
              screen: reduceScreen(step.screen, {
                  type: "rowsLoaded",
                  day: step.screen.day,
                  history: history(rows),
              }),
          }
        : step;
}

function shown(step: Step, day: string, rows: Interval[] | undefined): Step {
    return {
        rows,
        screen: reduceScreen(step.screen, { type: "dayShown", day }),
    };
}

const events = {
    stage: (step: Step) =>
        apply(step, { type: "stage", row: editorRow(step), action: "set" }),
    editStaged: (step: Step) =>
        apply(step, {
            type: "open",
            row:
                step.rows?.find((row) => intervalKey(row) === EIGHT) ??
                row(DAY, 8),
            reveal: true,
        }),
    openRow: (step: Step) =>
        apply(step, { type: "open", row: shownRow(step, 2) }),
    cancel: (step: Step) => apply(step, { type: "cancel" }),
    remove: (step: Step) => apply(step, { type: "remove", key: EIGHT }),
    discard: (step: Step) => apply(step, { type: "discard" }),
    reason: (step: Step) =>
        apply(step, { type: "reason", text: "Meter read on site" }),
    send: (step: Step) => apply(step, { type: "send", rows: step.rows }),
    sent: (step: Step) => apply(step, { type: "sent" }),
    failItem: (step: Step) =>
        apply(step, {
            type: "sendFailed",
            error: "The request is invalid.",
            refusals: [{ key: EIGHT, message: "Too large." }],
            fields: { general: [] },
        }),
    failRequest: (step: Step) =>
        apply(step, {
            type: "sendFailed",
            error: "Too many pending requests.",
            refusals: [],
            fields: { general: [] },
        }),
    // The day control acts only while it is enabled.
    pickDay: (step: Step) =>
        controls(step.screen, step.rows).dayNav
            ? shown(step, other(step), dayRows(other(step)))
            : step,
    back: (step: Step) => shown(step, other(step), dayRows(other(step))),
    backFails: (step: Step) => shown(step, other(step), undefined),
    rowsHeld: (step: Step) => reloaded(step, { pending_request_id: 7 }),
    rowsChanged: (step: Step) =>
        reloaded(step, { can_propose: false, can_edit: false }),
    // A failed refresh keeps the rows already read.
    readFailed: (step: Step) => step,
    promote: (step: Step) =>
        apply(step, { type: "accessChanged", access: "direct" }),
    fullDay: (step: Step) => apply(step, { type: "fullDay", on: true }),
    refresh: (step: Step) => reloaded(step, {}),
} satisfies Record<string, (step: Step) => Step>;

type EventName = keyof typeof events;

function phase({ screen, rows }: Step) {
    const derived = controls(screen, rows);
    const { draft, editor } = screen;
    const sendState = {
        idle: "idle",
        reason_missing: "missing",
        sending: "sending",
        failed: "failed",
    } as const;
    const editing =
        editor.status === "editing" &&
        draft?.items.some((item) => item.key === editor.key)
            ? "staged"
            : editor.status;
    return [
        screen.access === "propose" ? "" : `${screen.access}:`,
        draft ? sendState[draft.send.status] : "none",
        `/${editing}`,
        derived.away ? "+away" : "",
        derived.held.size ? "+held" : "",
    ].join("");
}

const start: Step = {
    screen: initialScreen(DAY, "propose"),
    rows: dayRows(DAY),
};
const staged = apply(
    start,
    { type: "open", row: dayRows(DAY)[0]! },
    { type: "change", energy: "0,5" },
    { type: "stage", row: dayRows(DAY)[0]!, action: "set" },
);
const sendingStep = apply(
    staged,
    { type: "reason", text: "Meter read on site" },
    { type: "send", rows: dayRows(DAY) },
);
const editingNine = apply(start, { type: "open", row: dayRows(DAY)[1]! });
const admin: Step = {
    screen: initialScreen(DAY, "direct"),
    rows: adminRows(DAY),
};

const states = {
    "none/closed": start,
    "none/editing": editingNine,
    "none/failed": apply(
        editingNine,
        { type: "change", energy: "x" },
        { type: "stage", row: dayRows(DAY)[1]!, action: "set" },
    ),
    "idle/closed": staged,
    "idle/staged": events.editStaged(staged),
    "idle/editing": apply(staged, { type: "open", row: dayRows(DAY)[1]! }),
    "missing/closed": events.send(staged),
    "sending/closed": sendingStep,
    "failed/closed": events.failItem(sendingStep),
    "idle/closed+away": events.back(staged),
    "idle/closed+held": events.rowsHeld(staged),
    "none/orphaned": events.rowsHeld(editingNine),
    "direct:none/saving": apply(
        admin,
        { type: "open", row: adminRows(DAY)[1]! },
        { type: "save", row: adminRows(DAY)[1]!, action: "set" },
    ),
} satisfies Record<string, Step>;

type StateName = keyof typeof states;

const same = (name: StateName) =>
    Object.fromEntries(
        Object.keys(events).map((event) => [event, name]),
    ) as Record<EventName, string>;

// Every state against every event: the class of the next state.
const expected: Record<StateName, Record<EventName, string>> = {
    "none/closed": {
        ...same("none/closed"),
        editStaged: "none/editing",
        openRow: "none/editing",
        promote: "direct:none/closed",
    },
    "none/editing": {
        ...same("none/editing"),
        stage: "idle/closed",
        cancel: "none/closed",
        pickDay: "none/closed",
        back: "none/closed",
        backFails: "none/closed",
        rowsHeld: "none/orphaned",
        rowsChanged: "none/orphaned",
        promote: "direct:none/closed",
    },
    "none/failed": {
        ...same("none/failed"),
        cancel: "none/closed",
        pickDay: "none/closed",
        back: "none/closed",
        backFails: "none/closed",
        rowsHeld: "none/orphaned",
        rowsChanged: "none/orphaned",
        promote: "direct:none/closed",
    },
    "idle/closed": {
        ...same("idle/closed"),
        editStaged: "idle/staged",
        openRow: "idle/editing",
        remove: "none/closed",
        discard: "none/closed",
        send: "missing/closed",
        back: "idle/closed+away",
        backFails: "idle/closed+away",
        rowsHeld: "idle/closed+held",
        promote: "direct:none/closed",
    },
    "idle/staged": {
        ...same("idle/staged"),
        stage: "idle/closed",
        cancel: "idle/closed",
        remove: "none/editing",
        discard: "none/closed",
        back: "idle/closed+away",
        backFails: "idle/closed+away",
        rowsHeld: "idle/orphaned+held",
        rowsChanged: "idle/orphaned",
        promote: "direct:none/closed",
    },
    "idle/editing": {
        ...same("idle/editing"),
        stage: "idle/closed",
        cancel: "idle/closed",
        remove: "none/editing",
        discard: "none/closed",
        back: "idle/closed+away",
        backFails: "idle/closed+away",
        rowsHeld: "idle/orphaned+held",
        rowsChanged: "idle/orphaned",
        promote: "direct:none/closed",
    },
    "missing/closed": {
        ...same("missing/closed"),
        editStaged: "missing/staged",
        openRow: "missing/editing",
        remove: "none/closed",
        discard: "none/closed",
        reason: "idle/closed",
        back: "missing/closed+away",
        backFails: "missing/closed+away",
        rowsHeld: "missing/closed+held",
        promote: "direct:none/closed",
    },
    "sending/closed": {
        ...same("sending/closed"),
        sent: "none/closed",
        failItem: "failed/closed",
        failRequest: "failed/closed",
        back: "sending/closed+away",
        backFails: "sending/closed+away",
        rowsHeld: "sending/closed+held",
        promote: "direct:none/closed",
    },
    "failed/closed": {
        ...same("failed/closed"),
        editStaged: "failed/staged",
        openRow: "failed/editing",
        remove: "none/closed",
        discard: "none/closed",
        send: "sending/closed",
        back: "failed/closed+away",
        backFails: "failed/closed+away",
        rowsHeld: "failed/closed+held",
        promote: "direct:none/closed",
    },
    "idle/closed+away": {
        ...same("idle/closed+away"),
        remove: "none/closed",
        discard: "none/closed",
        back: "idle/closed",
        backFails: "idle/closed",
        promote: "direct:none/closed",
    },
    "idle/closed+held": {
        ...same("idle/closed+held"),
        openRow: "idle/editing+held",
        remove: "none/closed",
        discard: "none/closed",
        back: "idle/closed+away",
        backFails: "idle/closed+away",
        promote: "direct:none/closed",
    },
    "none/orphaned": {
        ...same("none/orphaned"),
        cancel: "none/closed",
        pickDay: "none/closed",
        back: "none/closed",
        backFails: "none/closed",
        promote: "direct:none/closed",
    },
    "direct:none/saving": {
        ...same("direct:none/saving"),
        back: "direct:none/orphaned",
        backFails: "direct:none/orphaned",
        rowsChanged: "direct:none/orphaned",
    },
};

test("every state of the production history screen meets every event as the table says", () => {
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

test("no event reaches an editor open while sending, a sendable draft with a held interval, or an editor without its day's row", () => {
    for (const [name, state] of Object.entries(states))
        for (const [event, run] of Object.entries(events)) {
            const { screen, rows } = run(state);
            const cell = `${name} × ${event}`;
            const derived = controls(screen, rows);
            const { editor } = screen;
            if (screen.draft?.send.status === "sending")
                expect(editor.status, cell).toBe("closed");
            if (derived.held.size) expect(derived.send, cell).toBe(false);
            if (editor.status !== "closed" && editor.status !== "orphaned")
                expect(
                    rows?.some((row) => intervalKey(row) === editor.key),
                    cell,
                ).toBe(true);
            // A day whose read failed can always be read again, unless a write still runs.
            if (!rows && !locked(editor))
                expect(
                    derived.refresh ||
                        (editor.status === "orphaned" && !editor.pendingWrite),
                    cell,
                ).toBe(true);
        }
});

function enabled({ screen, rows }: Step) {
    const derived = controls(screen, rows);
    return {
        dayNav: derived.dayNav,
        fullDay: derived.fullDay,
        refresh: derived.refresh,
        rowActions: derived.rowActions,
        editable: [...derived.editable],
        held: [...derived.held],
        remove: derived.remove,
        discard: derived.discard,
        reason: derived.reason,
        send: derived.send,
    };
}

const quiet = {
    dayNav: true,
    fullDay: true,
    refresh: true,
    rowActions: true,
    editable: [] as string[],
    held: [] as string[],
    remove: false,
    discard: false,
    reason: false,
    send: false,
};
const editing = { ...quiet, fullDay: false, refresh: false, rowActions: false };
const drafted = {
    ...quiet,
    dayNav: false,
    editable: [EIGHT],
    remove: true,
    discard: true,
    reason: true,
    send: true,
};
const draftEditing = {
    ...drafted,
    fullDay: false,
    refresh: false,
    rowActions: false,
    editable: [],
    send: false,
};

test("every state enables only the controls whose event it accepts", () => {
    const table: Record<StateName, typeof quiet> = {
        "none/closed": quiet,
        "none/editing": editing,
        "none/failed": editing,
        "idle/closed": drafted,
        "idle/staged": draftEditing,
        "idle/editing": draftEditing,
        "missing/closed": drafted,
        "sending/closed": {
            ...drafted,
            rowActions: false,
            editable: [],
            remove: false,
            discard: false,
            reason: false,
            send: false,
        },
        "failed/closed": drafted,
        "idle/closed+away": {
            ...drafted,
            rowActions: false,
            editable: [],
            send: false,
        },
        "idle/closed+held": {
            ...drafted,
            editable: [],
            held: [EIGHT],
            send: false,
        },
        "none/orphaned": { ...editing, refresh: true },
        "direct:none/saving": { ...editing, dayNav: false },
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

test("a refusal names its item by key and leaves with that item's next edit or removal", () => {
    const two = apply(
        staged,
        { type: "open", row: dayRows(DAY)[1]! },
        { type: "stage", row: dayRows(DAY)[1]!, action: "set" },
        { type: "reason", text: "Meter read on site" },
        { type: "send", rows: dayRows(DAY) },
        {
            type: "sendFailed",
            error: "The request is invalid.",
            refusals: [
                { key: EIGHT, message: "Too large." },
                { key: intervalKey(dayRows(DAY)[1]!), message: "Unknown." },
            ],
            fields: { general: [] },
        },
    );
    const refusals = (step: Step) =>
        step.screen.draft?.send.status === "failed"
            ? step.screen.draft.send.refusals.map((refusal) => refusal.message)
            : [];
    expect(refusals(two)).toEqual(["Too large.", "Unknown."]);
    expect(refusals(events.remove(two))).toEqual(["Unknown."]);
    expect(
        refusals(
            apply(
                two,
                { type: "open", row: dayRows(DAY)[0]! },
                { type: "stage", row: dayRows(DAY)[0]!, action: "set" },
            ),
        ),
    ).toEqual(["Unknown."]);
});

test("a general refusal that repeats the error is kept once", () => {
    const failed = apply(sendingStep, {
        type: "sendFailed",
        error: "Submit 1 to 100 production intervals.",
        refusals: [],
        fields: {
            reason: "This field may not be blank.",
            general: ["Submit 1 to 100 production intervals.", "Use MWh."],
        },
    });
    expect(failed.screen.draft?.send).toEqual({
        status: "failed",
        error: "Submit 1 to 100 production intervals.",
        refusals: [],
        fields: {
            reason: "This field may not be blank.",
            general: ["Use MWh."],
        },
    });
});

test("an orphaned editor names why: its interval holds the member's request, went away, or its day is no longer shown", () => {
    const cause = (step: Step) =>
        step.screen.editor.status === "orphaned"
            ? step.screen.editor.cause
            : step.screen.editor.status;
    expect(cause(events.rowsHeld(editingNine))).toBe("held");
    expect(cause(events.rowsChanged(editingNine))).toBe("unavailable");
    expect(cause(events.back(states["direct:none/saving"]))).toBe("day_left");
    expect(cause(events.back(editingNine))).toBe("closed");
});

test("an invalid typed energy fails in the editor instead of staging", () => {
    const failed = states["none/failed"].screen.editor;
    expect(failed).toMatchObject({
        status: "failed",
        issue: "invalid_energy",
        energy: "x",
    });
    expect(states["none/failed"].screen.draft).toBeNull();
    expect(
        reduceScreen(states["none/failed"].screen, {
            type: "change",
            energy: "0,4",
        }).editor,
    ).toMatchObject({ status: "editing", energy: "0,4" });
});

test("a staged change keeps the revision its editor opened on", () => {
    expect(staged.screen.draft).toEqual({
        day: DAY,
        reason: "",
        send: { status: "idle" },
        items: [
            {
                key: EIGHT,
                starts_at: `${DAY}T08:00:00Z`,
                ends_at: `${DAY}T09:00:00Z`,
                expectedRevision: null,
                action: "set",
                energy: 0.5,
                quality: "valid",
            },
        ],
    });
    expect(states["idle/staged"].screen.editor).toMatchObject({
        energy: "0.5",
    });
    expect(states["idle/staged"].screen.fullDay).toBe(true);
});

test("a repeated send without a reason is refused again as a new refusal", () => {
    const first = states["missing/closed"].screen.draft?.send;
    const again = events.send(states["missing/closed"]).screen.draft?.send;
    expect(again).toEqual({ status: "reason_missing" });
    expect(again).not.toBe(first);
});

test("an administrator's write ends its editor, also once it was orphaned", () => {
    const saving = states["direct:none/saving"];
    const key = intervalKey(adminRows(DAY)[1]!);
    expect(apply(saving, { type: "saved", key }).screen.editor.status).toBe(
        "closed",
    );
    expect(
        apply(saving, {
            type: "saveFailed",
            key,
            message: "Above capacity.",
            implausible: true,
        }).screen.editor,
    ).toMatchObject({
        status: "failed",
        issue: { message: "Above capacity.", implausible: true },
    });
    const left = events.back(saving);
    expect(controls(left.screen, left.rows).refresh).toBe(false);
    const ended = apply(left, {
        type: "saveFailed",
        key,
        message: "Offline.",
        implausible: false,
    });
    expect(ended.screen.editor).toMatchObject({
        status: "orphaned",
        pendingWrite: false,
    });
    expect(controls(ended.screen, ended.rows).dayNav).toBe(true);
    expect(apply(left, { type: "saved", key }).screen.editor.status).toBe(
        "closed",
    );
});

test("an editor's reload follows the current revision or fails with its message", () => {
    const failed = apply(
        states["direct:none/saving"],
        {
            type: "saveFailed",
            key: intervalKey(adminRows(DAY)[1]!),
            message: "Changed.",
            implausible: false,
        },
        { type: "reload" },
    );
    expect(failed.screen.editor.status).toBe("reloading");
    expect(locked(failed.screen.editor)).toBe(true);
    const rows = adminRows(DAY).map((row) => ({ ...row, revision_id: 9 }));
    expect(
        reduceScreen(failed.screen, {
            type: "rowsLoaded",
            day: DAY,
            history: history(rows),
        }).editor,
    ).toMatchObject({ status: "editing", expectedRevision: 9 });
    expect(
        reduceScreen(failed.screen, {
            type: "reloadFailed",
            key: intervalKey(rows[1]!),
            message: "Offline.",
        }).editor,
    ).toMatchObject({
        status: "failed",
        issue: { message: "Offline.", implausible: false },
    });
    expect(
        reduceScreen(failed.screen, {
            type: "rowsLoaded",
            day: OTHER,
            history: history(rows),
        }),
    ).toBe(failed.screen);
});

test("rows of a day reconcile the editor by the viewer's access", () => {
    const key = intervalKey(row(DAY, 8));
    const editor: Editor = {
        status: "editing",
        key,
        energy: "2",
        quality: "valid",
        expectedRevision: null,
    };
    const closed: Editor = { status: "closed" };
    const rows = [row(DAY, 8)];
    expect(reconcileEditor(closed, history(rows), "propose")).toBe(closed);
    expect(reconcileEditor(editor, history(rows), "propose")).toBe(editor);
    expect(reconcileEditor(editor, history(rows), "direct")).toMatchObject({
        status: "orphaned",
        cause: "unavailable",
        pendingWrite: false,
    });
    expect(reconcileEditor(editor, history([]), "propose")).toMatchObject({
        status: "orphaned",
        cause: "unavailable",
    });
    expect(
        reconcileEditor(
            editor,
            history([row(DAY, 8, { pending_request_id: 7 })]),
            "propose",
        ),
    ).toMatchObject({ status: "orphaned", cause: "held" });
    expect(
        reconcileEditor({ ...editor, status: "saving" }, history([]), "direct"),
    ).toMatchObject({ status: "orphaned", pendingWrite: true });
});

test("a promotion drops the member's draft and an access change closes an idle editor", () => {
    const promoted = events.promote(states["idle/staged"]);
    expect(promoted.screen.draft).toBeNull();
    expect(promoted.screen.editor.status).toBe("closed");
    expect(controls(promoted.screen, promoted.rows).dayNav).toBe(true);
    const still = apply(states["idle/closed"], {
        type: "accessChanged",
        access: "propose",
    });
    expect(still.screen).toBe(states["idle/closed"].screen);
});
