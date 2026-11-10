"use client";
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
};
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
    const result = forecast.result as Record<string, unknown>,
        allRows = (
            Array.isArray(result.intervals) ? result.intervals : []
        ) as Interval[];
    const metric = electrical ? "mean_power_mw" : "ghi_wm2",
        unit = electrical ? "MW" : "W/m²";
    const positiveRows = dataIntervalRange(
        allRows,
        (row) => typeof row[metric] === "number" && row[metric]! > 0,
    );
    const rows = positiveRows.length
        ? positiveRows
        : dataIntervalRange(allRows, (row) => row[metric] != null);
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
                        {electrical
                            ? "Electrical generation"
                            : "Regional irradiance"}
                    </h2>
                    <p>Local times in {forecast.timezone}</p>
                    {forecast.source_kind === "estimated_actuals" && (
                        <p>
                            Estimated actuals. Satellite/model estimates, not
                            meter readings.
                        </p>
                    )}
                </div>
                <div className="button-group">
                    <CsvDownload
                        filename={`${electrical ? "plant" : "region"}-${forecast.date}-${forecast.source_kind}`}
                        records={allRows}
                        metadata={{
                            ...forecast,
                            result: { ...result, intervals: undefined },
                        }}
                    />
                    <Tag>{forecast.freshness}</Tag>
                </div>
            </div>
            {electrical &&
                Array.isArray(result.quality_flags) &&
                result.quality_flags.includes(
                    "historical_configuration_assumed",
                ) && (
                    <Notice>
                        {forecast.source_kind === "estimated_actuals"
                            ? "Calculated from estimated actuals using the plant configuration entered later. No calibration is applied."
                            : "Calculated from the archived forecast using the plant configuration entered later. No calibration is applied."}
                    </Notice>
                )}
            {result.complete !== true && (
                <Notice>
                    Partial coverage. The complete daily total is unavailable;
                    the partial estimate only includes covered intervals.
                </Notice>
            )}
            <div className="stats forecast-stats">
                <Stat
                    label={electrical ? "Daily energy" : "Daily solar energy"}
                    value={result[electrical ? "energy_mwh" : "energy_kwh_m2"]}
                    unit={electrical ? "MWh" : "kWh/m²"}
                />
                <Stat
                    label="Partial estimate"
                    value={
                        result[
                            electrical
                                ? "partial_energy_mwh"
                                : "partial_energy_kwh_m2"
                        ]
                    }
                    unit={electrical ? "MWh" : "kWh/m²"}
                />
                <Stat
                    label="Coverage"
                    value={
                        typeof result.coverage_fraction === "number"
                            ? result.coverage_fraction * 100
                            : undefined
                    }
                    unit="%"
                />
            </div>
            {rows.length === 0 && (
                <p>
                    No interval values are available for this day. Missing
                    values are not zero.
                </p>
            )}
            {rows.length > 0 && (
                <>
                    {positiveRows.length > 0 &&
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
                    <div
                        className="chart"
                        role="img"
                        aria-label={`${electrical ? "Power" : "Irradiance"} by interval. Full values are available in the following table. Missing values are gaps.`}
                    >
                        {rows.map((row, index) => (
                            <div
                                className={`chart-column ${row[metric] == null ? "missing" : ""}`}
                                key={index}
                                title={`${time(row.start)}: ${number(row[metric])} ${unit}`}
                            >
                                <div
                                    style={{
                                        height: `${typeof row[metric] === "number" ? (row[metric]! / max) * 100 : 0}%`,
                                    }}
                                />
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
                        <div className="table-scroll">
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
                                                ? "Mean power (MW)"
                                                : "Irradiance (W/m²)"}
                                        </th>
                                        {electrical && <th>Energy (MWh)</th>}
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
                                            <td>
                                                {row.quality_flags?.join(
                                                    ", ",
                                                ) || "—"}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </details>
                </>
            )}
            <div className="provenance">
                <strong>Source details</strong>
                <p>
                    {forecast.collected_at
                        ? `Collected ${new Date(forecast.collected_at).toLocaleString("en", { timeZone: forecast.timezone })}. `
                        : ""}
                    Model: {String(result.model_version ?? "—")}.{" "}
                    {forecast.configuration_id
                        ? `Configuration ${forecast.configuration_id}. `
                        : ""}
                    {electrical && forecast.source_kind === "estimated_actuals"
                        ? "Baseline estimate using the current configuration; no calibration factor applied."
                        : electrical
                          ? forecast.calibration_id
                              ? `Calibration ${forecast.calibration_id}.`
                              : "Baseline (no active calibration)."
                          : ""}
                </p>
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
                        issued_at: forecast.issued_at,
                    }}
                    label="Source & configuration details"
                />
            </div>
        </>
    );
}
