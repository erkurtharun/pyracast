import { expect, test } from "@playwright/test";
import { ApiError } from "../src/lib/api/errors";
import {
    intervalKey,
    mostFrequentReason,
    productionCsvRecords,
    reconcileEditor,
    sameInterval,
    saveFailure,
    shiftDay,
    type Editor,
    type History,
    type Interval,
} from "../src/features/production/history/model";

const provider = (starts_at: string, ends_at: string, energy_mwh: number) => ({
    starts_at,
    ends_at,
    energy_mwh,
    source_kind: "settlement_injection",
    snapshot_id: 1,
    collected_at: "2026-11-10T00:00:00Z",
    available_at: "2026-11-10T00:00:00Z",
    content_sha256: "",
    provisional: false,
});

function row(hour: number, fields: Partial<Interval> = {}): Interval {
    const starts_at = `2026-11-09T${String(hour).padStart(2, "0")}:00:00Z`;
    const ends_at = `2026-11-09T${String(hour + 1).padStart(2, "0")}:00:00Z`;
    return {
        starts_at,
        ends_at,
        observation: null,
        previous_configuration_observations: [],
        revision_id: hour,
        effective: null,
        provider_originals: [],
        can_edit: true,
        can_revert: false,
        edit_unavailable_reason: null,
        can_propose: false,
        can_propose_revert: false,
        proposal_unavailable_reason: null,
        ...fields,
    };
}

const history = (intervals: Interval[]) => ({ intervals }) as History;

const draft = (
    status: Exclude<Editor["status"], "closed">,
    key: string,
): Editor => {
    const fields = {
        key,
        energy: "2",
        quality: "valid" as const,
        expectedRevision: 1,
    };
    return status === "failed" || status === "orphaned"
        ? { status, ...fields, error: "gone" }
        : { status, ...fields };
};

test("days step across month and year ends", () => {
    expect(shiftDay("2026-12-31", 1)).toBe("2027-01-01");
    expect(shiftDay("2026-03-01", -1)).toBe("2026-02-28");
});

test("an open draft follows its interval and orphans when it disappears", () => {
    const eight = row(8, { revision_id: 7 });
    const key = intervalKey(eight);
    expect(key).toBe("2026-11-09T08:00:00Z/2026-11-09T09:00:00Z");
    const closed: Editor = { status: "closed" };
    expect(reconcileEditor(closed, history([]), "gone")).toBe(closed);
    const editing = draft("editing", key);
    expect(reconcileEditor(editing, history([eight]), "gone")).toBe(editing);
    expect(
        reconcileEditor(draft("reloading", key), history([eight]), "gone"),
    ).toEqual({ ...draft("editing", key), expectedRevision: 7 });
    expect(reconcileEditor(draft("saving", key), history([]), "gone")).toEqual({
        ...draft("orphaned", key),
        error: "gone",
        pendingWrite: true,
    });
    expect(
        reconcileEditor(
            draft("failed", key),
            history([row(8, { can_edit: false })]),
            "gone",
        ),
    ).toEqual({
        ...draft("orphaned", key),
        error: "gone",
        pendingWrite: false,
    });
    const orphaned = draft("orphaned", key);
    expect(reconcileEditor(orphaned, history([]), "gone")).toBe(orphaned);
});

test("the most frequent unavailable reason names one of its rows", () => {
    const rows = [
        row(1, { proposal_unavailable_reason: "settled" }),
        row(2, { proposal_unavailable_reason: "pending_request" }),
        row(3, { proposal_unavailable_reason: "pending_request" }),
        row(4),
    ];
    expect(mostFrequentReason(rows)).toBe(rows[1]);
    expect(mostFrequentReason([row(1)])).toBeUndefined();
});

test("intervals match by instant, not by their offset spelling", () => {
    const eight = row(8);
    expect(
        sameInterval(eight, {
            starts_at: "2026-11-09T11:00:00+03:00",
            ends_at: "2026-11-09T12:00:00+03:00",
        }),
    ).toBe(true);
    expect(
        sameInterval(eight, {
            starts_at: "2026-11-09T08:00:00Z",
            ends_at: "2026-11-09T08:30:00Z",
        }),
    ).toBe(false);
});

test("only an implausible-value refusal offers to save anyway", () => {
    expect(
        saveFailure(
            new ApiError("Above capacity.", {
                status: 400,
                code: "production_implausible",
            }),
        ),
    ).toEqual({ error: "Above capacity.", implausible: true });
    expect(
        saveFailure(
            new ApiError("Changed.", {
                status: 409,
                code: "production_revision_changed",
            }),
        ),
    ).toEqual({ error: "Changed.", implausible: false });
    expect(saveFailure(new Error("Offline."))).toEqual({
        error: "Offline.",
        implausible: false,
    });
});

test("CSV records carry the effective value and every provider original", () => {
    expect(
        productionCsvRecords([
            row(1),
            row(2, {
                effective: {
                    energy_mwh: 2,
                    quality: "valid",
                    source: "epias",
                    source_kind: "settlement_injection",
                },
                provider_originals: [
                    provider("x", "y", 1),
                    provider("y", "z", 1.5),
                ],
            } as Partial<Interval>),
        ]),
    ).toEqual([
        {
            starts_at: "2026-11-09T01:00:00Z",
            ends_at: "2026-11-09T02:00:00Z",
            energy: null,
            quality: "",
            source: "",
            provider_energy_mwh: "",
            provider_intervals: "",
            provider_source: "",
        },
        {
            starts_at: "2026-11-09T02:00:00Z",
            ends_at: "2026-11-09T03:00:00Z",
            energy: 2,
            quality: "valid",
            source: "epias",
            provider_energy_mwh: "1; 1.5",
            provider_intervals: "x/y; y/z",
            provider_source: "settlement_injection; settlement_injection",
        },
    ]);
});
