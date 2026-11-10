"use client";
import type { Forecast } from "@/lib/api";
import { Empty, JsonDetails, Notice, Stat, Tag } from "./ui";
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
                <Empty title="No forecast for this day yet">
                    A missing forecast is not zero production. Try another date,
                    or check back after the next scheduled collection.
                </Empty>
            </>
        );
    const result = forecast.result as Record<string, unknown>,
        rows = (
            Array.isArray(result.intervals) ? result.intervals : []
        ) as Interval[];
    const metric = electrical ? "mean_power_mw" : "ghi_wm2",
        unit = electrical ? "MW" : "W/m²";
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
                </div>
                <Tag>{forecast.freshness}</Tag>
            </div>
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
            {rows.length > 0 && (
                <>
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
                                    Forecast intervals, {forecast.timezone}
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
                <strong>Behind this forecast</strong>
                <p>
                    {forecast.collected_at
                        ? `Collected ${new Date(forecast.collected_at).toLocaleString("en", { timeZone: forecast.timezone })}. `
                        : ""}
                    Model: {String(result.model_version ?? "—")}.{" "}
                    {forecast.configuration_id
                        ? `Configuration ${forecast.configuration_id}. `
                        : ""}
                    {electrical
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
