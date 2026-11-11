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
    source_snapshot_ids?: number[];
    affected_mean_power_mw?: number | null;
    affected_energy_mwh?: number | null;
    affected_quality_flags?: string[];
};
type OutageScenario = {
    evaluated_at: string;
    status: "estimated" | "unknown_impact";
    incident_revision_ids: number[];
    incidents: Record<string, unknown>[];
    result: Record<string, unknown>;
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
    const scenario = electrical
        ? (forecast as Forecast & { outage_scenario?: OutageScenario | null })
              .outage_scenario
        : null;
    const result = forecast.result as Record<string, unknown>;
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
                            result: { ...result, intervals: undefined },
                            ...(scenario
                                ? {
                                      outage_scenario: {
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
                            ? "Calculated from estimated actuals using the plant configuration entered later. No calibration is applied."
                            : "Calculated from the archived forecast using the plant configuration entered later. No calibration is applied."}
                    </Notice>
                )}
            {result.complete !== true && (
                <Notice>
                    Partial coverage. Daily total unavailable; the partial
                    estimate includes covered intervals only.
                </Notice>
            )}
            <div className="stats forecast-stats">
                <Stat
                    label={
                        scenario
                            ? "Normal capacity · daily energy"
                            : electrical
                              ? "Daily energy"
                              : "Daily solar energy"
                    }
                    value={result[electrical ? "energy_mwh" : "energy_kwh_m2"]}
                    unit={electrical ? "MWh" : "kWh/m²"}
                />
                {scenario && (
                    <Stat
                        label="With outages · daily energy"
                        value={scenario.result.energy_mwh}
                        unit="MWh"
                    />
                )}
                {scenario && scenario.result.complete !== true && (
                    <Stat
                        label="With outages · partial estimate"
                        value={scenario.result.partial_energy_mwh}
                        unit="MWh"
                    />
                )}
                {result.complete !== true && (
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
                )}
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
                    {scenario && (
                        <p className="fine">
                            Green: normal capacity · Blue: with outages · Gaps:
                            unknown impact
                        </p>
                    )}
                    <div
                        className="chart"
                        role="img"
                        aria-label={`${scenario ? "Normal capacity and with outages power" : electrical ? "Power" : "Irradiance"} by interval. Full values are available in the following table. Missing values are gaps.`}
                    >
                        {rows.map((row, index) => (
                            <div
                                className={`chart-column ${row[metric] == null ? "missing" : ""}`}
                                key={index}
                                title={`${time(row.start)}: ${scenario ? "normal capacity " : ""}${number(row[metric])} ${unit}${scenario ? `; with outages ${number(row.affected_mean_power_mw)} MW` : ""}`}
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
                        <div className="table-scroll" tabIndex={0}>
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
                                        {scenario && <th>With outages (MW)</th>}
                                        {scenario && (
                                            <th>With outages energy (MWh)</th>
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
                                                    ? `; with outages: ${row.affected_quality_flags.map((flag) => flag.replaceAll("_", " ")).join(", ")}`
                                                    : ""}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </details>
                </>
            )}
            <details className="provenance">
                <summary>Source & quality</summary>
                {scenario && (
                    <p className="fine">
                        Normal capacity assumes a healthy plant. With outages
                        uses reported remaining capacity. Both are estimates,
                        not meter readings.
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
                    {electrical && forecast.source_kind === "estimated_actuals"
                        ? "Baseline estimate using the current configuration; no calibration factor applied."
                        : electrical
                          ? forecast.calibration_id
                              ? `Calibration ${forecast.calibration_id}.`
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
                        issued_at: forecast.issued_at,
                    }}
                    label="Source & configuration details"
                />
            </details>
        </>
    );
}
