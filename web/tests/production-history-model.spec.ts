import { expect, test } from "@playwright/test";
import { ApiError } from "../src/lib/api/errors";
import {
    daylightRows,
    energyValue,
    intervalKey,
    mostFrequentReason,
    sameInterval,
    saveFailure,
    shiftDay,
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
        pending_request_id: null,
        ...fields,
    };
}

test("typed energy accepts a comma or a point and only finite non-negative values", () => {
    expect(energyValue(" 0,75 ")).toBe(0.75);
    expect(energyValue("2.5")).toBe(2.5);
    expect(energyValue("0")).toBe(0);
    for (const text of ["", "  ", "-1", "abc", "Infinity"])
        expect(energyValue(text)).toBeNull();
});

test("days step across month and year ends", () => {
    expect(shiftDay("2026-12-31", 1)).toBe("2027-01-01");
    expect(shiftDay("2026-03-01", -1)).toBe("2026-02-28");
});

test("an interval's key is its start and end", () => {
    expect(intervalKey(row(8))).toBe(
        "2026-11-09T08:00:00Z/2026-11-09T09:00:00Z",
    );
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

test("the default view keeps intervals that overlap daylight and the whole producing range", () => {
    const value = (energy_mwh: number) => ({
        effective: {
            energy_mwh,
            quality: "valid",
            source: "operator",
            source_kind: "manual",
            provisional: false,
            aggregated: false,
        },
        provider_originals: [provider("x", "y", 1)],
    });
    const rows = [
        row(1, value(0)),
        row(2, value(0.1)),
        row(3),
        row(4, value(0.2)),
        row(5),
        row(6),
        row(17),
        row(18),
    ];
    expect(
        daylightRows(rows, [
            {
                starts_at: "2026-11-09T05:30:00Z",
                ends_at: "2026-11-09T17:30:00Z",
            },
        ]),
    ).toEqual(rows.slice(1, 7));
    expect(daylightRows(rows, [])).toEqual(rows.slice(1, 4));
    expect(daylightRows([row(1, value(0))], [])).toEqual([]);
});
