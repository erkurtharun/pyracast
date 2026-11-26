import { expect, test } from "@playwright/test";
import {
    MISSING,
    fileNumber,
    formatComputed,
    formatPercent,
    formatRecorded,
    formatRecordedQuantity,
    formatRounded,
    formatSigned,
} from "../src/lib/format/number";
import {
    daysZoneLabel,
    formatClock,
    formatDate,
    formatDay,
    formatTime,
    formatZonedClock,
    formatZonedTime,
    fromPlantInput,
    intervalClock,
    plantDay,
    plantTimestamp,
    toPlantInput,
    zoneLabel,
} from "../src/lib/format/time";

const UNIT_SPACE = " ";

test("a recorded value shows every decimal it has with the language's separators", () => {
    expect(formatRecorded("en", 0.0004)).toBe("0.0004");
    expect(formatRecorded("tr", 0.0004)).toBe("0,0004");
    expect(formatRecorded("en", 1.234567)).toBe("1.234567");
    expect(formatRecorded("tr", 1.234567)).toBe("1,234567");
    expect(formatRecorded("en", 1284)).toBe("1,284");
    expect(formatRecorded("tr", 1284)).toBe("1.284");
    expect(formatRecorded("en", 1284.25)).toBe("1,284.25");
    expect(formatRecorded("tr", -1234567.891)).toBe("-1.234.567,891");
    expect(formatRecorded("en", 0)).toBe("0");
    expect(MISSING).toBe("—");
    for (const value of [null, undefined, Number.NaN, "5"]) {
        expect(formatRecorded("en", value)).toBe(MISSING);
        expect(formatRecorded("tr", value)).toBe(MISSING);
    }
});

test("a computed figure keeps up to three decimals and drops the sign of a zero", () => {
    expect(formatComputed("en", 1234.56789)).toBe("1,234.568");
    expect(formatComputed("tr", 1234.56789)).toBe("1.234,568");
    expect(formatComputed("en", 0.5)).toBe("0.5");
    expect(formatComputed("tr", -0.0001)).toBe("0");
    expect(formatComputed("en", null)).toBe(MISSING);
});

test("numbers format where the browser refuses the negative sign display", () => {
    const NumberFormat = Intl.NumberFormat;
    class OlderNumberFormat extends NumberFormat {
        constructor(
            locales?: Intl.LocalesArgument,
            options?: Intl.NumberFormatOptions,
        ) {
            if (options?.signDisplay === "negative")
                throw new RangeError("signDisplay");
            super(locales, options);
        }
    }
    Object.defineProperty(Intl, "NumberFormat", { value: OlderNumberFormat });
    try {
        expect(formatRounded("en", 12.5, "MWh")).toBe("12.50");
        expect(formatRounded("tr", -0.001, "MWh")).toBe("0,00");
        expect(formatRecorded("tr", -1.5)).toBe("-1,5");
        expect(formatComputed("en", -0.0001)).toBe("0");
        expect(formatPercent("tr", -0.004)).toBe("%0");
        expect(formatSigned("en", -0.424, 2)).toBe("−0.42");
    } finally {
        Object.defineProperty(Intl, "NumberFormat", { value: NumberFormat });
    }
});

test("a summary is rounded by its unit and keeps zero apart from a missing value", () => {
    expect(formatRounded("en", 12.5, "MWh")).toBe("12.50");
    expect(formatRounded("tr", 12.5, "MWh")).toBe("12,50");
    expect(formatRounded("en", 1284.256, "MW")).toBe("1,284.26");
    expect(formatRounded("tr", 1284.256, "kWh/m²")).toBe("1.284,26");
    expect(formatRounded("en", 512.5, "W/m²")).toBe("513");
    expect(formatRounded("tr", 1284.4, "W/m²")).toBe("1.284");
    expect(formatRounded("en", -3.456, "MWh")).toBe("-3.46");
    expect(formatRounded("en", -0.001, "MWh")).toBe("0.00");
    expect(formatRounded("tr", 0, "MWh")).toBe("0,00");
    expect(formatRounded("en", null, "MWh")).toBe(MISSING);
});

test("a percent is whole and stands where the language puts it", () => {
    expect(formatPercent("en", 0.123)).toBe("12%");
    expect(formatPercent("tr", 0.123)).toBe("%12");
    expect(formatPercent("en", 12.345)).toBe("1,235%");
    expect(formatPercent("tr", -0.25)).toBe("-%25");
    expect(formatPercent("en", -0.004)).toBe("0%");
    expect(formatPercent("tr", 0)).toBe("%0");
    expect(formatPercent("en", null)).toBe(MISSING);
});

test("a signed number shows its sign with a minus sign and no sign once it rounds to zero", () => {
    expect(formatSigned("en", 0.424, 2)).toBe("+0.42");
    expect(formatSigned("tr", -1.5, 2)).toBe("−1,50");
    expect(formatSigned("en", -1284.5, 2)).toBe("−1,284.50");
    expect(formatSigned("en", -0.001, 2)).toBe("0.00");
    expect(formatSigned("en", null, 2)).toBe(MISSING);
});

test("a recorded quantity joins number and unit with a non-breaking space and a missing one has no unit", () => {
    expect(formatRecordedQuantity("en", 2.5, "MWh")).toBe(
        `2.5${UNIT_SPACE}MWh`,
    );
    expect(formatRecordedQuantity("tr", 1284.5, "MW")).toBe(
        `1.284,5${UNIT_SPACE}MW`,
    );
    expect(formatRecordedQuantity("en", 1.234567, "MWp")).toBe(
        `1.234567${UNIT_SPACE}MWp`,
    );
    expect(formatRecordedQuantity("tr", 0.0004, "MWh")).toBe(
        `0,0004${UNIT_SPACE}MWh`,
    );
    expect(formatRecordedQuantity("tr", 0, "MWp")).toBe(`0${UNIT_SPACE}MWp`);
    expect(formatRecordedQuantity("en", null, "MW")).toBe(MISSING);
});

test("a file number is the plain stored value and an impossible one is empty", () => {
    expect(fileNumber(1284.256789)).toBe("1284.256789");
    expect(fileNumber(-0.5)).toBe("-0.5");
    expect(fileNumber(0)).toBe("0");
    expect(fileNumber(Number.NaN)).toBe("");
});

test("a day reads with the short English or the full Turkish month", () => {
    expect(formatDay("en", "2026-11-26")).toBe("26 Nov 2026");
    expect(formatDay("tr", "2026-11-26")).toBe("26 Kasım 2026");
    expect(formatDay("en", "2026-09-01")).toBe("1 Sep 2026");
    expect(formatDay("tr", "2026-12-31")).toBe("31 Aralık 2026");
    expect(formatDay("en", "2027-01-01")).toBe("1 Jan 2027");
    expect(formatDay("en", "26.11.2026")).toBe(MISSING);
});

test("the date of an instant is the calendar date in the given zone", () => {
    const newYear = "2026-12-31T21:30:00Z";
    expect(formatDate("en", newYear, "UTC")).toBe("31 Dec 2026");
    expect(formatDate("tr", newYear, "Europe/Istanbul")).toBe("1 Ocak 2027");
    expect(formatDate("en", null, "UTC")).toBe(MISSING);
});

test("a clock has 24 hours in the given zone", () => {
    expect(formatClock("2026-01-07T21:00:00Z", "Europe/Istanbul")).toBe(
        "00:00",
    );
    expect(formatClock("2026-01-07T16:02:00Z", "UTC")).toBe("16:02");
    expect(formatClock("not a time", "UTC")).toBe(MISSING);
});

test("a time joins the date and the clock as the forecast email does", () => {
    expect(formatTime("en", "2026-11-26T13:02:00Z", "Europe/Istanbul")).toBe(
        "26 Nov 2026, 16:02",
    );
    expect(formatTime("tr", "2026-12-31T21:00:00Z", "Europe/Istanbul")).toBe(
        "1 Ocak 2027, 00:00",
    );
    expect(formatTime("en", new Date("2026-01-07T09:00:00Z"), "UTC")).toBe(
        "7 Jan 2026, 09:00",
    );
    expect(formatTime("en", undefined, "UTC")).toBe(MISSING);
});

test("a zoned time names the offset of its own instant", () => {
    expect(
        formatZonedTime("tr", "2026-11-26T13:02:00Z", "Europe/Istanbul"),
    ).toBe("26 Kasım 2026, 16:02 (UTC+3)");
    expect(formatZonedTime("en", "2026-03-29T00:30:00Z", "Europe/Berlin")).toBe(
        "29 Mar 2026, 01:30 (UTC+1)",
    );
    expect(formatZonedTime("en", "2026-03-29T01:30:00Z", "Europe/Berlin")).toBe(
        "29 Mar 2026, 03:30 (UTC+2)",
    );
    expect(formatZonedClock("2026-10-25T00:30:00Z", "Europe/Berlin")).toBe(
        "02:30 (UTC+2)",
    );
    expect(formatZonedClock("2026-10-25T01:30:00Z", "Europe/Berlin")).toBe(
        "02:30 (UTC+1)",
    );
    expect(formatZonedTime("en", null, "UTC")).toBe(MISSING);
    expect(formatZonedClock(null, "UTC")).toBe(MISSING);
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
    const steady = intervalClock("Europe/Istanbul", [
        ["2026-01-07T09:00:00Z", "2026-01-07T10:00:00Z"],
    ]);
    expect(steady("2026-01-07T09:00:00Z")).toBe("12:00");
    expect(steady("2026-01-07T10:00:00Z")).toBe("13:00");
    const shift = intervalClock("Europe/Berlin", [
        ["2026-03-29T00:00:00Z", "2026-03-29T00:30:00Z"],
        ["2026-03-29T01:30:00Z", "2026-03-29T02:00:00Z"],
    ]);
    expect(shift("2026-03-29T00:00:00Z")).toBe("01:00 (UTC+1)");
    expect(shift("2026-03-29T01:30:00Z")).toBe("03:30 (UTC+2)");
});

test("interval times name the offset when only the end of the last interval lies after the change", () => {
    const clock = intervalClock("Europe/Berlin", [
        ["2026-10-24T23:30:00Z", "2026-10-25T00:30:00Z"],
        ["2026-10-25T00:30:00Z", "2026-10-25T01:30:00Z"],
    ]);
    expect(clock("2026-10-25T00:30:00Z")).toBe("02:30 (UTC+2)");
    expect(clock("2026-10-25T01:30:00Z")).toBe("02:30 (UTC+1)");
});

test("the times of a zone's intervals reuse the formatters built for its first time", () => {
    const DateTimeFormat = Intl.DateTimeFormat;
    let built = 0;
    const counted = new Proxy(DateTimeFormat, {
        construct(target, args: ConstructorParameters<typeof DateTimeFormat>) {
            built += 1;
            return new target(...args);
        },
    });
    const hour = (index: number) =>
        new Date(Date.UTC(2026, 6, 7, index)).toISOString();
    Object.defineProperty(Intl, "DateTimeFormat", { value: counted });
    try {
        const bounds = Array.from(
            { length: 24 },
            (_, index) => [hour(index), hour(index + 1)] as const,
        );
        const clock = intervalClock("Asia/Tokyo", bounds);
        formatTime("tr", hour(0), "Asia/Tokyo");
        const first = built;
        for (const [start, end] of bounds) {
            clock(start);
            clock(end);
            formatTime("tr", start, "Asia/Tokyo");
        }
        expect(built).toBe(first);
    } finally {
        Object.defineProperty(Intl, "DateTimeFormat", {
            value: DateTimeFormat,
        });
    }
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
