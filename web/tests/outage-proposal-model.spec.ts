import { expect, test } from "@playwright/test";
import {
    editorHold,
    proposalActs,
    type Outage,
    type OutageEditing,
} from "../src/features/plant/outage/model";

function outage(id: number): Outage {
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
    };
}

const offer = (
    held: number | null,
    reason: "pending_request_limit" | null = null,
) => ({
    proposal_unavailable_reason: reason,
    results: [4, 5].map((id) => ({
        id,
        pending_request_id: id === held ? 9 : null,
    })),
});

const editors: Record<string, OutageEditing> = {
    closed: { status: "closed" },
    "open on 4": { status: "open", outage: outage(4) },
    "open on a new outage": { status: "open" },
};
const offers = {
    free: offer(null),
    "4 held": offer(4),
    "at the limit": offer(null, "pending_request_limit"),
    unread: undefined,
};

test("a member's open outage editor sends only while its outage holds no request of theirs and the limit is not reached", () => {
    const actual = Object.fromEntries(
        Object.entries(editors).map(([name, editing]) => [
            name,
            Object.fromEntries(
                Object.entries(offers).map(([state, page]) => [
                    state,
                    editorHold(editing, page, false),
                ]),
            ),
        ]),
    );
    expect(actual).toEqual({
        closed: {
            free: null,
            "4 held": null,
            "at the limit": null,
            unread: null,
        },
        "open on 4": {
            free: null,
            "4 held": "held",
            "at the limit": "pending_request_limit",
            unread: null,
        },
        "open on a new outage": {
            free: null,
            "4 held": null,
            "at the limit": "pending_request_limit",
            unread: null,
        },
    });
    for (const editing of Object.values(editors))
        for (const page of Object.values(offers))
            expect(editorHold(editing, page, true)).toBeNull();
});

test("an outage card proposes only without the member's request, below the limit and without an editor open on it", () => {
    const actual = Object.fromEntries(
        Object.entries(editors).map(([name, editing]) => [
            name,
            Object.fromEntries(
                Object.entries(offers).map(([state, page]) => [
                    state,
                    [4, 5].map((id) =>
                        proposalActs(
                            {
                                id,
                                pending_request_id:
                                    page?.results.find((row) => row.id === id)
                                        ?.pending_request_id ?? null,
                            },
                            editing,
                            page,
                            false,
                        ),
                    ),
                ]),
            ),
        ]),
    );
    expect(actual).toEqual({
        closed: {
            free: [true, true],
            "4 held": [false, true],
            "at the limit": [false, false],
            unread: [true, true],
        },
        "open on 4": {
            free: [false, true],
            "4 held": [false, true],
            "at the limit": [false, false],
            unread: [false, true],
        },
        "open on a new outage": {
            free: [true, true],
            "4 held": [false, true],
            "at the limit": [false, false],
            unread: [true, true],
        },
    });
    expect(
        proposalActs(
            { id: 4, pending_request_id: 9 },
            editors["open on 4"],
            offers["at the limit"],
            true,
        ),
    ).toBe(true);
});
