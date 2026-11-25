export type DataSource = "auto" | "estimated_actuals" | "archived_forecast";
export type DataChoice = "forecast" | "estimated_actuals" | "archived_forecast";

export type DateBounds = { min: string; max: string };

// The forecast API accepts one year back and fourteen days ahead, by source.
const HISTORY_DAYS = 366;
const OUTLOOK_DAYS = 14;

export function shiftDay(day: string, offset: number): string {
    const date = new Date(`${day}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + offset);
    return date.toISOString().slice(0, 10);
}

export function dateBounds(source: DataSource, today: string): DateBounds {
    return {
        min: shiftDay(today, -HISTORY_DAYS),
        max: source === "auto" ? shiftDay(today, OUTLOOK_DAYS) : today,
    };
}

export function clampDate(day: string, bounds: DateBounds): string {
    if (!day || day < bounds.min) return bounds.min;
    return day > bounds.max ? bounds.max : day;
}

// "auto" shows the forecast, or satellite data for a past day once it has arrived.
export function sourceOf(choice: DataChoice): DataSource {
    return choice === "forecast" ? "auto" : choice;
}

export function shownChoice(
    source: DataSource,
    shownKind: string | null | undefined,
    pastDay: boolean,
): DataChoice {
    if (source !== "auto") return source;
    const kind = shownKind ?? (pastDay ? "estimated_actuals" : "forecast");
    return kind === "estimated_actuals" ? "estimated_actuals" : "forecast";
}
