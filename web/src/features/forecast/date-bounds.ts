import type { operations } from "@/lib/schema";

export type DataSource = NonNullable<
    operations["tenants_regions_forecast_retrieve"]["parameters"]["query"]["data_source"]
>;

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
        max: source === "forecast" ? shiftDay(today, OUTLOOK_DAYS) : today,
    };
}

export function clampDate(day: string, bounds: DateBounds): string {
    if (!day || day < bounds.min) return bounds.min;
    return day > bounds.max ? bounds.max : day;
}
