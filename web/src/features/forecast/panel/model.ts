import type { Forecast } from "@/lib/api";
import { dataIntervalRange } from "@/lib/intervals";
import type { components } from "@/lib/schema";
import type { ForecastInterval as Interval } from "../forecast-intervals";

type Schemas = components["schemas"];

export type OutageScenario = Schemas["OutageScenarioOutput"];
export type ProductionDiagnostic = Schemas["ProductionDiagnosticOutput"];
export type ForecastResult =
    Schemas["IrradianceResultOutput"] | Schemas["PlantForecastResultOutput"];

type Energy = Partial<Record<"energy_mwh" | "energy_kwh_m2", number | null>>;

// Generation reports MWh and irradiance kWh/m².
export function dayEnergy(summary: Energy | undefined, electrical: boolean) {
    return electrical ? summary?.energy_mwh : summary?.energy_kwh_m2;
}

export function hasFlag(result: ForecastResult, flag: string) {
    return result.quality_flags?.includes(flag) ?? false;
}

// The newest collection among the data the figures use, as the forecast email states it.
export function collectedAt(
    forecast: Pick<Forecast, "collected_at">,
    result: ForecastResult,
): string | undefined {
    const provenance = result.provenance;
    const times = [
        forecast.collected_at,
        provenance?.collected_at,
        ...(provenance?.source_snapshots ?? []).map(
            (source) => source.collected_at,
        ),
    ].filter(
        (time): time is string =>
            typeof time === "string" && Number.isFinite(Date.parse(time)),
    );
    return times.reduce<string | undefined>(
        (latest, time) =>
            latest === undefined || Date.parse(time) > Date.parse(latest)
                ? time
                : latest,
        undefined,
    );
}

export function hourly(result: ForecastResult, rows: Interval[]): boolean {
    if ("resolution_minutes" in result && result.resolution_minutes)
        return result.resolution_minutes >= 60;
    const steps = rows.map(
        (row) => Date.parse(row.end) - Date.parse(row.start),
    );
    return Math.min(...steps) >= 3_600_000;
}

export function fullDayIntervals(
    rows: Interval[],
    start: unknown,
    end: unknown,
): Interval[] {
    if (typeof start !== "string" || typeof end !== "string") return rows;
    let cursor = Date.parse(start);
    const stop = Date.parse(end);
    if (!Number.isFinite(cursor) || !Number.isFinite(stop) || stop <= cursor)
        return rows;
    const result: Interval[] = [];
    for (const row of rows) {
        const begin = Date.parse(row.start);
        if (begin > cursor)
            result.push({
                start: new Date(cursor).toISOString(),
                end: row.start,
                quality_flags: ["missing_source_interval"],
            });
        result.push(row);
        cursor = Math.max(cursor, Date.parse(row.end));
    }
    if (cursor < stop)
        result.push({
            start: new Date(cursor).toISOString(),
            end,
            quality_flags: ["missing_source_interval"],
        });
    return result;
}

// `affected` is null without an outage scenario; a scenario without intervals leaves every affected value missing.
export function scenarioIntervals(
    rows: Interval[],
    affected: Interval[] | null,
): Interval[] {
    if (!affected) return rows;
    const byStart = new Map(affected.map((row) => [row.start, row]));
    return rows.map((row) => ({
        ...row,
        affected_mean_power_mw: byStart.get(row.start)?.mean_power_mw ?? null,
        affected_energy_mwh: byStart.get(row.start)?.energy_mwh ?? null,
        affected_quality_flags: byStart.get(row.start)?.quality_flags,
    }));
}

export function chartIntervals(
    rows: Interval[],
    metric: "mean_power_mw" | "ghi_wm2",
    showFullDay: boolean,
    day: { start?: unknown; end?: unknown },
): Interval[] {
    if (showFullDay) return fullDayIntervals(rows, day.start, day.end);
    const positive = dataIntervalRange(
        rows,
        (row) => typeof row[metric] === "number" && row[metric] > 0,
    );
    return positive.length
        ? positive
        : dataIntervalRange(rows, (row) => row[metric] != null);
}
