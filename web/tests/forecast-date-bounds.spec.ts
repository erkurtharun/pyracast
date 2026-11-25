import { expect, test } from "@playwright/test";
import {
    clampDate,
    dateBounds,
    shiftDay,
    shownChoice,
    sourceOf,
} from "../src/features/forecast/date-bounds";

const today = "2026-11-20";

test("each source accepts the dates the forecast API accepts", () => {
    expect(dateBounds("archived_forecast", today)).toEqual({
        min: "2025-11-19",
        max: today,
    });
    expect(dateBounds("estimated_actuals", today)).toEqual({
        min: "2025-11-19",
        max: today,
    });
    expect(dateBounds("auto", today)).toEqual({
        min: "2025-11-19",
        max: "2026-12-04",
    });
});

test("a date outside the source's bounds moves to the nearest accepted day", () => {
    expect(
        clampDate("2026-11-21", dateBounds("archived_forecast", today)),
    ).toBe(today);
    expect(
        clampDate("2026-11-18", dateBounds("estimated_actuals", today)),
    ).toBe("2026-11-18");
    expect(clampDate("", dateBounds("auto", today))).toBe("2025-11-19");
});

test("the forecast choice reads the forecast, or satellite data for a past day once it has arrived", () => {
    expect(sourceOf("forecast")).toBe("auto");
    expect(sourceOf("estimated_actuals")).toBe("estimated_actuals");
    expect(sourceOf("archived_forecast")).toBe("archived_forecast");
});

test("the data choice names what the screen shows", () => {
    const result = { complete: true };
    expect(
        shownChoice("auto", { source_kind: "estimated_actuals", result }),
    ).toBe("estimated_actuals");
    expect(shownChoice("auto", { source_kind: "forecast", result })).toBe(
        "forecast",
    );
    expect(shownChoice("auto", { source_kind: "mixed", result })).toBe(
        "forecast",
    );
    expect(
        shownChoice("archived_forecast", { source_kind: "forecast", result }),
    ).toBe("archived_forecast");
});

test("the automatic choice names satellite data only while a satellite result is shown", () => {
    expect(shownChoice("auto", undefined)).toBe("forecast");
    expect(
        shownChoice("auto", { source_kind: "estimated_actuals", result: null }),
    ).toBe("forecast");
});

test("a chosen source keeps its name while its read is empty or failed", () => {
    for (const source of ["estimated_actuals", "archived_forecast"] as const) {
        expect(shownChoice(source, undefined)).toBe(source);
        expect(
            shownChoice(source, { source_kind: "forecast", result: null }),
        ).toBe(source);
    }
});

test("days shift across month and year boundaries", () => {
    expect(shiftDay("2026-12-31", 1)).toBe("2027-01-01");
    expect(shiftDay("2026-03-01", -1)).toBe("2026-02-28");
});
