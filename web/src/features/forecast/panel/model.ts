import { dataIntervalRange } from "@/lib/intervals";
import type { ForecastInterval as Interval } from "../forecast-intervals";

export type DaylightSummary = {
    status: "normal" | "polar_day" | "polar_night";
    complete: boolean;
    coverage_fraction: number | null;
    input_coverage_fraction?: number | null;
    energy_mwh?: number | null;
    energy_kwh_m2?: number | null;
    partial_energy_mwh?: number;
    partial_energy_kwh_m2?: number;
};

export type OutageScenario = {
    evaluated_at: string;
    status: "estimated" | "unknown_impact";
    incident_revision_ids: number[];
    incidents: Record<string, unknown>[];
    result: Record<string, unknown>;
};

export type ProductionDiagnostic = {
    status: "suspected" | "normal" | "insufficient";
    performance_ratio: number | null;
    reference_kind: "active_calibration" | "uncalibrated_reference";
    window_start: string | null;
    window_end: string | null;
    days: {
        day: string;
        performance_index: number;
        hours: number;
        status: "shortfall" | "within_threshold";
        observed_zero: boolean;
    }[];
    latest_matched_day: string | null;
    latest_day_status:
        "shortfall" | "within_threshold" | "insufficient" | "unavailable";
    latest_performance_index: number | null;
    evidence_state: "current" | "stale";
};

export type ForecastResult = Record<string, unknown>;

export function hasFlag(result: ForecastResult, flag: string) {
    return (
        Array.isArray(result.quality_flags) &&
        result.quality_flags.includes(flag)
    );
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

export function forecastCsvRows(rows: Interval[], scenario: boolean) {
    if (!scenario) return rows;
    return rows.map((row) => ({
        start: row.start,
        end: row.end,
        normal_mean_power_mw: row.mean_power_mw,
        normal_energy_mwh: row.energy_mwh,
        affected_mean_power_mw: row.affected_mean_power_mw,
        affected_energy_mwh: row.affected_energy_mwh,
        normal_quality_flags: row.quality_flags,
        affected_quality_flags: row.affected_quality_flags,
        source_snapshot_ids: row.source_snapshot_ids,
        ...(row.source_kinds ? { source_kinds: row.source_kinds } : {}),
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
