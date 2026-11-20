import { expect, test } from "@playwright/test";
import {
    chartIntervals,
    fullDayIntervals,
    scenarioIntervals,
} from "../src/features/forecast/panel/model";

const hour = (value: number) =>
    `2026-11-20T${String(value).padStart(2, "0")}:00:00.000Z`;
const interval = (start: number, power: number | null) => ({
    start: hour(start),
    end: hour(start + 1),
    mean_power_mw: power,
    energy_mwh: power,
});

test("the full day fills gaps before, between and after the intervals", () => {
    const rows = [interval(6, 1), interval(8, 2)];
    expect(fullDayIntervals(rows, hour(5), hour(10))).toEqual([
        {
            start: hour(5),
            end: hour(6),
            quality_flags: ["missing_source_interval"],
        },
        rows[0],
        {
            start: hour(7),
            end: hour(8),
            quality_flags: ["missing_source_interval"],
        },
        rows[1],
        {
            start: hour(9),
            end: hour(10),
            quality_flags: ["missing_source_interval"],
        },
    ]);
});

test("the full day keeps the rows when the day bounds are unusable", () => {
    const rows = [interval(6, 1)];
    expect(fullDayIntervals(rows, undefined, hour(10))).toBe(rows);
    expect(fullDayIntervals(rows, hour(5), "not a time")).toBe(rows);
    expect(fullDayIntervals(rows, hour(10), hour(5))).toBe(rows);
});

test("a scenario adds its values to the interval with the same start", () => {
    const rows = [interval(6, 1), interval(7, 2)];
    expect(scenarioIntervals(rows, null)).toBe(rows);
    expect(
        scenarioIntervals(rows, [
            {
                ...interval(7, 0.5),
                quality_flags: ["outage_configuration_changed"],
            },
        ]),
    ).toEqual([
        {
            ...rows[0],
            affected_mean_power_mw: null,
            affected_energy_mwh: null,
            affected_quality_flags: undefined,
        },
        {
            ...rows[1],
            affected_mean_power_mw: 0.5,
            affected_energy_mwh: 0.5,
            affected_quality_flags: ["outage_configuration_changed"],
        },
    ]);
});

test("the chart shows the generation hours unless the full day is chosen", () => {
    const rows = [
        interval(5, 0),
        interval(6, 1),
        interval(7, 0),
        interval(8, 2),
        interval(9, null),
    ];
    const day = { start: hour(4), end: hour(10) };
    expect(chartIntervals(rows, "mean_power_mw", false, day)).toEqual(
        rows.slice(1, 4),
    );
    expect(chartIntervals(rows, "mean_power_mw", true, day)).toEqual(
        fullDayIntervals(rows, day.start, day.end),
    );
    const night = [interval(5, 0), interval(6, 0), interval(7, null)];
    expect(chartIntervals(night, "mean_power_mw", false, day)).toEqual(
        night.slice(0, 2),
    );
    expect(chartIntervals([], "ghi_wm2", false, day)).toEqual([]);
});
