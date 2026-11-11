"use client";
import { ScrollRegion } from "./scroll-region";

import { useState, type FormEvent } from "react";
import type { components } from "@/lib/schema";
import { localDay } from "./data-source";
import { Notice, Stat, useResource } from "./ui";
import { CsvDownload } from "./csv-download";

type Performance = components["schemas"]["PerformanceOutput"];

const reasons: Record<string, string> = {
    configuration_not_available:
        "Configuration was not available before this day",
    forecast_not_available: "Day-ahead forecast was not available",
    incomplete_or_invalid_day:
        "Production or forecast coverage is incomplete or invalid",
    invalid_forecast_data: "Archived forecast data is invalid",
};
const format = (value: number | null) =>
    value === null
        ? "—"
        : new Intl.NumberFormat("en", { maximumFractionDigits: 3 }).format(
              value,
          );

export function ForecastPerformance({
    base,
    timezone,
}: {
    base: string;
    timezone: string;
}) {
    const [start, setStart] = useState(() => localDay(timezone, -30));
    const [end, setEnd] = useState(() => localDay(timezone, -1));
    const [url, setUrl] = useState(`${base}performance/?daylight_only=true`);
    function load(event: FormEvent) {
        event.preventDefault();
        setUrl(
            `${base}performance/?${new URLSearchParams({ start_date: start, end_date: end, daylight_only: "true" })}`,
        );
    }
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>Forecast performance</h2>
                    <p className="fine">
                        Day-ahead replay vs measured daylight production ·{" "}
                        {timezone}
                    </p>
                </div>
            </div>
            <details>
                <summary>How performance is measured</summary>
                <p>
                    Historical replay using archived day-ahead forecasts and the
                    configuration and calibration available before each day.
                    This does not establish that the plant prediction was
                    published at the time.
                </p>
                <p>
                    Results use measured intervals overlapping daylight, not
                    estimated actuals or full-day totals. The complete daylight
                    window must be covered. Dates are local to {timezone};
                    choose up to 90 completed days.
                </p>
            </details>
            <form className="forecast-controls" onSubmit={load}>
                <label className="field">
                    Start date
                    <input
                        type="date"
                        required
                        value={start}
                        max={end}
                        onChange={(event) => setStart(event.target.value)}
                    />
                </label>
                <label className="field">
                    End date
                    <input
                        type="date"
                        required
                        value={end}
                        max={localDay(timezone, -1)}
                        onChange={(event) => setEnd(event.target.value)}
                    />
                </label>
                <button className="button secondary" type="submit">
                    Load performance
                </button>
            </form>
            <PerformanceResults key={url} url={url} timezone={timezone} />
        </section>
    );
}

function PerformanceResults({
    url,
    timezone,
}: {
    url: string;
    timezone: string;
}) {
    const resource = useResource<Performance>(url);
    return (
        <>
            {resource.loading ? (
                <p role="status">Loading performance…</p>
            ) : resource.error ? (
                <Notice error>{resource.error}</Notice>
            ) : (
                resource.data && (
                    <>
                        <div className="toolbar">
                            <CsvDownload
                                filename={`performance-${resource.data.start_date}-${resource.data.end_date}`}
                                records={resource.data.days}
                                metadata={{
                                    ...resource.data,
                                    days: undefined,
                                    timezone,
                                }}
                            />
                        </div>
                        <div className="stats">
                            <Stat
                                label="Matched days"
                                value={resource.data.matched_days}
                            />
                            <Stat
                                label="Missing days"
                                value={
                                    resource.data.requested_days -
                                    resource.data.matched_days
                                }
                            />
                            <Stat
                                label="Daylight energy error"
                                value={resource.data.metrics?.daily_mae_mwh}
                                unit="MWh"
                            />
                            <Stat
                                label="Daylight energy bias"
                                value={resource.data.metrics?.daily_bias_mwh}
                                unit="MWh"
                            />
                        </div>
                        <details>
                            <summary>
                                Additional metrics & interpretation
                            </summary>
                            <div className="stats">
                                <Stat
                                    label="Baseline daylight energy error"
                                    value={
                                        resource.data.baseline_metrics
                                            ?.daily_mae_mwh
                                    }
                                    unit="MWh"
                                />
                                <Stat
                                    label="Generating-interval error"
                                    value={
                                        resource.data.metrics
                                            ?.source_positive_mae_mw
                                    }
                                    unit="MW"
                                />
                            </div>
                            <p className="fine">
                                Bias is predicted minus measured. Daily energy
                                error is mean absolute error.
                                Generating-interval error is mean absolute error
                                in intervals with positive forecast input.
                                Uncertainty is not estimated.
                            </p>
                        </details>
                        {!resource.data.metrics && (
                            <Notice>
                                No matched days are available for this range.
                            </Notice>
                        )}
                        <details>
                            <summary>
                                Daily comparison ({resource.data.requested_days}{" "}
                                days)
                            </summary>
                            <ScrollRegion label="Daily forecast comparison">
                                <table>
                                    <caption>
                                        Daylight replay:{" "}
                                        {resource.data.start_date} to{" "}
                                        {resource.data.end_date}
                                    </caption>
                                    <thead>
                                        <tr>
                                            <th>Date</th>
                                            <th>Status</th>
                                            <th>Measured (MWh)</th>
                                            <th>Predicted (MWh)</th>
                                            <th>Error (MWh)</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {resource.data.days.map((day) => (
                                            <tr key={day.date}>
                                                <td>{day.date}</td>
                                                <td>
                                                    {day.status === "matched"
                                                        ? "Matched"
                                                        : (day.reason &&
                                                              (reasons[
                                                                  day.reason
                                                              ] ??
                                                                  day.reason.replaceAll(
                                                                      "_",
                                                                      " ",
                                                                  ))) ||
                                                          "Missing"}
                                                </td>
                                                <td>
                                                    {format(
                                                        day.measured_energy_mwh,
                                                    )}
                                                </td>
                                                <td>
                                                    {format(
                                                        day.predicted_energy_mwh,
                                                    )}
                                                </td>
                                                <td>{format(day.error_mwh)}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </ScrollRegion>
                        </details>
                    </>
                )
            )}
        </>
    );
}
