import { expect, test } from "@playwright/test";
import { ApiError } from "../src/lib/api/errors";
import type { Interval } from "../src/features/production/history/model";
import {
    changedSinceStaged,
    proposalChanges,
    refusedItems,
    stageChange,
    unstageChange,
    type StagedChange,
} from "../src/features/production/history/proposal";

function row(hour: number, fields: Partial<Interval> = {}): Interval {
    const starts_at = `2026-11-09T${String(hour).padStart(2, "0")}:00:00Z`;
    const ends_at = `2026-11-09T${String(hour + 1).padStart(2, "0")}:00:00Z`;
    return {
        starts_at,
        ends_at,
        observation: null,
        previous_configuration_observations: [],
        revision_id: null,
        effective: null,
        provider_originals: [],
        can_edit: false,
        can_revert: false,
        edit_unavailable_reason: "workspace_readonly",
        can_propose: true,
        can_propose_revert: false,
        proposal_unavailable_reason: null,
        pending_request_id: null,
        ...fields,
    };
}

function staged(hour: number, fields: Partial<StagedChange> = {}) {
    const interval = row(hour);
    return {
        key: `${interval.starts_at}/${interval.ends_at}`,
        starts_at: interval.starts_at,
        ends_at: interval.ends_at,
        expectedRevision: null,
        action: "set",
        energy: hour / 10,
        quality: "valid",
        ...fields,
    } as StagedChange;
}

test("staging keeps one change per interval in time order", () => {
    const nine = staged(9);
    const eight = staged(8);
    const items = stageChange(stageChange([], nine), eight);
    expect(items.map((item) => item.key)).toEqual([eight.key, nine.key]);

    const corrected = staged(9, { energy: 2 });
    expect(stageChange(items, corrected)).toEqual([eight, corrected]);
    expect(unstageChange(items, eight.key)).toEqual([nine]);
});

test("a staged change becomes a change row of one request", () => {
    expect(
        proposalChanges([
            staged(8, { expectedRevision: 4, energy: 0.75, quality: "outage" }),
            staged(9, { action: "revert", expectedRevision: 5 }),
        ]),
    ).toEqual([
        {
            starts_at: "2026-11-09T08:00:00Z",
            ends_at: "2026-11-09T09:00:00Z",
            expected_revision_id: 4,
            action: "set",
            energy: 0.75,
            quality: "outage",
            source: "operator",
        },
        {
            starts_at: "2026-11-09T09:00:00Z",
            ends_at: "2026-11-09T10:00:00Z",
            expected_revision_id: 5,
            action: "revert",
            quality: "valid",
            source: "operator",
        },
    ]);
});

test("a staged change whose interval moved on since staging is named", () => {
    const items = [
        staged(8),
        staged(9, { expectedRevision: 3 }),
        staged(10, { action: "revert", expectedRevision: 6 }),
        staged(11),
        staged(12),
    ];
    const rows = [
        row(8),
        row(9, { revision_id: 4 }),
        row(10, { revision_id: 6, can_propose_revert: false }),
        row(11, { can_propose: false }),
    ];
    expect([...changedSinceStaged(items, rows)]).toEqual([
        items[1]!.key,
        items[2]!.key,
        items[3]!.key,
        items[4]!.key,
    ]);
    expect(
        changedSinceStaged(
            [staged(10, { action: "revert", expectedRevision: 6 })],
            [row(10, { revision_id: 6, can_propose_revert: true })],
        ).size,
    ).toBe(0);
});

test("a refusal naming an item by its position names that staged change", () => {
    const items = [staged(8), staged(9), staged(10)];
    const refusal = new ApiError("Invalid", {
        status: 400,
        code: "invalid_request",
        fields: {
            "changes.2.energy": "Too large",
            "changes.9": "Gone",
            reason: "",
        },
    });
    expect(refusedItems(refusal, items)).toEqual([items[2]!.key]);
    expect(
        refusedItems(
            new ApiError("Full", {
                status: 409,
                code: "pending_request_limit",
            }),
            items,
        ),
    ).toEqual([]);
    expect(refusedItems(new Error("offline"), items)).toEqual([]);
});
