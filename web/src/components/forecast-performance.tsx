"use client";
import { ScrollRegion } from "./scroll-region";

import { useState, type FormEvent } from "react";
import type { components } from "@/lib/schema";
import { localDay } from "./data-source";
import { Notice, Stat, useResource } from "./ui";
import { CsvDownload } from "./csv-download";

type Performance = components["schemas"]["PerformanceOutput"];

const reasons: Record<string, string> = {
    estimated_actuals_not_available:
        "Historical weather estimates are not available",
    configuration_not_available:
        "Configuration was not available before this day",
    forecast_not_available: "Day-ahead forecast was not available",
    incomplete_or_invalid_day:
        "Production or reference coverage is incomplete or invalid",
    invalid_forecast_data: "Saved reference data is invalid",
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
    const [comparison, setComparison] = useState("historical_model");
    const [url, setUrl] = useState(
        `${base}performance/?daylight_only=true&comparison=historical_model`,
    );
    function load(event: FormEvent) {
        event.preventDefault();
        setUrl(
            `${base}performance/?${new URLSearchParams({ start_date: start, end_date: end, daylight_only: "true", comparison })}`,
        );
    }
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>Production comparison</h2>
                    <p className="fine">
                        Historical model or archived forecast vs daylight
                        production · {timezone}
                    </p>
                </div>
            </div>
            <details>
                <summary>How comparison is measured</summary>
                <p>
                    Historical model comparison uses saved historical weather
                    estimates with the current plant configuration and factor 1.
                    It assumes that configuration for past dates and can include
                    provisional reported production. It does not measure
                    forecast accuracy or establish the production meter
                    boundary.
                </p>
                <p>
                    Archived forecast performance uses day-ahead forecasts and
                    the configuration and calibration available before each day.
                    This does not establish that the plant prediction was
                    published at the time.
                </p>
                <p>
                    Results compare production intervals overlapping daylight.
                    Whole boundary intervals retain their energy. The complete
                    daylight window must be covered. Dates are local to{" "}
                    {timezone}; choose up to 90 completed days.
                </p>
            </details>
            <form className="forecast-controls" onSubmit={load}>
                <label className="field">
                    Comparison
                    <select
                        value={comparison}
                        onChange={(event) => setComparison(event.target.value)}
                    >
                        <option value="historical_model">
                            Historical model
                        </option>
                        <option value="archived_forecast">
                            Archived forecast performance
                        </option>
                    </select>
                </label>
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
    const matched =
        resource.data?.days.filter((day) => day.status === "matched") ?? [];
    const historical = resource.data?.comparison === "historical_model";
    const missingReasons = new Map<string, number>();
    for (const day of resource.data?.days ?? []) {
        if (day.status === "matched") continue;
        const reason =
            (day.reason && reasons[day.reason]) ||
            "Production or reference data is unavailable";
        missingReasons.set(reason, (missingReasons.get(reason) ?? 0) + 1);
    }
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
                        {resource.data.metrics && (
                            <>
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
                                        value={
                                            resource.data.metrics?.daily_mae_mwh
                                        }
                                        unit="MWh"
                                    />
                                    <Stat
                                        label="Daylight energy bias"
                                        value={
                                            resource.data.metrics
                                                ?.daily_bias_mwh
                                        }
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
                                        Bias is predicted minus measured. Daily
                                        energy error is mean absolute error.
                                        Generating-interval error is mean
                                        absolute error in intervals with
                                        positive reference input. Uncertainty is
                                        not estimated.
                                    </p>
                                </details>
                            </>
                        )}
                        {historical && matched.length > 0 && (
                            <p className="fine">
                                Current configuration · factor 1 · retrospective
                                model comparison.
                                {matched.some(
                                    (day) => day.provisional_production,
                                ) &&
                                    " Includes provisional reported production; meter boundary is unverified."}
                            </p>
                        )}
                        {!resource.data.metrics && (
                            <Notice>
                                No complete production and reference pairs are
                                available for this range. Select another range
                                or check saved production data.
                            </Notice>
                        )}
                        {matched.length > 0 && (
                            <details open>
                                <summary>
                                    Daily comparison ({matched.length} days)
                                </summary>
                                <ScrollRegion label="Daily forecast comparison">
                                    <table>
                                        <caption>
                                            {historical
                                                ? "Historical daylight model"
                                                : "Daylight replay"}
                                            : {resource.data.start_date} to{" "}
                                            {resource.data.end_date}
                                        </caption>
                                        <thead>
                                            <tr>
                                                <th>Date</th>
                                                <th>Status</th>
                                                <th>
                                                    {historical
                                                        ? "Measured / reported (MWh)"
                                                        : "Measured (MWh)"}
                                                </th>
                                                <th>
                                                    {historical
                                                        ? "Model (MWh)"
                                                        : "Predicted (MWh)"}
                                                </th>
                                                <th>Error (MWh)</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {matched.map((day) => (
                                                <tr key={day.date}>
                                                    <td>{day.date}</td>
                                                    <td>
                                                        {day.provisional_production
                                                            ? "Provisional production"
                                                            : "Matched"}
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
                                                    <td>
                                                        {format(day.error_mwh)}
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </ScrollRegion>
                            </details>
                        )}
                        {missingReasons.size > 0 && (
                            <details>
                                <summary>
                                    Unavailable dates (
                                    {resource.data.requested_days -
                                        resource.data.matched_days}
                                    )
                                </summary>
                                <ul>
                                    {[...missingReasons].map(
                                        ([reason, count]) => (
                                            <li key={reason}>
                                                {reason}: {count}{" "}
                                                {count === 1 ? "day" : "days"}
                                            </li>
                                        ),
                                    )}
                                </ul>
                            </details>
                        )}
                    </>
                )
            )}
        </>
    );
}
