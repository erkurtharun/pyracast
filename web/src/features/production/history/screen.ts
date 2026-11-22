import type { Quality } from "@/lib/production";
import { qualityOf } from "@/lib/production";
import {
    energyValue,
    intervalKey,
    type Draft,
    type Editor,
    type History,
    type Interval,
} from "./model";
import {
    canStage,
    changedSinceStaged,
    stageChange,
    unstageChange,
    type FieldRefusals,
    type ProposalSend,
    type Refusal,
    type StagedChange,
} from "./proposal";

// What the viewer does with a row: an administrator writes it, any other member proposes, nobody acts without a configuration.
export type Access = "direct" | "propose" | "none";

export type Proposal = {
    day: string;
    items: StagedChange[];
    reason: string;
    send: ProposalSend;
};

// The one state of the production history screen; every control is derived from it and the shown day's rows.
export type Screen = {
    day: string;
    access: Access;
    fullDay: boolean;
    editor: Editor;
    draft: Proposal | null;
};

export type ScreenEvent =
    | { type: "open"; row: Interval; reveal?: boolean }
    | { type: "change"; energy?: string; quality?: Quality }
    | { type: "cancel" }
    | { type: "stage"; row: Interval; action: "set" | "revert" }
    | { type: "remove"; key: string }
    | { type: "discard" }
    | { type: "reason"; text: string }
    | { type: "send"; rows: readonly Interval[] | undefined }
    | { type: "sent" }
    | {
          type: "sendFailed";
          error: string;
          refusals: Refusal[];
          fields: FieldRefusals;
      }
    | { type: "save"; row: Interval; action: "set" | "revert" }
    | { type: "saved"; key: string }
    | { type: "saveFailed"; key: string; message: string; implausible: boolean }
    | { type: "reload" }
    | { type: "reloadFailed"; key: string; message: string }
    | { type: "dayShown"; day: string }
    | { type: "rowsLoaded"; day: string; history: History }
    | { type: "accessChanged"; access: Access }
    | { type: "fullDay"; on: boolean };

export type Controls = {
    dayNav: boolean;
    fullDay: boolean;
    refresh: boolean;
    rowActions: boolean;
    away: boolean;
    held: ReadonlySet<string>;
    stale: ReadonlySet<string>;
    editable: ReadonlySet<string>;
    remove: boolean;
    discard: boolean;
    reason: boolean;
    send: boolean;
};

const CLOSED: Editor = { status: "closed" };
const IDLE: ProposalSend = { status: "idle" };

export function initialScreen(day: string, access: Access): Screen {
    return { day, access, fullDay: false, editor: CLOSED, draft: null };
}

export function canAct(access: Access, row: Interval) {
    return access === "direct"
        ? row.can_edit
        : access === "propose" && canStage(row);
}

// A write still running keeps its editor until the write ends.
function runningWrite(editor: Editor) {
    return editor.status === "saving" ||
        (editor.status === "orphaned" && editor.pendingWrite)
        ? editor
        : null;
}

function writing(editor: Editor) {
    return runningWrite(editor) !== null;
}

// The day navigation and the editor's own controls wait for a write or for the editor's reload.
export function locked(editor: Editor) {
    return writing(editor) || editor.status === "reloading";
}

function sending(screen: Screen) {
    return screen.draft?.send.status === "sending";
}

function away(screen: Screen) {
    return screen.draft !== null && screen.draft.day !== screen.day;
}

function values(editor: Exclude<Editor, { status: "closed" }>): Draft {
    const { key, energy, quality, expectedRevision } = editor;
    return { key, energy, quality, expectedRevision };
}

function opened(row: Interval, item: StagedChange | undefined): Draft {
    return {
        key: intervalKey(row),
        energy:
            item?.action === "set"
                ? item.energy.toString()
                : (row.effective?.energy_mwh.toString() ?? ""),
        quality:
            item?.action === "set"
                ? item.quality
                : qualityOf(row.observation?.quality),
        expectedRevision: row.revision_id,
    };
}

function editingOn(editor: Editor, row: Interval) {
    return (editor.status === "editing" || editor.status === "failed") &&
        editor.key === intervalKey(row)
        ? editor
        : null;
}

// An editor whose day or access ends closes, unless its write still runs.
function release(editor: Editor, cause: "day_left" | "unavailable"): Editor {
    if (editor.status === "closed") return editor;
    return writing(editor)
        ? { ...values(editor), status: "orphaned", cause, pendingWrite: true }
        : CLOSED;
}

function forget(send: ProposalSend, key: string): ProposalSend {
    return send.status === "failed"
        ? {
              ...send,
              refusals: send.refusals.filter((refusal) => refusal.key !== key),
          }
        : send;
}

function heldKeys(items: readonly StagedChange[], rows: readonly Interval[]) {
    const held = new Set(
        rows.filter((row) => row.pending_request_id != null).map(intervalKey),
    );
    return new Set(
        items.filter((item) => held.has(item.key)).map((item) => item.key),
    );
}

export function reconcileEditor(
    editor: Editor,
    history: History,
    access: Access,
): Editor {
    if (editor.status === "closed" || editor.status === "orphaned")
        return editor;
    const row = history.intervals.find(
        (row) => intervalKey(row) === editor.key,
    );
    if (!row || !canAct(access, row))
        return {
            ...values(editor),
            status: "orphaned",
            cause:
                access === "propose" && row?.pending_request_id != null
                    ? "held"
                    : "unavailable",
            pendingWrite: editor.status === "saving",
        };
    return editor.status === "reloading"
        ? {
              ...values(editor),
              status: "editing",
              expectedRevision: row.revision_id,
              reloaded: true,
          }
        : editor;
}

// Whether an access change dropped what the person had prepared: the draft, or an open editor with its values.
export function dropped(before: Screen, after: Screen) {
    return (
        (before.draft !== null && after.draft === null) ||
        (before.editor.status !== "closed" && after.editor.status === "closed")
    );
}

export function reduceScreen(screen: Screen, event: ScreenEvent): Screen {
    const { editor, draft } = screen;
    switch (event.type) {
        case "open": {
            if (
                editor.status !== "closed" ||
                sending(screen) ||
                away(screen) ||
                !canAct(screen.access, event.row)
            )
                return screen;
            const key = intervalKey(event.row);
            const item = draft?.items.find((item) => item.key === key);
            return {
                ...screen,
                fullDay: screen.fullDay || event.reveal === true,
                editor: { status: "editing", ...opened(event.row, item) },
            };
        }
        case "change":
            if (editor.status !== "editing" && editor.status !== "failed")
                return screen;
            return {
                ...screen,
                editor: {
                    ...values(editor),
                    ...(event.energy !== undefined && { energy: event.energy }),
                    ...(event.quality !== undefined && {
                        quality: event.quality,
                    }),
                    status: "editing",
                },
            };
        case "cancel":
            if (editor.status === "closed" || locked(editor)) return screen;
            return { ...screen, editor: CLOSED };
        case "stage": {
            const open = editingOn(editor, event.row);
            if (
                screen.access !== "propose" ||
                !open ||
                sending(screen) ||
                away(screen) ||
                !canStage(event.row, event.action)
            )
                return screen;
            const energy = energyValue(open.energy);
            if (event.action === "set" && energy === null)
                return {
                    ...screen,
                    editor: {
                        ...values(open),
                        status: "failed",
                        issue: "invalid_energy",
                    },
                };
            const bounds = {
                key: open.key,
                starts_at: event.row.starts_at,
                ends_at: event.row.ends_at,
                expectedRevision: open.expectedRevision,
            };
            const change: StagedChange =
                event.action === "set" && energy !== null
                    ? {
                          ...bounds,
                          action: "set",
                          energy,
                          quality: open.quality,
                      }
                    : { ...bounds, action: "revert" };
            const base = draft ?? {
                day: screen.day,
                items: [],
                reason: "",
                send: IDLE,
            };
            return {
                ...screen,
                editor: CLOSED,
                draft: {
                    ...base,
                    items: stageChange(base.items, change),
                    send: forget(base.send, open.key),
                },
            };
        }
        case "remove": {
            if (!draft || sending(screen)) return screen;
            const items = unstageChange(draft.items, event.key);
            if (items.length === draft.items.length) return screen;
            return {
                ...screen,
                draft: items.length
                    ? { ...draft, items, send: forget(draft.send, event.key) }
                    : null,
            };
        }
        case "discard":
            if (!draft || sending(screen) || locked(editor)) return screen;
            return { ...screen, draft: null, editor: CLOSED };
        case "reason":
            if (!draft || sending(screen)) return screen;
            return {
                ...screen,
                draft: {
                    ...draft,
                    reason: event.text,
                    send:
                        draft.send.status === "reason_missing"
                            ? IDLE
                            : draft.send,
                },
            };
        case "send":
            if (!draft || !controls(screen, event.rows).send) return screen;
            return {
                ...screen,
                draft: {
                    ...draft,
                    // A new value each time, so a repeated refusal focuses the reason again.
                    send: draft.reason.trim()
                        ? { status: "sending" }
                        : { status: "reason_missing" },
                },
            };
        case "sent":
            if (!sending(screen)) return screen;
            return { ...screen, draft: null, editor: CLOSED };
        case "sendFailed":
            if (!draft || !sending(screen)) return screen;
            return {
                ...screen,
                draft: {
                    ...draft,
                    send: {
                        status: "failed",
                        error: event.error,
                        refusals: event.refusals,
                        fields: {
                            ...event.fields,
                            general: event.fields.general.filter(
                                (text) => text !== event.error,
                            ),
                        },
                    },
                },
            };
        case "save": {
            const open = editingOn(editor, event.row);
            if (
                screen.access !== "direct" ||
                !open ||
                !event.row.can_edit ||
                (event.action === "revert" && !event.row.can_revert)
            )
                return screen;
            if (event.action === "set" && energyValue(open.energy) === null)
                return {
                    ...screen,
                    editor: {
                        ...values(open),
                        status: "failed",
                        issue: "invalid_energy",
                    },
                };
            return { ...screen, editor: { ...values(open), status: "saving" } };
        }
        case "saved":
            if (runningWrite(editor)?.key !== event.key) return screen;
            return { ...screen, editor: CLOSED };
        case "saveFailed": {
            const write = runningWrite(editor);
            if (write?.key !== event.key) return screen;
            return {
                ...screen,
                editor:
                    write.status === "orphaned"
                        ? {
                              ...write,
                              pendingWrite: false,
                              error: event.message,
                          }
                        : {
                              ...values(write),
                              status: "failed",
                              issue: {
                                  message: event.message,
                                  implausible: event.implausible,
                              },
                          },
            };
        }
        case "reload":
            if (editor.status !== "editing" && editor.status !== "failed")
                return screen;
            return {
                ...screen,
                editor: { ...values(editor), status: "reloading" },
            };
        case "reloadFailed":
            if (editor.status !== "reloading" || editor.key !== event.key)
                return screen;
            return {
                ...screen,
                editor: {
                    ...values(editor),
                    status: "failed",
                    issue: { message: event.message, implausible: false },
                },
            };
        case "dayShown":
            if (event.day === screen.day) return screen;
            return {
                ...screen,
                day: event.day,
                editor: release(editor, "day_left"),
            };
        case "rowsLoaded": {
            if (event.day !== screen.day) return screen;
            const next = reconcileEditor(editor, event.history, screen.access);
            return next === editor ? screen : { ...screen, editor: next };
        }
        case "accessChanged":
            if (event.access === screen.access) return screen;
            return {
                ...screen,
                access: event.access,
                editor: release(editor, "unavailable"),
                draft: event.access === "propose" ? draft : null,
            };
        case "fullDay":
            if (editor.status !== "closed" || screen.fullDay === event.on)
                return screen;
            return { ...screen, fullDay: event.on };
    }
}

export function controls(
    screen: Screen,
    rows: readonly Interval[] | undefined,
): Controls {
    const { editor, draft } = screen;
    const isAway = away(screen);
    const busy = sending(screen);
    const closed = editor.status === "closed";
    const shown =
        draft && !isAway && rows ? { items: draft.items, rows } : null;
    const held = shown ? heldKeys(shown.items, shown.rows) : new Set<string>();
    const current = new Map(
        (shown?.rows ?? []).map((row) => [intervalKey(row), row]),
    );
    const editable = new Set(
        shown && closed && !busy
            ? shown.items
                  .filter((item) => {
                      const row = current.get(item.key);
                      return row !== undefined && canStage(row);
                  })
                  .map((item) => item.key)
            : [],
    );
    const open = draft !== null && !busy;
    return {
        dayNav: !locked(editor) && !draft,
        fullDay: closed,
        // An orphaned editor without a running write holds nothing a reload could lose.
        refresh:
            closed || (editor.status === "orphaned" && !editor.pendingWrite),
        rowActions: closed && !busy && !isAway,
        away: isAway,
        held,
        stale: shown
            ? changedSinceStaged(shown.items, shown.rows)
            : new Set<string>(),
        editable,
        remove: open,
        discard: open && !locked(editor),
        reason: open,
        send: open && shown !== null && closed && !held.size,
    };
}
