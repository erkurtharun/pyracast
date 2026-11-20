import { expect, test } from "@playwright/test";
import type { Plant } from "../src/lib/api";
import { latestPlant } from "../src/features/plant/workspace/latest-plant";

const configuration = (id: number, revision: number) =>
    ({ id, revision }) as NonNullable<Plant["current_configuration"]>;
const submission = (dc_capacity_mw: number) =>
    ({
        dc_capacity_mw,
        inverter_capacity_mw: 4,
        export_limit_mw: null,
        measurement_basis: "net_export",
        mounting_type: "fixed",
        surface_tilt_degrees: 20,
        surface_azimuth_degrees: 180,
    }) as unknown as NonNullable<Plant["submission"]>;
const plant = (fields: Partial<Plant>) =>
    ({
        id: 21,
        name: "East array",
        current_configuration: null,
        submission: null,
        ...fields,
    }) as Plant;

test("without a saved answer the server plant is shown", () => {
    const server = plant({});
    expect(latestPlant(server)).toBe(server);
});

test("a saved configuration stands until the server catches up", () => {
    const saved = plant({ current_configuration: configuration(2, 2) });
    const older = plant({ current_configuration: configuration(1, 1) });
    expect(latestPlant(older, saved)).toBe(saved);
    const newer = plant({ current_configuration: configuration(3, 3) });
    expect(latestPlant(newer, saved)).toBe(newer);
    const same = plant({ current_configuration: configuration(2, 2) });
    expect(latestPlant(same, saved)).toBe(same);
    const renamed = plant({
        name: "West array",
        current_configuration: configuration(2, 2),
    });
    expect(latestPlant(renamed, saved)).toBe(saved);
    expect(latestPlant(older, plant({}))).toBe(older);
});

test("an older pending submission cannot replace a saved technical update", () => {
    const saved = plant({ submission: submission(6) });
    expect(latestPlant(plant({ submission: submission(5) }), saved)).toBe(
        saved,
    );
    const matching = plant({ submission: submission(6) });
    expect(latestPlant(matching, saved)).toBe(matching);
    expect(latestPlant(plant({}), saved)).toBe(saved);
});
