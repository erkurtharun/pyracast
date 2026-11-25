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
    expect(shownChoice("auto", "estimated_actuals", true)).toBe(
        "estimated_actuals",
    );
    expect(shownChoice("auto", "forecast", true)).toBe("forecast");
    expect(shownChoice("auto", "mixed", false)).toBe("forecast");
    expect(shownChoice("auto", undefined, true)).toBe("estimated_actuals");
    expect(shownChoice("auto", undefined, false)).toBe("forecast");
    expect(shownChoice("archived_forecast", "forecast", true)).toBe(
        "archived_forecast",
    );
    expect(shownChoice("estimated_actuals", undefined, false)).toBe(
        "estimated_actuals",
    );
});

test("days shift across month and year boundaries", () => {
    expect(shiftDay("2026-12-31", 1)).toBe("2027-01-01");
    expect(shiftDay("2026-03-01", -1)).toBe("2026-02-28");
});
