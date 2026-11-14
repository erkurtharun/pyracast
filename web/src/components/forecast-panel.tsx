"use client";
import { useState } from "react";
import { ScrollRegion } from "./scroll-region";
import { WeatherReferenceCard } from "./weather-reference";
import type { Forecast } from "@/lib/api";
import { Empty, JsonDetails, Notice, Stat, Tag } from "./ui";
import { CsvDownload } from "./csv-download";
import { dataIntervalRange } from "@/lib/intervals";
type Interval = {
    start: string;
    end: string;
    ghi_wm2?: number | null;
    mean_power_mw?: number | null;
    energy_mwh?: number | null;
    quality_flags?: string[];
    source_snapshot_ids?: number[];
    affected_mean_power_mw?: number | null;
    affected_energy_mwh?: number | null;
    affected_quality_flags?: string[];
};
type DaylightSummary = {
    status: "normal" | "polar_day" | "polar_night";
    complete: boolean;
    coverage_fraction: number | null;
    energy_mwh?: number | null;
    energy_kwh_m2?: number | null;
    partial_energy_mwh?: number;
    partial_energy_kwh_m2?: number;
};
type OutageScenario = {
    evaluated_at: string;
    status: "estimated" | "unknown_impact";
    incident_revision_ids: number[];
    incidents: Record<string, unknown>[];
    result: Record<string, unknown>;
};
type DeratingScenario = {
    evaluated_at: string;
    status: "suspected";
    performance_ratio: number;
    window_start: string;
    window_end: string;
    days?: { day: string; performance_index: number; hours: number }[];
    result: Record<string, unknown>;
};
function fullDayIntervals(
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
function number(value: unknown): string {
    return typeof value === "number"
        ? new Intl.NumberFormat("en", { maximumFractionDigits: 3 }).format(
              value,
          )
        : "—";
}
export function ForecastPanel({
    forecast,
    electrical = false,
}: {
    forecast: Forecast;
    electrical?: boolean;
}) {
    const [showFullDay, setShowFullDay] = useState(false);
    if (
        !forecast.result ||
        typeof forecast.result !== "object" ||
        Array.isArray(forecast.result)
    )
        return (
            <>
                <Tag>{forecast.freshness}</Tag>
                {forecast.unavailable_reason ===
                "estimated_actuals_unavailable" ? (
                    <Empty title="No estimated actuals for this day">
                        Estimated actuals are not available for this day yet.
                        Try an archived forecast or another date.
                    </Empty>
                ) : electrical &&
                  forecast.unavailable_reason ===
                      "configuration_unavailable" ? (
                    <Empty title="No configuration for this day">
                        No configuration existed before the selected day began.
                        Choose a later date.
                    </Empty>
                ) : (
                    <Empty title="No forecast for this day yet">
                        A missing forecast is not zero production. Try another
                        date, or check back after the next scheduled collection.
                    </Empty>
                )}
            </>
        );
    const scenarios = forecast as Forecast & {
        outage_scenario?: OutageScenario | null;
        derating_scenario?: DeratingScenario | null;
    };
    const outage = electrical ? scenarios.outage_scenario : null;
    const derating = electrical && !outage ? scenarios.derating_scenario : null;
    const scenario = outage ?? derating ?? null;
    const affected = derating ? "if derating persists" : "with outages";
    const Affected = derating ? "If derating persists" : "With outages";
    const result = forecast.result as Record<string, unknown>;
    const daylight = result.daylight as DaylightSummary | undefined;
    const primary = daylight ?? result;
    const affectedDaylight = scenario?.result.daylight as
        DaylightSummary | undefined;
    const affectedSummary = affectedDaylight ?? scenario?.result;
    const noDaylight = daylight?.status === "polar_night";
    const energyField = electrical ? "energy_mwh" : "energy_kwh_m2";
    const partialEnergyField = electrical
        ? "partial_energy_mwh"
        : "partial_energy_kwh_m2";
    const dayHours =
        typeof result.start === "string" && typeof result.end === "string"
            ? (Date.parse(result.end) - Date.parse(result.start)) / 3_600_000
            : Number.NaN;
    const coveredHours =
        typeof result.coverage_fraction === "number"
            ? result.coverage_fraction * dayHours
            : Number.NaN;
    const deratingDays = derating?.days?.map((day) => day.day).sort() ?? [];
    const mixedVintages =
        Array.isArray(result.quality_flags) &&
        result.quality_flags.includes("mixed_forecast_vintages");
    const provenance = result.provenance as
        | { source_snapshots?: { snapshot_id: number; collected_at: string }[] }
        | undefined;
    const affectedRows = (
        scenario && Array.isArray(scenario.result.intervals)
            ? scenario.result.intervals
            : []
    ) as Interval[];
    const affectedByStart = new Map(
        affectedRows.map((row) => [row.start, row]),
    );
    const allRows = (
        Array.isArray(result.intervals) ? result.intervals : []
    ).map((row: Interval) =>
        scenario
            ? {
                  ...row,
                  affected_mean_power_mw:
                      affectedByStart.get(row.start)?.mean_power_mw ?? null,
                  affected_energy_mwh:
                      affectedByStart.get(row.start)?.energy_mwh ?? null,
                  affected_quality_flags: affectedByStart.get(row.start)
                      ?.quality_flags,
              }
            : row,
    ) as Interval[];
    const csvRows = scenario
        ? allRows.map((row) => ({
              start: row.start,
              end: row.end,
              normal_mean_power_mw: row.mean_power_mw,
              normal_energy_mwh: row.energy_mwh,
              affected_mean_power_mw: row.affected_mean_power_mw,
              affected_energy_mwh: row.affected_energy_mwh,
              normal_quality_flags: row.quality_flags,
              affected_quality_flags: row.affected_quality_flags,
              source_snapshot_ids: row.source_snapshot_ids,
          }))
        : allRows;
    const metric = electrical ? "mean_power_mw" : "ghi_wm2",
        unit = electrical ? "MW" : "W/m²";
    const positiveRows = dataIntervalRange(
        allRows,
        (row) => typeof row[metric] === "number" && row[metric]! > 0,
    );
    const generationRows = positiveRows.length
        ? positiveRows
        : dataIntervalRange(allRows, (row) => row[metric] != null);
    const rows = showFullDay
        ? fullDayIntervals(allRows, result.start, result.end)
        : generationRows;
    const max = Math.max(
        1,
        ...rows.map((row) =>
            typeof row[metric] === "number" ? row[metric]! : 0,
        ),
    );
    const time = (value: string) =>
        new Intl.DateTimeFormat("en", {
            timeZone: forecast.timezone,
            hour: "2-digit",
            minute: "2-digit",
            hourCycle: "h23",
        }).format(new Date(value));
    return (
        <>
            <div className="section-heading">
                <div>
                    <h2>
                        {electrical ? "Electrical generation" : "Irradiance"}
                    </h2>
                    <p className="fine">
                        {forecast.date} · {forecast.timezone}
                    </p>
                    {forecast.source_kind === "estimated_actuals" && (
                        <p className="fine">
                            Satellite/model estimates, not meter readings.
                        </p>
                    )}
                    {mixedVintages && (
                        <p className="fine">
                            Includes intervals from earlier forecast updates.
                        </p>
                    )}
                </div>
                <div className="button-group">
                    <CsvDownload
                        filename={`${electrical ? "plant" : "region"}-${forecast.date}-${forecast.source_kind}`}
                        label={
                            scenario
                                ? "Download both scenarios CSV"
                                : "Download CSV"
                        }
                        records={csvRows}
                        metadata={{
                            ...forecast,
                            result: {
                                ...result,
                                intervals: undefined,
                                weather_reference: undefined,
                            },
                            ...(scenario
                                ? {
                                      [derating
                                          ? "derating_scenario"
                                          : "outage_scenario"]: {
                                          ...scenario,
                                          result: {
                                              ...scenario.result,
                                              intervals: undefined,
                                          },
                                      },
                                  }
                                : {}),
                        }}
                    />
                    <Tag>{forecast.freshness}</Tag>
                    {Array.isArray(result.quality_flags) &&
                        result.quality_flags.includes(
                            "provisional_production_calibration",
                        ) && (
                            <Tag tone="warning">
                                Provisional production calibration
                            </Tag>
                        )}
                    {Array.isArray(result.quality_flags) &&
                        result.quality_flags.includes(
                            "unvalidated_reference_transfer",
                        ) && <Tag tone="warning">Needs validation</Tag>}
                </div>
            </div>
            {!electrical && result.weather_reference != null && (
                <WeatherReferenceCard
                    reference={
                        result.weather_reference as Parameters<
                            typeof WeatherReferenceCard
                        >[0]["reference"]
                    }
                />
            )}
            {electrical &&
                Array.isArray(result.quality_flags) &&
                result.quality_flags.includes("archived_forecast") && (
                    <Notice>
                        Saved day-ahead forecast using configuration{" "}
                        {forecast.configuration_id ?? "—"}. This archive is
                        separate from the current outlook and metered
                        production.
                    </Notice>
                )}
            {derating && (
                <Notice>
                    {deratingDays.length > 0
                        ? `Across ${deratingDays.length} qualifying days from ${deratingDays[0]} to ${deratingDays[deratingDays.length - 1]}, `
                        : `In the assessment window from ${derating.window_start} to ${derating.window_end}, `}
                    the median daily production/reference ratio was{" "}
                    {number(
                        Math.round((1 - derating.performance_ratio) * 1000) /
                            10,
                    )}
                    % below the calibration estimate. This comparison does not
                    confirm an outage. The second scenario assumes this ratio
                    persists; normal capacity uses the calibration estimate.
                    Record an outage only if independently confirmed.
                </Notice>
            )}
            {scenario?.status === "unknown_impact" && (
                <Notice>
                    Capacity impact is unknown for some intervals; missing
                    affected values are not zero.
                </Notice>
            )}
            {scenario &&
                ((Array.isArray(scenario.result.quality_flags) &&
                    scenario.result.quality_flags.includes(
                        "outage_configuration_changed",
                    )) ||
                    affectedRows.some((row) =>
                        row.quality_flags?.includes(
                            "outage_configuration_changed",
                        ),
                    )) && (
                    <Notice>
                        Configuration changed. Confirm remaining capacities in
                        Outages to restore the affected estimate.
                    </Notice>
                )}
            {electrical &&
                Array.isArray(result.quality_flags) &&
                result.quality_flags.includes(
                    "historical_configuration_assumed",
                ) && (
                    <Notice>
                        {forecast.source_kind === "estimated_actuals"
                            ? forecast.calibration_id
                                ? "Calculated from estimated actuals using the plant configuration entered later and the calibration fitted for that period."
                                : "Calculated from estimated actuals using the plant configuration entered later. No calibration is applied."
                            : "Calculated from the archived forecast using the plant configuration entered later. No calibration is applied."}
                    </Notice>
                )}
            {daylight &&
                (noDaylight ? (
                    <p className="fine">
                        No daylight on this local date. Daylight energy is
                        unavailable.
                    </p>
                ) : !daylight.complete ? (
                    <Notice>
                        {daylight.coverage_fraction === 1
                            ? "Partial estimate."
                            : "Partial coverage."}{" "}
                        Some required {electrical ? "production" : "irradiance"}{" "}
                        intervals have missing values. The total is unavailable;
                        the partial estimate includes valid intervals only.
                        Missing values are not zero.
                        {!electrical &&
                            Array.isArray(result.quality_flags) &&
                            result.quality_flags.includes(
                                "ambiguous_zero_power",
                            ) && (
                                <>
                                    {" "}
                                    Zero reference power remains unknown
                                    irradiance unless nighttime is confirmed.
                                </>
                            )}
                    </Notice>
                ) : null)}
            {!daylight && result.complete !== true && (
                <Notice>
                    Partial coverage.
                    {Number.isFinite(coveredHours) && Number.isFinite(dayHours)
                        ? ` ${number(coveredHours)} of ${number(dayHours)} hours have valid values.`
                        : ""}{" "}
                    Daily total unavailable: it requires the entire local day,
                    including nighttime. The partial estimate includes valid
                    intervals only; missing values are not zero.
                    {!electrical &&
                        typeof result.coverage_seconds === "number" &&
                        Number.isFinite(dayHours) && (
                            <>
                                {" "}
                                Source intervals cover{" "}
                                {number(result.coverage_seconds / 3600)} of{" "}
                                {number(dayHours)} hours; irradiance validity is
                                separate from source availability.
                            </>
                        )}
                    {!electrical &&
                        Array.isArray(result.quality_flags) &&
                        result.quality_flags.includes(
                            "ambiguous_zero_power",
                        ) && (
                            <>
                                {" "}
                                Zero reference power cannot identify irradiance
                                without a confirmed nighttime classification.
                            </>
                        )}
                </Notice>
            )}
            <div className="stats forecast-stats">
                <Stat
                    label={
                        scenario
                            ? `Normal capacity · ${daylight ? "daylight" : "daily"} energy`
                            : daylight
                              ? "Daylight energy"
                              : electrical
                                ? "Daily energy"
                                : "Daily solar energy"
                    }
                    value={primary[energyField]}
                    unit={electrical ? "MWh" : "kWh/m²"}
                />
                {scenario && (
                    <Stat
                        label={`${Affected} · ${affectedDaylight ? "daylight" : "daily"} energy`}
                        value={affectedSummary?.energy_mwh}
                        unit="MWh"
                    />
                )}
                {scenario &&
                    affectedSummary?.complete !== true &&
                    affectedDaylight?.status !== "polar_night" && (
                        <Stat
                            label={`${Affected} · ${affectedDaylight ? "daylight partial" : "partial"} estimate`}
                            value={affectedSummary?.partial_energy_mwh}
                            unit="MWh"
                        />
                    )}
                {primary.complete !== true && !noDaylight && (
                    <Stat
                        label={
                            daylight
                                ? "Daylight partial estimate"
                                : "Partial estimate"
                        }
                        value={primary[partialEnergyField]}
                        unit={electrical ? "MWh" : "kWh/m²"}
                    />
                )}
                <Stat
                    label="Coverage"
                    value={
                        typeof primary.coverage_fraction === "number"
                            ? primary.coverage_fraction * 100
                            : undefined
                    }
                    unit="%"
                />
            </div>
            <label className="field">
                <span>
                    <input
                        type="checkbox"
                        checked={showFullDay}
                        onChange={(event) =>
                            setShowFullDay(event.target.checked)
                        }
                    />{" "}
                    Show full day, including missing intervals
                </span>
            </label>
            {rows.length === 0 && (
                <p>
                    No interval values are available for this day. Missing
                    values are not zero.
                </p>
            )}
            {rows.length > 0 && (
                <>
                    {!showFullDay &&
                        positiveRows.length > 0 &&
                        rows.length < allRows.length && (
                            <p className="fine">
                                Showing the first through last generating
                                interval, including gaps. CSV includes all
                                intervals.
                            </p>
                        )}
                    {!positiveRows.length &&
                        rows.every(
                            (row) => row[metric] == null || row[metric] === 0,
                        ) && (
                            <p className="fine">
                                All available intervals have zero generation.
                            </p>
                        )}
                    <div className="chart-title">
                        <strong>
                            {electrical
                                ? "Power across the day"
                                : "Irradiance across the day"}
                        </strong>
                        <span>{unit}</span>
                    </div>
                    {scenario && (
                        <p className="fine">
                            Green: normal capacity · Blue: {affected} · Gaps:
                            unknown impact
                        </p>
                    )}
                    <div
                        className="chart"
                        role="img"
                        aria-label={`${scenario ? `Normal capacity and ${affected} power` : electrical ? "Power" : "Irradiance"} by interval. Full values are available in the following table. Missing values are gaps.`}
                    >
                        {rows.map((row, index) => (
                            <div
                                className={`chart-column ${row[metric] == null ? "missing" : ""}`}
                                key={index}
                                style={{
                                    flexGrow:
                                        Date.parse(row.end) -
                                        Date.parse(row.start),
                                }}
                                title={`${time(row.start)}: ${scenario ? "normal capacity " : ""}${number(row[metric])} ${unit}${scenario ? `; ${affected} ${number(row.affected_mean_power_mw)} MW` : ""}`}
                            >
                                <div
                                    style={{
                                        height: `${typeof row[metric] === "number" ? (row[metric]! / max) * 100 : 0}%`,
                                    }}
                                />
                                {scenario && (
                                    <div
                                        style={{
                                            height: `${typeof row.affected_mean_power_mw === "number" ? (row.affected_mean_power_mw / max) * 100 : 0}%`,
                                            background: "#3874a1",
                                        }}
                                    />
                                )}
                            </div>
                        ))}
                    </div>
                    <div className="chart-axis">
                        <span>{time(rows[0].start)}</span>
                        <span>Local time · {forecast.timezone}</span>
                        <span>{time(rows[rows.length - 1].end)}</span>
                    </div>
                    <details className="interval-details">
                        <summary>
                            View interval data ({rows.length} intervals)
                        </summary>
                        <ScrollRegion
                            label={
                                forecast.source_kind === "estimated_actuals"
                                    ? "Estimated actual intervals"
                                    : "Forecast intervals"
                            }
                        >
                            <table>
                                <caption className="sr-only">
                                    {forecast.source_kind ===
                                    "estimated_actuals"
                                        ? "Estimated actual intervals"
                                        : "Forecast intervals"}
                                    , {forecast.timezone}
                                </caption>
                                <thead>
                                    <tr>
                                        <th>Start</th>
                                        <th>End</th>
                                        <th>
                                            {electrical
                                                ? scenario
                                                    ? "Normal capacity (MW)"
                                                    : "Mean power (MW)"
                                                : "Irradiance (W/m²)"}
                                        </th>
                                        {electrical && (
                                            <th>
                                                {scenario
                                                    ? "Normal capacity energy (MWh)"
                                                    : "Energy (MWh)"}
                                            </th>
                                        )}
                                        {scenario && <th>{Affected} (MW)</th>}
                                        {scenario && (
                                            <th>{Affected} energy (MWh)</th>
                                        )}
                                        <th>Quality</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {rows.map((row, index) => (
                                        <tr key={index}>
                                            <td>{time(row.start)}</td>
                                            <td>{time(row.end)}</td>
                                            <td>{number(row[metric])}</td>
                                            {electrical && (
                                                <td>
                                                    {number(row.energy_mwh)}
                                                </td>
                                            )}
                                            {scenario && (
                                                <td>
                                                    {number(
                                                        row.affected_mean_power_mw,
                                                    )}
                                                </td>
                                            )}
                                            {scenario && (
                                                <td>
                                                    {number(
                                                        row.affected_energy_mwh,
                                                    )}
                                                </td>
                                            )}
                                            <td>
                                                {row.quality_flags
                                                    ?.map((flag) =>
                                                        flag.replaceAll(
                                                            "_",
                                                            " ",
                                                        ),
                                                    )
                                                    .join(", ") || "—"}
                                                {scenario &&
                                                row.affected_quality_flags
                                                    ?.length
                                                    ? `; ${affected}: ${row.affected_quality_flags.map((flag) => flag.replaceAll("_", " ")).join(", ")}`
                                                    : ""}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </ScrollRegion>
                    </details>
                </>
            )}
            <details className="provenance">
                <summary>Source & quality</summary>
                {daylight && (
                    <>
                        <p>
                            Daylight energy includes whole source intervals that
                            overlap daylight. Sunrise and sunset boundary
                            intervals are kept intact.
                        </p>
                        <p>
                            Full-day coverage:
                            {` ${number(typeof result.coverage_fraction === "number" ? result.coverage_fraction * 100 : undefined)}%.`}
                            {Number.isFinite(coveredHours) &&
                            Number.isFinite(dayHours)
                                ? ` ${number(coveredHours)} of ${number(dayHours)} hours have valid values.`
                                : ""}{" "}
                            {result.complete === true
                                ? `Full-day energy: ${number(result[energyField])} ${electrical ? "MWh" : "kWh/m²"}.`
                                : `Full-day total unavailable. Available partial energy: ${number(result[partialEnergyField])} ${electrical ? "MWh" : "kWh/m²"}.`}
                        </p>
                        {!electrical &&
                            typeof result.coverage_seconds === "number" && (
                                <p>
                                    Source intervals cover{" "}
                                    {number(result.coverage_seconds / 3600)}{" "}
                                    hours. Zero reference power cannot identify
                                    irradiance without a confirmed nighttime
                                    classification.
                                </p>
                            )}
                    </>
                )}
                {scenario && (
                    <p className="fine">
                        Normal capacity uses configured capacity and the
                        selected calibration.{" "}
                        {derating
                            ? "If derating persists scales capacity by the recent measured shortfall."
                            : "With outages uses reported remaining capacity."}{" "}
                        Both are estimates, not meter readings.
                    </p>
                )}
                <p>
                    {forecast.collected_at
                        ? `Collected ${new Date(forecast.collected_at).toLocaleString("en", { timeZone: forecast.timezone })}. `
                        : ""}
                    Model: {String(result.model_version ?? "—")}.{" "}
                    {forecast.configuration_id
                        ? `Configuration ${forecast.configuration_id}. `
                        : ""}
                    {electrical &&
                    forecast.source_kind === "estimated_actuals" &&
                    !forecast.calibration_id
                        ? "Baseline estimate using the current configuration; no calibration factor applied."
                        : electrical
                          ? forecast.calibration_id
                              ? forecast.source_kind === "estimated_actuals"
                                  ? `Calibration ${forecast.calibration_id}, applied retrospectively.`
                                  : `Calibration ${forecast.calibration_id}.`
                              : "Baseline (no active calibration)."
                          : ""}
                </p>
                {mixedVintages &&
                    Array.isArray(provenance?.source_snapshots) && (
                        <ul>
                            {provenance.source_snapshots.map((source) => (
                                <li key={source.snapshot_id}>
                                    Forecast update {source.snapshot_id}:
                                    collected{" "}
                                    {new Date(
                                        source.collected_at,
                                    ).toLocaleString("en", {
                                        timeZone: forecast.timezone,
                                    })}
                                </li>
                            ))}
                        </ul>
                    )}
                {Array.isArray(result.quality_flags) &&
                    result.quality_flags.length > 0 && (
                        <div className="tags">
                            {result.quality_flags.map((flag, index) => (
                                <Tag key={index}>
                                    {String(flag).replaceAll("_", " ")}
                                </Tag>
                            ))}
                        </div>
                    )}
                <JsonDetails
                    data={{
                        vintage: result.vintage,
                        provenance: result.provenance,
                        configuration: result.configuration,
                        daylight: result.daylight,
                        issued_at: forecast.issued_at,
                    }}
                    label="Source & configuration details"
                />
            </details>
        </>
    );
}
