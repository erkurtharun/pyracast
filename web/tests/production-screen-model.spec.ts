import { expect, test } from "@playwright/test";
import { ApiError, UnconfirmedError } from "../src/lib/api/errors";
import {
    intervalKey,
    type Draft,
    type Editor,
    type History,
    type Interval,
} from "../src/features/production/history/model";
import {
    controls,
    dropped,
    editorUnsaved,
    initialScreen,
    locked,
    reconcileEditor,
    reduceScreen,
    repeatsAttempt,
    repeatsOrphan,
    unsentInput,
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
const editorKey = (step: Step) =>
    step.screen.editor.status === "closed" ? EIGHT : step.screen.editor.key;
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

// A row control exists only for a row the screen shows.
const onShownRow = (step: Step, event: (rows: Interval[]) => ScreenEvent) =>
    step.rows ? apply(step, event(step.rows)) : step;

const events = {
    stage: (step: Step) =>
        apply(step, { type: "stage", row: editorRow(step), action: "set" }),
    stageRevert: (step: Step) =>
        apply(step, { type: "stage", row: editorRow(step), action: "revert" }),
    editStaged: (step: Step) =>
        onShownRow(step, (rows) => ({
            type: "open",
            row: rows.find((row) => intervalKey(row) === EIGHT) ?? row(DAY, 8),
            reveal: true,
        })),
    openRow: (step: Step) =>
        onShownRow(step, (rows) => ({ type: "open", row: rows[2]! })),
    change: (step: Step) => apply(step, { type: "change", energy: "0,7" }),
    cancel: (step: Step) => apply(step, { type: "cancel" }),
    remove: (step: Step) => apply(step, { type: "remove", key: EIGHT }),
    removeUnstaged: (step: Step) =>
        apply(step, { type: "remove", key: intervalKey(row(DAY, 23)) }),
    discard: (step: Step) => apply(step, { type: "discard" }),
    reason: (step: Step) => apply(step, { type: "reason", text: "verified" }),
    send: (step: Step) => apply(step, { type: "send", rows: step.rows }),
    sent: (step: Step) => apply(step, { type: "sent" }),
    failItem: (step: Step) =>
        apply(step, {
            type: "sendFailed",
            error: new ApiError("The request is invalid."),
            refusals: [{ key: EIGHT, message: "Too large." }],
            fields: { general: [] },
        }),
    failRequest: (step: Step) =>
        apply(step, {
            type: "sendFailed",
            error: new ApiError("Too many pending requests."),
            refusals: [],
            fields: { general: [] },
        }),
    save: (step: Step) =>
        apply(step, { type: "save", row: editorRow(step), action: "set" }),
    saveRevert: (step: Step) =>
        apply(step, { type: "save", row: editorRow(step), action: "revert" }),
    saved: (step: Step) => apply(step, { type: "saved" }),
    saveFailed: (step: Step) =>
        apply(step, {
            type: "saveFailed",
            error: new ApiError("Production changed."),
            implausible: false,
        }),
    reload: (step: Step) => apply(step, { type: "reload" }),
    reloadFailed: (step: Step) =>
        apply(step, {
            type: "reloadFailed",
            key: editorKey(step),
            error: new ApiError("Offline."),
        }),
    // The answer of a reload started for another interval, before its editor closed.
    staleReloadFailed: (step: Step) =>
        apply(step, {
            type: "reloadFailed",
            key: intervalKey(row(DAY, 23)),
            error: new ApiError("Offline."),
        }),
    // The day control acts only while it is enabled.
    pickDay: (step: Step) =>
        controls(step.screen, step.rows).dayNav
            ? shown(step, other(step), dayRows(other(step)))
            : step,
    back: (step: Step) => shown(step, other(step), dayRows(other(step))),
    backFails: (step: Step) => shown(step, other(step), undefined),
    sameDay: (step: Step) => shown(step, step.screen.day, step.rows),
    rowsHeld: (step: Step) => reloaded(step, { pending_request_id: 7 }),
    rowsChanged: (step: Step) =>
        reloaded(step, { can_propose: false, can_edit: false }),
    promote: (step: Step) =>
        apply(step, { type: "accessChanged", access: "direct" }),
    lose: (step: Step) =>
        apply(step, { type: "accessChanged", access: "none" }),
    fullDay: (step: Step) => apply(step, { type: "fullDay", on: true }),
    fullDayOff: (step: Step) => apply(step, { type: "fullDay", on: false }),
    // Refresh reads the shown day again; a day whose read failed is read now.
    refresh: (step: Step) =>
        step.rows
            ? reloaded(step, {})
            : {
                  rows: dayRows(step.screen.day),
                  screen: reduceScreen(step.screen, {
                      type: "rowsLoaded",
                      day: step.screen.day,
                      history: history(dayRows(step.screen.day)),
                  }),
              },
} satisfies Record<string, (step: Step) => Step>;

type EventName = keyof typeof events;

function editorPhase({ editor, draft }: Screen) {
    if (editor.status === "orphaned")
        return [
            "orphaned:",
            editor.cause,
            editor.pendingWrite ? "*" : "",
            editor.error ? "!" : "",
        ].join("");
    if (editor.status !== "editing") return editor.status;
    if (draft?.items.some((item) => item.key === editor.key)) return "staged";
    return editor.reloaded ? "reloaded" : "editing";
}

function phase({ screen, rows }: Step) {
    const derived = controls(screen, rows);
    const { draft } = screen;
    const sendState = {
        idle: "idle",
        reason_missing: "missing",
        sending: "sending",
        failed: "failed",
    } as const;
    return [
        screen.access === "propose" ? "" : `${screen.access}:`,
        draft ? sendState[draft.send.status] : "none",
        `/${editorPhase(screen)}`,
        derived.away ? "+away" : "",
        derived.held.size ? "+held" : "",
        screen.fullDay ? "+full" : "",
        draft?.reason ? `+reason=${draft.reason}` : "",
        rows ? "" : "?unread",
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
    { type: "reason", text: "metered" },
    { type: "send", rows: dayRows(DAY) },
);
const editingNine = apply(start, { type: "open", row: dayRows(DAY)[1]! });
const failedNine = apply(
    editingNine,
    { type: "change", energy: "x" },
    { type: "stage", row: dayRows(DAY)[1]!, action: "set" },
);
const draftEditing = apply(staged, { type: "open", row: dayRows(DAY)[1]! });
const admin: Step = {
    screen: initialScreen(DAY, "direct"),
    rows: adminRows(DAY),
};
const adminEditing = apply(admin, { type: "open", row: adminRows(DAY)[1]! });
const adminSaving = apply(adminEditing, {
    type: "save",
    row: adminRows(DAY)[1]!,
    action: "set",
});

const states = {
    "none/closed": start,
    "none/closed?unread": {
        screen: initialScreen(DAY, "propose"),
        rows: undefined,
    },
    "none:none/closed": {
        screen: initialScreen(DAY, "none"),
        rows: dayRows(DAY),
    },
    "none/editing": editingNine,
    "none/failed": failedNine,
    "none/reloading": events.reload(failedNine),
    "none/orphaned:held": events.rowsHeld(editingNine),
    "idle/closed": staged,
    "idle/staged+full": events.editStaged(staged),
    "idle/editing": draftEditing,
    "idle/reloading": events.reload(draftEditing),
    "idle/orphaned:unavailable": events.rowsChanged(draftEditing),
    "idle/orphaned:held+held": events.rowsHeld(draftEditing),
    "missing/closed": events.send(staged),
    "sending/closed+reason=metered": sendingStep,
    "sending/closed+away+reason=metered": events.back(sendingStep),
    "sending/closed+held+reason=metered": events.rowsHeld(sendingStep),
    "failed/closed+reason=metered": events.failItem(sendingStep),
    "idle/closed+away": events.back(staged),
    "idle/closed+held": events.rowsHeld(staged),
    "idle/editing+held": events.openRow(events.rowsHeld(staged)),
    "direct:none/editing": adminEditing,
    "direct:none/saving": adminSaving,
    "direct:none/orphaned:day_left*": events.back(adminSaving),
    "direct:none/orphaned:day_left!": events.saveFailed(
        events.back(adminSaving),
    ),
} satisfies Record<string, Step>;

type StateName = keyof typeof states;

// A refused event returns the very same screen, marked "=".
const same = (name: StateName) =>
    Object.fromEntries(
        Object.keys(events).map((event) => [event, `${name}=`]),
    ) as Record<EventName, string>;

// Leaving for another day, by Back or a link, with the read of that day answering or failing.
const leaves = (next: string) => ({
    back: next,
    backFails: `${next}?unread`,
});
const quits = {
    promote: "direct:none/closed",
    lose: "none:none/closed",
};

// Every state against every event: the class of the next state.
const expected: Record<StateName, Record<EventName, string>> = {
    "none/closed": {
        ...same("none/closed"),
        editStaged: "none/editing+full",
        openRow: "none/editing",
        ...leaves("none/closed"),
        ...quits,
        fullDay: "none/closed+full",
        pickDay: "none/closed",
    },
    "none/closed?unread": {
        ...same("none/closed?unread"),
        pickDay: "none/closed",
        ...leaves("none/closed"),
        promote: "direct:none/closed?unread",
        lose: "none:none/closed?unread",
        fullDay: "none/closed+full?unread",
        refresh: "none/closed=",
    },
    "none:none/closed": {
        ...same("none:none/closed"),
        ...leaves("none:none/closed"),
        promote: "direct:none/closed",
        fullDay: "none:none/closed+full",
        pickDay: "none:none/closed",
    },
    "none/editing": {
        ...same("none/editing"),
        stage: "idle/closed",
        cancel: "none/closed",
        reload: "none/reloading",
        pickDay: "none/closed",
        ...leaves("none/closed"),
        rowsHeld: "none/orphaned:held",
        rowsChanged: "none/orphaned:unavailable",
        ...quits,
        change: "none/editing",
    },
    "none/failed": {
        ...same("none/failed"),
        change: "none/editing",
        cancel: "none/closed",
        reload: "none/reloading",
        pickDay: "none/closed",
        ...leaves("none/closed"),
        rowsHeld: "none/orphaned:held",
        rowsChanged: "none/orphaned:unavailable",
        ...quits,
        stage: "none/failed",
    },
    "none/reloading": {
        ...same("none/reloading"),
        reloadFailed: "none/failed",
        ...leaves("none/closed"),
        rowsHeld: "none/orphaned:held",
        rowsChanged: "none/orphaned:unavailable",
        ...quits,
        refresh: "none/reloaded",
    },
    "none/orphaned:held": {
        ...same("none/orphaned:held"),
        cancel: "none/closed",
        pickDay: "none/closed",
        ...leaves("none/closed"),
        ...quits,
    },
    "idle/closed": {
        ...same("idle/closed"),
        editStaged: "idle/staged+full",
        openRow: "idle/editing",
        remove: "none/closed",
        discard: "none/closed",
        reason: "idle/closed+reason=verified",
        send: "missing/closed",
        ...leaves("idle/closed+away"),
        rowsHeld: "idle/closed+held=",
        ...quits,
        fullDay: "idle/closed+full",
    },
    "idle/staged+full": {
        ...same("idle/staged+full"),
        stage: "idle/closed+full",
        cancel: "idle/closed+full",
        remove: "none/editing+full",
        discard: "none/closed+full",
        reason: "idle/staged+full+reason=verified",
        reload: "idle/reloading+full",
        ...leaves("idle/closed+away+full"),
        rowsHeld: "idle/orphaned:held+held+full",
        rowsChanged: "idle/orphaned:unavailable+full",
        promote: "direct:none/closed+full",
        lose: "none:none/closed+full",
        change: "idle/staged+full",
    },
    "idle/editing": {
        ...same("idle/editing"),
        stage: "idle/closed",
        cancel: "idle/closed",
        remove: "none/editing",
        discard: "none/closed",
        reason: "idle/editing+reason=verified",
        reload: "idle/reloading",
        ...leaves("idle/closed+away"),
        rowsHeld: "idle/orphaned:held+held",
        rowsChanged: "idle/orphaned:unavailable",
        ...quits,
        change: "idle/editing",
    },
    "idle/reloading": {
        ...same("idle/reloading"),
        remove: "none/reloading",
        reason: "idle/reloading+reason=verified",
        reloadFailed: "idle/failed",
        ...leaves("idle/closed+away"),
        rowsHeld: "idle/orphaned:held+held",
        rowsChanged: "idle/orphaned:unavailable",
        ...quits,
        refresh: "idle/reloaded",
    },
    "idle/orphaned:unavailable": {
        ...same("idle/orphaned:unavailable"),
        cancel: "idle/closed",
        remove: "none/orphaned:unavailable",
        discard: "none/closed",
        reason: "idle/orphaned:unavailable+reason=verified",
        ...leaves("idle/closed+away"),
        rowsHeld: "idle/orphaned:unavailable+held=",
        ...quits,
    },
    "idle/orphaned:held+held": {
        ...same("idle/orphaned:held+held"),
        cancel: "idle/closed+held",
        remove: "none/orphaned:held",
        discard: "none/closed",
        reason: "idle/orphaned:held+held+reason=verified",
        ...leaves("idle/closed+away"),
        ...quits,
    },
    "missing/closed": {
        ...same("missing/closed"),
        editStaged: "missing/staged+full",
        openRow: "missing/editing",
        remove: "none/closed",
        discard: "none/closed",
        reason: "idle/closed+reason=verified",
        ...leaves("missing/closed+away"),
        rowsHeld: "missing/closed+held=",
        ...quits,
        fullDay: "missing/closed+full",
        send: "missing/closed",
    },
    "sending/closed+reason=metered": {
        ...same("sending/closed+reason=metered"),
        sent: "none/closed",
        failItem: "failed/closed+reason=metered",
        failRequest: "failed/closed+reason=metered",
        ...leaves("sending/closed+away+reason=metered"),
        rowsHeld: "sending/closed+held+reason=metered=",
        ...quits,
        fullDay: "sending/closed+full+reason=metered",
    },
    "sending/closed+away+reason=metered": {
        ...same("sending/closed+away+reason=metered"),
        sent: "none/closed",
        failItem: "failed/closed+away+reason=metered",
        failRequest: "failed/closed+away+reason=metered",
        ...leaves("sending/closed+reason=metered"),
        ...quits,
        fullDay: "sending/closed+away+full+reason=metered",
    },
    "sending/closed+held+reason=metered": {
        ...same("sending/closed+held+reason=metered"),
        sent: "none/closed",
        failItem: "failed/closed+held+reason=metered",
        failRequest: "failed/closed+held+reason=metered",
        ...leaves("sending/closed+away+reason=metered"),
        ...quits,
        fullDay: "sending/closed+held+full+reason=metered",
    },
    "failed/closed+reason=metered": {
        ...same("failed/closed+reason=metered"),
        editStaged: "failed/staged+full+reason=metered",
        openRow: "failed/editing+reason=metered",
        remove: "none/closed",
        discard: "none/closed",
        reason: "failed/closed+reason=verified",
        send: "sending/closed+reason=metered",
        ...leaves("failed/closed+away+reason=metered"),
        rowsHeld: "failed/closed+held+reason=metered=",
        ...quits,
        fullDay: "failed/closed+full+reason=metered",
    },
    "idle/closed+away": {
        ...same("idle/closed+away"),
        remove: "none/closed",
        discard: "none/closed",
        reason: "idle/closed+away+reason=verified",
        ...leaves("idle/closed"),
        ...quits,
        fullDay: "idle/closed+away+full",
    },
    "idle/closed+held": {
        ...same("idle/closed+held"),
        openRow: "idle/editing+held",
        remove: "none/closed",
        discard: "none/closed",
        reason: "idle/closed+held+reason=verified",
        ...leaves("idle/closed+away"),
        ...quits,
        fullDay: "idle/closed+held+full",
    },
    "idle/editing+held": {
        ...same("idle/editing+held"),
        stage: "idle/closed+held",
        cancel: "idle/closed+held",
        remove: "none/editing",
        discard: "none/closed",
        reason: "idle/editing+held+reason=verified",
        reload: "idle/reloading+held",
        ...leaves("idle/closed+away"),
        ...quits,
        change: "idle/editing+held",
    },
    "direct:none/editing": {
        ...same("direct:none/editing"),
        change: "direct:none/editing",
        cancel: "direct:none/closed",
        save: "direct:none/saving",
        reload: "direct:none/reloading",
        pickDay: "direct:none/closed",
        ...leaves("direct:none/closed"),
        rowsChanged: "direct:none/orphaned:unavailable",
        lose: "none:none/closed",
    },
    "direct:none/saving": {
        ...same("direct:none/saving"),
        saved: "direct:none/closed",
        saveFailed: "direct:none/failed",
        ...leaves("direct:none/orphaned:day_left*"),
        rowsChanged: "direct:none/orphaned:unavailable*",
        lose: "none:none/orphaned:unavailable*",
    },
    "direct:none/orphaned:day_left*": {
        ...same("direct:none/orphaned:day_left*"),
        saved: "direct:none/closed",
        saveFailed: "direct:none/orphaned:day_left!",
        backFails: "direct:none/orphaned:day_left*?unread",
        lose: "none:none/orphaned:unavailable*",
        back: "direct:none/orphaned:day_left*",
    },
    "direct:none/orphaned:day_left!": {
        ...same("direct:none/orphaned:day_left!"),
        cancel: "direct:none/closed",
        pickDay: "direct:none/closed",
        ...leaves("direct:none/closed"),
        lose: "none:none/closed",
    },
};

test("every state of the production history screen meets every event as the table says", () => {
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
const draftOpen = {
    ...drafted,
    fullDay: false,
    refresh: false,
    rowActions: false,
    editable: [],
    send: false,
};
const sendingDraft = {
    ...drafted,
    rowActions: false,
    editable: [],
    remove: false,
    discard: false,
    reason: false,
    send: false,
};

test("every state enables only the controls whose event it accepts", () => {
    const table: Record<StateName, typeof quiet> = {
        "none/closed": quiet,
        "none/closed?unread": quiet,
        "none:none/closed": quiet,
        "none/editing": editing,
        "none/failed": editing,
        "none/reloading": { ...editing, dayNav: false },
        "none/orphaned:held": { ...editing, refresh: true },
        "idle/closed": drafted,
        "idle/staged+full": draftOpen,
        "idle/editing": draftOpen,
        "idle/reloading": { ...draftOpen, discard: false },
        "idle/orphaned:unavailable": { ...draftOpen, refresh: true },
        "idle/orphaned:held+held": {
            ...draftOpen,
            refresh: true,
            held: [EIGHT],
        },
        "missing/closed": drafted,
        "sending/closed+reason=metered": sendingDraft,
        "sending/closed+away+reason=metered": sendingDraft,
        "sending/closed+held+reason=metered": {
            ...sendingDraft,
            held: [EIGHT],
        },
        "failed/closed+reason=metered": drafted,
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
        "idle/editing+held": { ...draftOpen, held: [EIGHT] },
        "direct:none/editing": editing,
        "direct:none/saving": { ...editing, dayNav: false },
        "direct:none/orphaned:day_left*": { ...editing, dayNav: false },
        "direct:none/orphaned:day_left!": { ...editing, refresh: true },
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

test("an orphaned editor whose write fails keeps the failure's message and no longer waits for the write", () => {
    expect(
        states["direct:none/orphaned:day_left!"].screen.editor,
    ).toMatchObject({
        status: "orphaned",
        cause: "day_left",
        pendingWrite: false,
        error: expect.objectContaining({ message: "Production changed." }),
    });
});

test("a reload says the current values were reloaded only when it returned the editor to editing", () => {
    const back = events.refresh(states["none/reloading"]).screen.editor;
    expect(back).toMatchObject({ status: "editing", reloaded: true });
    expect(
        events.change(events.refresh(states["none/reloading"])).screen.editor,
    ).not.toHaveProperty("reloaded");
    expect(events.back(states["none/reloading"]).screen.editor).toEqual({
        status: "closed",
    });
    expect(
        events.refresh(states["none/editing"]).screen.editor,
    ).not.toHaveProperty("reloaded");
});

test("an access change tells once that it dropped a draft or an open editor", () => {
    const told = (state: Step, access: "direct" | "propose" | "none") =>
        dropped(
            state.screen,
            reduceScreen(state.screen, { type: "accessChanged", access }),
        );
    expect(told(states["idle/closed"], "direct")).toBe(true);
    expect(told(states["none/editing"], "direct")).toBe(true);
    expect(told(states["none/orphaned:held"], "direct")).toBe(true);
    expect(told(states["none/closed"], "direct")).toBe(false);
    expect(told(states["direct:none/saving"], "none")).toBe(false);
    expect(told(states["idle/closed"], "propose")).toBe(false);
    const promoted = events.promote(states["idle/closed"]);
    expect(dropped(promoted.screen, promoted.screen)).toBe(false);
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
            error: new ApiError("The request is invalid."),
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
    const error = new ApiError("Send 1 to 100 values in one change request.");
    const failed = apply(sendingStep, {
        type: "sendFailed",
        error,
        refusals: [],
        fields: {
            reason: "This field may not be blank.",
            general: [
                "Send 1 to 100 values in one change request.",
                "Use MWh.",
            ],
        },
    });
    expect(failed.screen.draft?.send).toEqual({
        status: "failed",
        error,
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
    expect(states["idle/staged+full"].screen.editor).toMatchObject({
        energy: "0.5",
    });
    expect(states["idle/staged+full"].screen.fullDay).toBe(true);
});

test("a repeated send without a reason is refused again as a new refusal", () => {
    const first = states["missing/closed"].screen.draft?.send;
    const again = events.send(states["missing/closed"]).screen.draft?.send;
    expect(again).toEqual({ status: "reason_missing" });
    expect(again).not.toBe(first);
});

test("an administrator's write ends its editor, also once it was orphaned", () => {
    const saving = states["direct:none/saving"];
    expect(apply(saving, { type: "saved" }).screen.editor.status).toBe(
        "closed",
    );
    expect(
        apply(saving, {
            type: "saveFailed",
            error: new ApiError("Above capacity."),
            implausible: true,
        }).screen.editor,
    ).toMatchObject({
        status: "failed",
        issue: {
            error: expect.objectContaining({ message: "Above capacity." }),
            implausible: true,
        },
    });
    const left = events.back(saving);
    expect(controls(left.screen, left.rows).refresh).toBe(false);
    const ended = apply(left, {
        type: "saveFailed",
        error: new ApiError("Offline."),
        implausible: false,
    });
    expect(ended.screen.editor).toMatchObject({
        status: "orphaned",
        pendingWrite: false,
    });
    expect(controls(ended.screen, ended.rows).dayNav).toBe(true);
    expect(apply(left, { type: "saved" }).screen.editor.status).toBe("closed");
});

test("an editor's reload follows the current revision or fails with its message", () => {
    const failed = apply(
        states["direct:none/saving"],
        {
            type: "saveFailed",
            error: new ApiError("Changed."),
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
            error: new ApiError("Offline."),
        }).editor,
    ).toMatchObject({
        status: "failed",
        issue: {
            error: expect.objectContaining({ message: "Offline." }),
            implausible: false,
        },
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
        opened: { energy: "2", quality: "valid" },
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
        reconcileEditor(
            {
                ...editor,
                status: "saving",
                write: {
                    payload: "",
                    action: "set",
                    change: {
                        starts_at: row(DAY, 8).starts_at,
                        ends_at: row(DAY, 8).ends_at,
                        expected_revision_id: null,
                        action: "set",
                        energy: 1,
                        quality: "valid",
                        source: "operator",
                    },
                    confirmImplausible: false,
                },
            },
            history([]),
            "direct",
        ),
    ).toMatchObject({ status: "orphaned", pendingWrite: true });
});

test("a promotion drops the member's draft and an access change closes an idle editor", () => {
    const promoted = events.promote(states["idle/staged+full"]);
    expect(promoted.screen.draft).toBeNull();
    expect(promoted.screen.editor.status).toBe("closed");
    expect(controls(promoted.screen, promoted.rows).dayNav).toBe(true);
    const still = apply(states["idle/closed"], {
        type: "accessChanged",
        access: "propose",
    });
    expect(still.screen).toBe(states["idle/closed"].screen);
});

const lostAnswer = () => new UnconfirmedError("Not confirmed.");

test("a direct write whose answer was lost is sent again as it was while the editor would send the same, and an edit makes a new write", () => {
    const lost = apply(adminSaving, {
        type: "saveFailed",
        error: lostAnswer(),
        implausible: false,
    });
    const editor = lost.screen.editor;
    expect(editor).toMatchObject({
        status: "failed",
        attempt: { action: "set", confirmImplausible: false },
    });
    const attempt = editor.status === "failed" ? editor.attempt : undefined;
    const applied = adminRows(DAY).map((row, index) =>
        index === 1 ? { ...row, revision_id: 7 } : row,
    );
    const reread: Step = {
        rows: applied,
        screen: reduceScreen(lost.screen, {
            type: "rowsLoaded",
            day: DAY,
            history: history(applied),
        }),
    };
    const again = apply(reread, {
        type: "save",
        row: applied[1]!,
        action: "set",
    });
    expect(again.screen.editor).toMatchObject({
        status: "saving",
        expectedRevision: null,
        write: attempt,
    });
    const edited = apply(reread, { type: "change", energy: "0,7" });
    expect(edited.screen.editor).toMatchObject({
        status: "editing",
        energy: "0,7",
        attempt,
    });
    expect(repeatsAttempt(edited.screen.editor as Draft)).toBe(false);
    const changedWrite = apply(edited, {
        type: "save",
        row: applied[1]!,
        action: "set",
    }).screen.editor;
    expect(changedWrite).toMatchObject({
        status: "saving",
        expectedRevision: null,
    });
    expect(changedWrite.status === "saving" && changedWrite.write).not.toEqual(
        attempt,
    );
    const undone = apply(edited, { type: "change", energy: "1" });
    expect(repeatsAttempt(undone.screen.editor as Draft)).toBe(true);
});

test("an answer to a repeat decides the write whose answer was lost unless it is throttled", () => {
    const lost = apply(adminSaving, {
        type: "saveFailed",
        error: lostAnswer(),
        implausible: false,
    });
    const repeat = apply(lost, {
        type: "save",
        row: adminRows(DAY)[1]!,
        action: "set",
    });
    const attemptOf = (step: Step) =>
        step.screen.editor.status === "failed"
            ? step.screen.editor.attempt
            : "none";
    const throttled = apply(repeat, {
        type: "saveFailed",
        error: new ApiError("Too many requests.", { status: 429 }),
        implausible: false,
    });
    expect(attemptOf(throttled)).toEqual(attemptOf(lost));
    const refused = apply(repeat, {
        type: "saveFailed",
        error: new ApiError("Production changed.", { status: 409 }),
        implausible: false,
    });
    expect(attemptOf(refused)).toBeUndefined();
    const other = apply(
        lost,
        { type: "change", energy: "0,7" },
        { type: "save", row: adminRows(DAY)[1]!, action: "set" },
        {
            type: "saveFailed",
            error: new ApiError("Production changed.", { status: 409 }),
            implausible: false,
        },
    );
    expect(attemptOf(other)).toEqual(attemptOf(lost));
});

test("a revert whose answer was lost can be sent again after the rows show it applied", () => {
    const rows = adminRows(DAY).map((row) => ({
        ...row,
        revision_id: 3,
        can_revert: true,
    }));
    const opened = apply(
        { screen: initialScreen(DAY, "direct"), rows },
        { type: "open", row: rows[1]! },
        { type: "save", row: rows[1]!, action: "revert" },
        { type: "saveFailed", error: lostAnswer(), implausible: false },
    );
    const reverted = rows.map((row) => ({
        ...row,
        revision_id: 4,
        can_revert: false,
    }));
    const again = apply(
        {
            rows: reverted,
            screen: reduceScreen(opened.screen, {
                type: "rowsLoaded",
                day: DAY,
                history: history(reverted),
            }),
        },
        { type: "save", row: reverted[1]!, action: "revert" },
    );
    expect(again.screen.editor).toMatchObject({
        status: "saving",
        expectedRevision: 3,
        write: { action: "revert" },
    });
    const fresh = apply(
        { screen: initialScreen(DAY, "direct"), rows: reverted },
        { type: "open", row: reverted[1]! },
        { type: "save", row: reverted[1]!, action: "revert" },
    );
    expect(fresh.screen.editor.status).toBe("editing");
});

const lostSet = (revision: number) => {
    const rows = adminRows(DAY).map((row) => ({
        ...row,
        revision_id: revision,
    }));
    return apply(
        { screen: initialScreen(DAY, "direct"), rows },
        { type: "open", row: rows[1]! },
        { type: "change", energy: "0,5" },
        { type: "save", row: rows[1]!, action: "set" },
        { type: "saveFailed", error: lostAnswer(), implausible: false },
    );
};

test("a reload keeps a write whose answer was lost with its revision and its notice, and an edit is sent against the reloaded revision", () => {
    const lost = lostSet(3);
    const rows = adminRows(DAY).map((row) => ({ ...row, revision_id: 5 }));
    const reloaded = apply(lost, { type: "reload" });
    const read: Step = {
        rows,
        screen: reduceScreen(reloaded.screen, {
            type: "rowsLoaded",
            day: DAY,
            history: history(rows),
        }),
    };
    expect(read.screen.editor).toMatchObject({
        status: "editing",
        reloaded: true,
        expectedRevision: 5,
        attempt: { change: { expected_revision_id: 3, energy: 0.5 } },
    });
    const again = apply(read, { type: "save", row: rows[1]!, action: "set" });
    expect(again.screen.editor).toMatchObject({
        status: "saving",
        write: { change: { expected_revision_id: 3, energy: 0.5 } },
    });
    const edited = apply(
        read,
        { type: "change", energy: "0,6" },
        { type: "save", row: rows[1]!, action: "set" },
    );
    expect(edited.screen.editor).toMatchObject({
        status: "saving",
        write: { change: { expected_revision_id: 5, energy: 0.6 } },
    });
});

test("a write whose answer was lost keeps its outcome and its request once its interval is gone, and is sent again as it was", () => {
    const lost = lostSet(3);
    const hourly = [row(DAY, 8, { can_edit: true, revision_id: null })];
    const gone = reduceScreen(lost.screen, {
        type: "rowsLoaded",
        day: DAY,
        history: history(hourly),
    });
    const attempt =
        lost.screen.editor.status === "failed"
            ? lost.screen.editor.attempt
            : undefined;
    expect(gone.editor).toMatchObject({
        status: "orphaned",
        cause: "unavailable",
        pendingWrite: false,
        error: expect.any(UnconfirmedError),
        attempt,
    });
    const repeating = reduceScreen(gone, { type: "repeat" });
    expect(repeating.editor).toMatchObject({
        status: "orphaned",
        pendingWrite: true,
        write: attempt,
    });
    expect(reduceScreen(repeating, { type: "repeat" })).toBe(repeating);
    const throttled = reduceScreen(repeating, {
        type: "saveFailed",
        error: new ApiError("Too many requests.", { status: 429 }),
        implausible: false,
    });
    expect(throttled.editor).toMatchObject({
        status: "orphaned",
        pendingWrite: false,
        attempt,
    });
    expect(reduceScreen(repeating, { type: "saved" }).editor).toEqual({
        status: "closed",
    });
    const member = { ...gone, access: "propose" as const };
    expect(reduceScreen(member, { type: "repeat" })).toBe(member);
});

test("a failed reload keeps the write whose answer was lost beside the read's failure, and a reload after it returns to editing with that write", () => {
    const lost = lostSet(3);
    const attempt = (lost.screen.editor as Draft).attempt;
    const reloading = apply(lost, { type: "reload" });
    const failed = apply(reloading, {
        type: "reloadFailed",
        key: editorKey(reloading),
        error: new ApiError("Offline."),
    });
    expect(failed.screen.editor).toMatchObject({
        status: "failed",
        attempt,
        issue: { error: expect.objectContaining({ message: "Offline." }) },
    });
    const rows = adminRows(DAY).map((row) => ({ ...row, revision_id: 5 }));
    const read = reduceScreen(apply(failed, { type: "reload" }).screen, {
        type: "rowsLoaded",
        day: DAY,
        history: history(rows),
    });
    expect(read.editor).toMatchObject({
        status: "editing",
        expectedRevision: 5,
        attempt,
    });
    expect(read.editor).not.toHaveProperty("issue");
});

test("rows read after a failed reload drop the read's failure and keep the write whose answer was lost, also once the interval is gone", () => {
    const lost = lostSet(3);
    const attempt = (lost.screen.editor as Draft).attempt;
    const reloading = apply(lost, { type: "reload" });
    const failed = apply(reloading, {
        type: "reloadFailed",
        key: editorKey(reloading),
        error: new ApiError("Not found."),
    });
    const recovered = reduceScreen(failed.screen, {
        type: "rowsLoaded",
        day: DAY,
        history: history(adminRows(DAY)),
    });
    expect(recovered.editor).toMatchObject({ status: "editing", attempt });
    expect(recovered.editor).not.toHaveProperty("issue");
    const gone = reduceScreen(failed.screen, {
        type: "rowsLoaded",
        day: DAY,
        history: history([]),
    });
    expect(gone.editor).toMatchObject({ status: "orphaned", attempt });
    expect(gone.editor).not.toHaveProperty("error");
});

test("an access or day change keeps an editor whose write's outcome is unknown, and only a direct writer can send it again", () => {
    const lost = lostSet(3);
    const attempt = (lost.screen.editor as Draft).attempt;
    const demoted = apply(lost, { type: "accessChanged", access: "propose" });
    expect(demoted.screen.editor).toMatchObject({
        status: "orphaned",
        cause: "unavailable",
        pendingWrite: false,
        attempt,
    });
    expect(dropped(lost.screen, demoted.screen)).toBe(false);
    expect(repeatsOrphan(demoted.screen)).toBe(false);
    const promoted = apply(demoted, {
        type: "accessChanged",
        access: "direct",
    });
    expect(repeatsOrphan(promoted.screen)).toBe(true);
    expect(shown(lost, OTHER, adminRows(OTHER)).screen.editor).toMatchObject({
        status: "orphaned",
        cause: "day_left",
        pendingWrite: false,
        attempt,
    });
});

test("a reason of nothing but the outer whitespace the server strips is missing", () => {
    const sent = apply(
        staged,
        { type: "reason", text: "\u0085\u001f " },
        { type: "send", rows: dayRows(DAY) },
    );
    expect(sent.screen.draft?.send).toEqual({ status: "reason_missing" });
});

test("an editor counts as unsaved while its values differ from those it opened with or a write of it runs or is unconfirmed", () => {
    const eight = row(DAY, 8, { can_edit: true, can_revert: true });
    const open = (rows: Interval[]) =>
        [
            { type: "dayShown", day: DAY },
            { type: "open", row: eight },
            { type: "rowsLoaded", day: DAY, history: history(rows) },
        ] satisfies ScreenEvent[];
    const opened = (open([eight]) as ScreenEvent[]).reduce(
        reduceScreen,
        initialScreen(OTHER, "direct"),
    );
    expect(editorUnsaved(opened.editor)).toBe(false);
    const refreshed = reduceScreen(opened, {
        type: "rowsLoaded",
        day: DAY,
        history: history([
            {
                ...eight,
                effective: {
                    energy_mwh: 0.7,
                    quality: "valid",
                    source: "meter",
                    source_kind: "manual",
                    provisional: false,
                    aggregated: false,
                },
            },
        ]),
    });
    expect(editorUnsaved(refreshed.editor)).toBe(false);
    const orphaned = reduceScreen(opened, {
        type: "rowsLoaded",
        day: DAY,
        history: history([{ ...eight, can_edit: false }]),
    });
    expect(orphaned.editor.status).toBe("orphaned");
    expect(editorUnsaved(orphaned.editor)).toBe(false);
    const changed = reduceScreen(opened, { type: "change", energy: "0,5" });
    expect(editorUnsaved(changed.editor)).toBe(true);
    expect(
        editorUnsaved(
            reduceScreen(changed, { type: "change", energy: "1" }).editor,
        ),
    ).toBe(false);
    expect(
        editorUnsaved(
            reduceScreen(opened, { type: "change", quality: "outage" }).editor,
        ),
    ).toBe(true);
    const reverting = reduceScreen(opened, {
        type: "save",
        row: eight,
        action: "revert",
    });
    expect(reverting.editor.status).toBe("saving");
    expect(editorUnsaved(reverting.editor)).toBe(true);
    const lost = reduceScreen(reverting, {
        type: "saveFailed",
        error: lostAnswer(),
        implausible: false,
    });
    expect(editorUnsaved(lost.editor)).toBe(true);
    expect(
        editorUnsaved(
            reduceScreen(
                reduceScreen(lost, {
                    type: "save",
                    row: eight,
                    action: "revert",
                }),
                {
                    type: "saved",
                },
            ).editor,
        ),
    ).toBe(false);
});

test("input counts as unsent while a staged proposal is not on its way or lost, or editor values differ from what was opened and from every write sent", () => {
    const eight = row(DAY, 8, { can_edit: true, can_revert: true });
    const opened = reduceScreen(initialScreen(DAY, "direct"), {
        type: "open",
        row: eight,
    });
    expect(unsentInput(opened)).toBe(false);
    const changed = reduceScreen(opened, { type: "change", energy: "0,5" });
    expect(unsentInput(changed)).toBe(true);
    const saving = reduceScreen(changed, {
        type: "save",
        row: eight,
        action: "set",
    });
    expect(unsentInput(saving)).toBe(false);
    const lost = reduceScreen(saving, {
        type: "saveFailed",
        error: lostAnswer(),
        implausible: false,
    });
    expect(unsentInput(lost)).toBe(false);
    expect(
        unsentInput(reduceScreen(lost, { type: "change", energy: "0,6" })),
    ).toBe(true);
    const reverting = reduceScreen(opened, {
        type: "save",
        row: eight,
        action: "revert",
    });
    expect(
        unsentInput(
            reduceScreen(reverting, {
                type: "saveFailed",
                error: lostAnswer(),
                implausible: false,
            }),
        ),
    ).toBe(false);
    const proposing = reduceScreen(initialScreen(DAY, "propose"), {
        type: "open",
        row: row(DAY, 8),
    });
    const steps: ScreenEvent[] = [
        { type: "change", energy: "0,5" },
        { type: "stage", row: row(DAY, 8), action: "set" },
        { type: "reason", text: "verified" },
    ];
    const staged = steps.reduce(reduceScreen, proposing);
    expect(unsentInput(staged)).toBe(true);
    const sending = reduceScreen(staged, { type: "send", rows: [row(DAY, 8)] });
    expect(unsentInput(sending)).toBe(false);
    const failed = (error: unknown) =>
        reduceScreen(sending, {
            type: "sendFailed",
            error,
            refusals: [],
            fields: { general: [] },
        });
    expect(unsentInput(failed(lostAnswer()))).toBe(false);
    expect(unsentInput(failed(new ApiError("Refused.")))).toBe(true);
});
