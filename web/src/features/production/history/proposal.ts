import { ApiError } from "@/lib/api/errors";
import type { Quality } from "@/lib/production";
import type { components } from "@/lib/schema";
import { intervalKey, type Interval } from "./model";

type ChangeRow = components["schemas"]["ProductionChangeRow"];

export type StagedChange = {
    key: string;
    starts_at: string;
    ends_at: string;
    expectedRevision: number | null;
} & (
    { action: "set"; energy: number; quality: Quality } | { action: "revert" }
);

export type Refusal = { item: StagedChange; message: string };

export type ProposalSend =
    | { status: "idle" }
    | { status: "sending" }
    | { status: "failed"; error: string; refusals: Refusal[] };

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

// A staged change the server would refuse as stale: its interval left the day, can no longer be proposed or has a newer revision.
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
                    !(item.action === "revert"
                        ? row.can_propose_revert
                        : row.can_propose) ||
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
        const match = /^changes\.(\d+)(?:\.|$)/.exec(field);
        if (match)
            messages.set(Number(match[1]), [
                ...(messages.get(Number(match[1])) ?? []),
                message,
            ]);
    }
    return items.flatMap((item, index) => {
        const found = messages.get(index);
        return found ? [{ item, message: found.join(" ") }] : [];
    });
}
