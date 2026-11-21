import { expect, test } from "@playwright/test";
import type { Plant } from "../src/lib/api";
import {
    conflictDraft,
    loadedDraft,
    type Draft,
    type Values,
} from "../src/features/plant/workspace/parameters-draft";

const parameters = {
    dc_capacity_mw: 5,
    inverter_capacity_mw: 4,
    export_limit_mw: null,
    measurement_basis: "net_export",
    mounting_type: "fixed",
    surface_tilt_degrees: 20,
    surface_azimuth_degrees: 180,
} satisfies NonNullable<Plant["parameters"]>;

type Parameters = NonNullable<Plant["parameters"]>;

function server(
    revision: number,
    changes: Partial<Parameters> = {},
    name = "East array",
) {
    const declared: Parameters = { ...parameters, ...changes };
    const plant = {
        id: 21,
        name,
        current_configuration: null,
        parameters: declared,
        parameters_revision: revision,
    } as Plant;
    return [plant, declared] as const;
}

const typed = (draft: Draft, changes: Partial<Values>): Values => ({
    ...draft.values,
    ...changes,
});

const opened = () => loadedDraft(...server(1), 0);

test("conflicts in a row keep every edit the person made", () => {
    const first = conflictDraft(
        opened(),
        typed(opened(), { export_limit_mw: 3 }),
        ...server(2, { dc_capacity_mw: 6 }),
    );
    expect(first.base).toBe(2);
    expect(first.values).toMatchObject({
        dc_capacity_mw: 6,
        export_limit_mw: 3,
    });

    const second = conflictDraft(
        first,
        typed(first, {}),
        ...server(3, { dc_capacity_mw: 8 }),
    );
    expect(second.base).toBe(3);
    expect(second.values).toMatchObject({
        dc_capacity_mw: 8,
        export_limit_mw: 3,
    });

    const third = conflictDraft(
        second,
        typed(second, { name: "West array" }),
        ...server(4, { dc_capacity_mw: 8, inverter_capacity_mw: 3 }),
    );
    expect(third.key).toBe(3);
    expect(third.values).toMatchObject({
        name: "West array",
        dc_capacity_mw: 8,
        inverter_capacity_mw: 3,
        export_limit_mw: 3,
    });
    expect(third.conflict).toMatchObject({
        changed: ["dc_capacity_mw", "inverter_capacity_mw"],
        contested: [],
        dropped: [],
    });
    expect(third.conflict?.before.dc_capacity_mw).toBe(5);
    expect(third.conflict?.current.dc_capacity_mw).toBe(8);
});

test("a field both sides changed keeps the typed value and names the server's", () => {
    const next = conflictDraft(
        opened(),
        typed(opened(), { dc_capacity_mw: 7 }),
        ...server(2, { dc_capacity_mw: 6 }),
    );
    expect(next.values.dc_capacity_mw).toBe(7);
    expect(next.conflict).toMatchObject({
        changed: ["dc_capacity_mw"],
        contested: ["dc_capacity_mw"],
        dropped: [],
    });
    expect(next.conflict?.current.dc_capacity_mw).toBe(6);
    expect(next.conflict?.typed.dc_capacity_mw).toBe(7);

    const again = conflictDraft(
        next,
        typed(next, {}),
        ...server(3, { dc_capacity_mw: 6, export_limit_mw: 2 }),
    );
    expect(again.values).toMatchObject({
        dc_capacity_mw: 7,
        export_limit_mw: 2,
    });
    expect(again.conflict?.contested).toEqual(["dc_capacity_mw"]);
});

test("an angle typed for a mounting type the server changed is dropped and named", () => {
    const next = conflictDraft(
        opened(),
        typed(opened(), { surface_tilt_degrees: 25 }),
        ...server(2, {
            mounting_type: "tracking",
            surface_tilt_degrees: null,
            surface_azimuth_degrees: null,
        }),
    );
    expect(next.values).toMatchObject({
        mounting_type: "tracking",
        surface_tilt_degrees: null,
        surface_azimuth_degrees: null,
    });
    expect(next.conflict).toMatchObject({
        changed: [
            "mounting_type",
            "surface_tilt_degrees",
            "surface_azimuth_degrees",
        ],
        contested: [],
        dropped: ["surface_tilt_degrees"],
    });
    expect(next.conflict?.typed.surface_tilt_degrees).toBe(25);
});

test("a save or a reload clears the conflict", () => {
    const conflicted = conflictDraft(
        opened(),
        typed(opened(), { dc_capacity_mw: 7 }),
        ...server(2, { dc_capacity_mw: 6 }),
    );
    expect(conflicted.edited).toBe(true);
    const saved = loadedDraft(...server(3, { dc_capacity_mw: 7 }), 2);
    expect(saved).toMatchObject({ base: 3, edited: false, conflict: null });
});
