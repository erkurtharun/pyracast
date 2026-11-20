import { expect, test } from "@playwright/test";
import {
    clampDate,
    dateBounds,
    shiftDay,
} from "../src/features/forecast/date-bounds";

const today = "2026-11-20";

test("each source accepts the dates the forecast API accepts", () => {
    expect(dateBounds("forecast", today)).toEqual({
        min: today,
        max: "2026-12-04",
    });
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
    expect(clampDate("2026-11-18", dateBounds("forecast", today))).toBe(today);
    expect(
        clampDate("2026-11-21", dateBounds("archived_forecast", today)),
    ).toBe(today);
    expect(
        clampDate("2026-11-18", dateBounds("estimated_actuals", today)),
    ).toBe("2026-11-18");
    expect(clampDate("", dateBounds("forecast", today))).toBe(today);
});

test("days shift across month and year boundaries", () => {
    expect(shiftDay("2026-12-31", 1)).toBe("2027-01-01");
    expect(shiftDay("2026-03-01", -1)).toBe("2026-02-28");
});
