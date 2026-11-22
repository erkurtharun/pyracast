import { expect, test } from "@playwright/test";
import type { Plant } from "../src/lib/api";
import { latestPlant } from "../src/features/plant/workspace/latest-plant";

const plant = (parameters_revision: number, fields: Partial<Plant> = {}) =>
    ({
        id: 21,
        name: "East array",
        current_configuration: null,
        parameters: null,
        parameters_revision,
        ...fields,
    }) as Plant;

test("without a saved answer the server plant is shown", () => {
    const server = plant(1);
    expect(latestPlant(server)).toBe(server);
});

test("a saved answer stands until the server reaches its revision", () => {
    const saved = plant(2, { name: "West array" });
    expect(latestPlant(plant(1), saved)).toBe(saved);
    const caught = plant(2);
    expect(latestPlant(caught, saved)).toBe(caught);
    const newer = plant(3);
    expect(latestPlant(newer, saved)).toBe(newer);
});
