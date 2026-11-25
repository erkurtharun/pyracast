import { ApiError } from "@/lib/api/errors";
import type { Quality } from "@/lib/production";
import type { components } from "@/lib/schema";
import { intervalKey, type Interval, type RevertOutcome } from "./model";

type ChangeRow = components["schemas"]["ProductionChangeRow"];

export type StagedChange = {
    key: string;
    starts_at: string;
    ends_at: string;
    expectedRevision: number | null;
} & (
    | { action: "set"; energy: number; quality: Quality }
    | { action: "revert"; outcome: RevertOutcome }
);

export type Refusal = { key: string; message: string };

// Refused fields that name no item: the reason, and those of the request as a whole.
export type FieldRefusals = { reason?: string; general: string[] };

export type ProposalSend =
    | { status: "idle" }
    | { status: "reason_missing" }
    | { status: "sending" }
    | {
          status: "failed";
          error: unknown;
          refusals: Refusal[];
          fields: FieldRefusals;
      };

const ITEM_FIELD = /^changes\.(\d+)(?:\.|$)/;

// The member's own pending request of an interval is revised by withdrawing it, never by a second one.
export function canStage(row: Interval, action: "set" | "revert" = "set") {
    return (
        row.pending_request_id == null &&
        (action === "revert" ? row.can_propose_revert : row.can_propose)
    );
}

export function stageChange(
    items: readonly StagedChange[],
    change: StagedChange,
): StagedChange[] {
    return [...items.filter((item) => item.key !== change.key), change].sort(
        (a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at),
    );
}

export function unstageChange(items: readonly StagedChange[], key: string) {
    return items.filter((item) => item.key !== key);
}

// A staged change that can no longer be sent: its interval left the day, cannot be staged any more or has a newer revision.
export function changedSinceStaged(
    items: readonly StagedChange[],
    rows: readonly Interval[],
): Set<string> {
    const current = new Map(rows.map((row) => [intervalKey(row), row]));
    return new Set(
        items
            .filter((item) => {
                const row = current.get(item.key);
                return (
                    !row ||
                    !canStage(row, item.action) ||
                    row.revision_id !== item.expectedRevision
                );
            })
            .map((item) => item.key),
    );
}

// An edit on this screen is the operator's value; a revert ignores quality and source.
export function proposalChanges(items: readonly StagedChange[]): ChangeRow[] {
    return items.map((item) => ({
        starts_at: item.starts_at,
        ends_at: item.ends_at,
        expected_revision_id: item.expectedRevision,
        action: item.action,
        ...(item.action === "set"
            ? { energy: item.energy, quality: item.quality, source: "operator" }
            : { quality: "valid", source: "operator" }),
    }));
}

// The API names a refused item by its position in the sent list, as "changes.<index>.<field>".
export function refusals(
    error: unknown,
    items: readonly StagedChange[],
): Refusal[] {
    if (!(error instanceof ApiError)) return [];
    const messages = new Map<number, string[]>();
    for (const [field, message] of Object.entries(error.fields)) {
        const match = ITEM_FIELD.exec(field);
        if (match)
            messages.set(Number(match[1]), [
                ...(messages.get(Number(match[1])) ?? []),
                message,
            ]);
    }
    return items.flatMap((item, index) => {
        const found = messages.get(index);
        return found ? [{ key: item.key, message: found.join(" ") }] : [];
    });
}

export function fieldRefusals(error: unknown): FieldRefusals {
    const refused: FieldRefusals = { general: [] };
    if (!(error instanceof ApiError)) return refused;
    for (const [field, message] of Object.entries(error.fields)) {
        if (!message || ITEM_FIELD.test(field)) continue;
        if (field === "reason") refused.reason = message;
        else refused.general.push(message);
    }
    return refused;
}
