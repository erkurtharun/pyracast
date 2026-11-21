import type { components } from "@/lib/schema";
import { ApiError } from "@/lib/api/errors";
import type { Quality } from "@/lib/production";

export type History = components["schemas"]["ProductionHistoryOutput"];
export type Interval = components["schemas"]["ProductionIntervalOutput"];

export type Draft = {
    key: string;
    energy: string;
    quality: Quality;
    expectedRevision: number | null;
    error?: string;
    implausible?: boolean;
};

export type Editor =
    | { status: "closed" }
    | ({ status: "editing" | "saving" | "reloading" } & Draft)
    | ({ status: "failed"; error: string } & Draft)
    // A write still running when its interval became unavailable keeps the controls locked until it ends.
    | ({ status: "orphaned"; error: string; pendingWrite: boolean } & Draft);

type Window = Pick<Interval, "starts_at" | "ends_at">;

// The default view: intervals that overlap daylight, and every interval from the first to the last one with production.
export function daylightRows(rows: Interval[], windows: readonly Window[]) {
    const producing = (row: Interval) => (row.effective?.energy_mwh ?? 0) > 0;
    const first = rows.findIndex(producing);
    const last = rows.findLastIndex(producing);
    return rows.filter(
        (row, index) =>
            (first !== -1 && index >= first && index <= last) ||
            windows.some(
                (window) =>
                    Date.parse(row.starts_at) < Date.parse(window.ends_at) &&
                    Date.parse(row.ends_at) > Date.parse(window.starts_at),
            ),
    );
}

export function intervalKey(row: Pick<Interval, "starts_at" | "ends_at">) {
    return `${row.starts_at}/${row.ends_at}`;
}

// Energy as typed in MWh, with a comma or a point as the decimal separator; null unless finite and non-negative.
export function energyValue(text: string) {
    const value = Number(text.trim().replace(",", "."));
    return text.trim() && Number.isFinite(value) && value >= 0 ? value : null;
}

export function shiftDay(day: string, offset: number) {
    const date = new Date(`${day}T12:00:00Z`);
    date.setUTCDate(date.getUTCDate() + offset);
    return date.toISOString().slice(0, 10);
}

export function reconcileEditor(
    editor: Editor,
    history: History,
    unavailableMessage: string,
): Editor {
    if (editor.status === "closed" || editor.status === "orphaned")
        return editor;
    const row = history.intervals.find(
        (row) => intervalKey(row) === editor.key,
    );
    if (!row || !(row.can_edit || row.can_propose))
        return {
            ...editor,
            status: "orphaned",
            error: unavailableMessage,
            pendingWrite: editor.status === "saving",
        };
    return editor.status === "reloading"
        ? {
              ...editor,
              status: "editing",
              expectedRevision: row.revision_id,
              error: undefined,
          }
        : editor;
}

export function mostFrequentReason(rows: Interval[]) {
    const counts = new Map<string, number>();
    for (const row of rows) {
        const code = row.proposal_unavailable_reason;
        if (code) counts.set(code, (counts.get(code) ?? 0) + 1);
    }
    let best: Interval | undefined;
    let most = 0;
    for (const row of rows) {
        const count = counts.get(row.proposal_unavailable_reason ?? "") ?? 0;
        if (count > most) {
            best = row;
            most = count;
        }
    }
    return best;
}

type Bounds = Pick<Interval, "starts_at" | "ends_at">;

export function sameInterval(a: Bounds, b: Bounds) {
    return (
        Date.parse(a.starts_at) === Date.parse(b.starts_at) &&
        Date.parse(a.ends_at) === Date.parse(b.ends_at)
    );
}

export function saveFailure(error: unknown) {
    return {
        error: (error as Error).message,
        implausible:
            error instanceof ApiError &&
            error.code === "production_implausible",
    };
}
