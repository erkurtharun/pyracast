import { expect, test } from "@playwright/test";
import {
    MISSING,
    formatNumber,
    formatQuantity,
    formatSigned,
    formatSignificant,
} from "../src/lib/format/number";
import {
    daysZoneLabel,
    formatTime,
    fromPlantInput,
    intervalTimeFormatter,
    plantDay,
    plantTimestamp,
    toPlantInput,
    zoneLabel,
} from "../src/lib/format/time";

test("numbers keep three fractional digits and show a missing value as one dash", () => {
    expect(formatNumber("en", 1234.56789)).toBe("1,234.568");
    expect(formatNumber("tr", 1234.5)).toBe("1.234,5");
    expect(formatNumber("en", 0.1234567, 6)).toBe("0.123457");
    expect(MISSING).toBe("—");
    for (const value of [null, undefined, Number.NaN, "5"])
        expect(formatNumber("en", value)).toBe(MISSING);
});

test("significant digits round to the requested precision", () => {
    expect(formatSignificant("tr", 1234.5, 4)).toBe("1.235");
    expect(formatSignificant("en", 0.000123456, 2)).toBe("0.00012");
    expect(formatSignificant("en", null, 4)).toBe(MISSING);
});

test("a signed number shows its sign with a minus sign and no sign once it rounds to zero", () => {
    expect(formatSigned("en", 0.424, 2)).toBe("+0.42");
    expect(formatSigned("tr", -1.5, 2)).toBe("−1,50");
    expect(formatSigned("en", -0.001, 2)).toBe("0.00");
    expect(formatSigned("en", null, 2)).toBe(MISSING);
});

test("quantities carry their unit and missing quantities carry none", () => {
    expect(formatQuantity("en", 2.5, "MWh")).toBe("2.5 MWh");
    expect(formatQuantity("tr", 2.5, "MW")).toBe("2,5 MW");
    expect(formatQuantity("en", 0.1234567, "MW", 6)).toBe("0.123457 MW");
    expect(formatQuantity("en", null, "MW")).toBe(MISSING);
});

test("times render in the zone they are given and name it only when asked", () => {
    const instant = "2026-01-07T09:00:00Z";
    expect(
        formatTime("en", instant, "Europe/Istanbul", {
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
        }),
    ).toBe("12:00");
    expect(
        formatTime("en", instant, "UTC", {
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
        }),
    ).toBe("09:00");
    expect(
        formatTime("en", new Date(instant), "Europe/Istanbul", {
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
            timeZoneName: "shortOffset",
        }),
    ).toBe("12:00 GMT+3");
    expect(formatTime("en", "not a time", "UTC")).toBe(MISSING);
    expect(formatTime("en", null, "UTC")).toBe(MISSING);
});

test("a zone label names the plant's offset from UTC at the given instant", () => {
    expect(zoneLabel("Europe/Istanbul", new Date("2026-01-07T09:00:00Z"))).toBe(
        "UTC+3",
    );
    expect(zoneLabel("Europe/Berlin", new Date("2026-07-07T09:00:00Z"))).toBe(
        "UTC+2",
    );
    expect(zoneLabel("Europe/Berlin", new Date("2026-01-07T09:00:00Z"))).toBe(
        "UTC+1",
    );
    expect(zoneLabel("Asia/Kolkata", new Date("2026-01-07T09:00:00Z"))).toBe(
        "UTC+5:30",
    );
    expect(
        zoneLabel("America/St_Johns", new Date("2026-01-07T09:00:00Z")),
    ).toBe("UTC-3:30");
    expect(zoneLabel("UTC", new Date("2026-01-07T09:00:00Z"))).toBe("UTC");
});

test("a plant day is the calendar date in the plant zone, shifted by whole days", () => {
    const now = new Date("2026-01-07T22:30:00Z");
    expect(plantDay("Europe/Istanbul", 0, now)).toBe("2026-01-08");
    expect(plantDay("UTC", 0, now)).toBe("2026-01-07");
    expect(plantDay("Europe/Istanbul", 1, now)).toBe("2026-01-09");
    expect(plantDay("Europe/Istanbul", -8, now)).toBe("2025-12-31");
});

test("interval times name the offset only when it changes inside the range", () => {
    const steady = intervalTimeFormatter("en", "Europe/Istanbul", [
        "2026-01-07T09:00:00Z",
        "2026-01-07T10:00:00Z",
    ]);
    expect(steady.format(new Date("2026-01-07T09:00:00Z"))).toBe("12:00");
    const shift = intervalTimeFormatter("en", "Europe/Berlin", [
        "2026-03-29T00:00:00Z",
        "2026-03-29T01:30:00Z",
    ]);
    expect(shift.format(new Date("2026-03-29T00:00:00Z"))).toBe("01:00 GMT+1");
});

test("a plant timestamp is the ISO wall time in the plant zone with its offset", () => {
    expect(plantTimestamp("2026-11-11T08:00:00Z", "Europe/Istanbul")).toBe(
        "2026-11-11T11:00:00+03:00",
    );
    expect(plantTimestamp("2026-03-29T01:30:00Z", "Europe/Berlin")).toBe(
        "2026-03-29T03:30:00+02:00",
    );
    expect(plantTimestamp("2026-01-07T09:00:00Z", "UTC")).toBe(
        "2026-01-07T09:00:00+00:00",
    );
    expect(plantTimestamp(null, "UTC")).toBeNull();
});

test("a plant time input shows the wall clock of the plant zone", () => {
    expect(toPlantInput("2026-11-11T08:00:00Z", "Europe/Istanbul")).toBe(
        "2026-11-11T11:00",
    );
    expect(toPlantInput("2026-11-11T08:00:00Z", "America/New_York")).toBe(
        "2026-11-11T03:00",
    );
});

test("an entered plant time is read in the plant zone and a missing hour is refused", () => {
    expect(fromPlantInput("2026-11-11T11:00", "Europe/Istanbul")).toBe(
        "2026-11-11T08:00:00.000Z",
    );
    expect(fromPlantInput("2026-03-29T02:30", "Europe/Berlin")).toBeNull();
    expect(fromPlantInput("2026-10-25T02:30", "Europe/Berlin")).toBe(
        "2026-10-25T00:30:00.000Z",
    );
    expect(fromPlantInput("", "Europe/Berlin")).toBeNull();
    expect(fromPlantInput("2026-02-30T10:00", "Europe/Berlin")).toBeNull();
});

test("the offset of plant days names each offset of a day with a clock change", () => {
    expect(daysZoneLabel("Europe/Berlin", ["2026-07-15", "2026-07-15"])).toBe(
        "UTC+2",
    );
    expect(daysZoneLabel("Europe/Berlin", ["2026-03-29", "2026-03-29"])).toBe(
        "UTC+1 / UTC+2",
    );
    expect(daysZoneLabel("Europe/Berlin", ["2026-10-25", "2026-10-25"])).toBe(
        "UTC+2 / UTC+1",
    );
    expect(daysZoneLabel("Europe/Berlin", ["2026-10-11", "2026-11-09"])).toBe(
        "UTC+2 / UTC+1",
    );
    expect(daysZoneLabel("Europe/Istanbul", ["2026-03-29", "2026-10-25"])).toBe(
        "UTC+3",
    );
});
