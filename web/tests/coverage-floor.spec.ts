import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { coverageShortfalls, lcovTotals } from "../testing/coverage-floor";

const record = (lines: [number, number], branches: [number, number]) =>
    [
        "SF:web/src/lib/example.ts",
        "FNF:4",
        "FNH:3",
        `LF:${lines[0]}`,
        `LH:${lines[1]}`,
        `BRF:${branches[0]}`,
        `BRH:${branches[1]}`,
        "end_of_record",
    ].join("\n");

test("the totals add every source record of the LCOV report", () => {
    expect(
        lcovTotals(`${record([10, 8], [4, 3])}\n${record([30, 30], [6, 6])}\n`),
    ).toEqual({
        lines: { found: 40, hit: 38 },
        branches: { found: 10, hit: 9 },
        functions: { found: 8, hit: 6 },
    });
});

test("coverage at the floor passes and coverage below it names each metric", () => {
    const floor = { lines: 95, branches: 90, functions: 75 };
    const report = `${record([10, 8], [4, 3])}\n${record([30, 30], [6, 6])}\n`;
    expect(coverageShortfalls(report, floor)).toEqual([]);
    const lowered = `${record([10, 7], [4, 2])}\n${record([30, 30], [6, 6])}\n`;
    expect(coverageShortfalls(lowered, floor)).toEqual([
        "lines 92.5% is below the floor of 95%",
        "branches 80% is below the floor of 90%",
    ]);
});

test("an LCOV report without measured lines does not pass the floor", () => {
    expect(
        coverageShortfalls("SF:web/src/lib/example.ts\nend_of_record\n", {
            lines: 1,
            branches: 1,
            functions: 1,
        }),
    ).toEqual([
        "lines 0% is below the floor of 1%",
        "branches 0% is below the floor of 1%",
        "functions 0% is below the floor of 1%",
    ]);
});

test("the committed floor names each metric as a percentage", () => {
    const floor = JSON.parse(
        readFileSync("coverage-floor.json", "utf8"),
    ) as Record<string, unknown>;
    expect(Object.keys(floor).sort()).toEqual([
        "branches",
        "functions",
        "lines",
    ]);
    for (const value of Object.values(floor))
        expect(value).toEqual(expect.any(Number));
});
