"use client";
import { useLocale, useTranslations } from "next-intl";
import { ScrollRegion } from "./scroll-region";

import { useState, type FormEvent } from "react";
import { localDay } from "./data-source";
import type { PlantScope } from "@/lib/api";
import type { RequestOptions } from "@/lib/api/client";
import { useApiQuery } from "@/lib/api/query";
import { Notice, RefreshFailed, Stat } from "./ui";
import { CsvDownload } from "./csv-download";

type PerformanceQuery = RequestOptions<
    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/performance/",
    "get"
>["query"];
type Comparison = NonNullable<PerformanceQuery>["comparison"];

export function ForecastPerformance({
    scope,
    timezone,
}: {
    scope: PlantScope;
    timezone: string;
}) {
    const t = useTranslations("ForecastPerformance");

    const [start, setStart] = useState(() => localDay(timezone, -30));
    const [end, setEnd] = useState(() => localDay(timezone, -1));
    const [comparison, setComparison] =
        useState<Comparison>("historical_model");
    const [issuanceCutoff, setIssuanceCutoff] = useState("");
    const [query, setQuery] = useState<PerformanceQuery>({
        daylight_only: true,
        comparison: "historical_model",
    });
    function load(event: FormEvent) {
        event.preventDefault();
        setQuery({
            start_date: start,
            end_date: end,
            daylight_only: true,
            comparison,
            ...(comparison === "archived_forecast" && issuanceCutoff
                ? { issuance_cutoff: issuanceCutoff }
                : {}),
        });
    }
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>{t("production_comparison")}</h2>
                    <p className="fine">
                        {t("comparison_description", { timezone })}
                    </p>
                </div>
            </div>
            <details>
                <summary>{t("how_comparison_is_measured")}</summary>
                <p>{t("historical_model_uses_saved_weather_estimates_and")}</p>
                <p>{t("archived_forecast_description")}</p>
                <p>{t("daylight_rules", { timezone })}</p>
            </details>
            <form className="forecast-controls" onSubmit={load}>
                <label className="field">
                    {t("comparison")}
                    <select
                        value={comparison}
                        onChange={(event) =>
                            setComparison(
                                event.target.value === "archived_forecast"
                                    ? "archived_forecast"
                                    : "historical_model",
                            )
                        }
                    >
                        <option value="historical_model">
                            {t("historical_model")}
                        </option>
                        <option value="archived_forecast">
                            {t("archived_forecast_performance")}
                        </option>
                    </select>
                </label>
                {comparison === "archived_forecast" && (
                    <label className="field">
                        {t("previousday_cutoff")}
                        <input
                            type="time"
                            value={issuanceCutoff}
                            onChange={(event) =>
                                setIssuanceCutoff(event.target.value)
                            }
                        />
                    </label>
                )}
                <label className="field">
                    {t("start_date")}
                    <input
                        type="date"
                        required
                        value={start}
                        max={end}
                        onChange={(event) => setStart(event.target.value)}
                    />
                </label>
                <label className="field">
                    {t("end_date")}
                    <input
                        type="date"
                        required
                        value={end}
                        max={localDay(timezone, -1)}
                        onChange={(event) => setEnd(event.target.value)}
                    />
                </label>
                <button className="button secondary" type="submit">
                    {t("load_performance")}
                </button>
            </form>
            <PerformanceResults
                key={JSON.stringify(query)}
                scope={scope}
                query={query}
                timezone={timezone}
            />
        </section>
    );
}

function PerformanceResults({
    scope,
    query,
    timezone,
}: {
    scope: PlantScope;
    query: PerformanceQuery;
    timezone: string;
}) {
    const t = useTranslations("ForecastPerformance");
    const locale = useLocale();
    const format = (value: number | null) =>
        value === null
            ? "—"
            : new Intl.NumberFormat(locale, {
                  maximumFractionDigits: 3,
              }).format(value);
    const reasons: Record<string, string> = {
        estimated_actuals_not_available: t(
            "historical_weather_estimates_are_not_available",
        ),
        configuration_not_available: t(
            "configuration_was_not_available_before_this_day",
        ),
        forecast_not_available: t("archived_forecast_unavailable"),
        configuration_not_comparable: t("configuration_not_comparable"),
        incomplete_or_invalid_day: t(
            "production_or_reference_coverage_is_incomplete_or",
        ),
        invalid_forecast_data: t("saved_reference_data_is_invalid"),
    };

    const resource = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/performance/",
        {
            path: scope,
            query,
        },
    );
    const matched =
        resource.data?.days.filter((day) => day.status === "matched") ?? [];
    const historical = resource.data?.comparison === "historical_model";
    const missingReasons = new Map<string, number>();
    for (const day of resource.data?.days ?? []) {
        if (day.status === "matched") continue;
        const reason =
            (day.reason && reasons[day.reason]) ||
            t("production_or_reference_data_is_unavailable");
        missingReasons.set(reason, (missingReasons.get(reason) ?? 0) + 1);
    }
    return (
        <>
            {resource.data === undefined ? (
                resource.error ? (
                    <Notice error>{resource.error}</Notice>
                ) : (
                    <p role="status">{t("loading_performance")}</p>
                )
            ) : (
                resource.data && (
                    <>
                        <RefreshFailed resource={resource} />
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
                                        label={t("matched_days")}
                                        value={resource.data.matched_days}
                                    />
                                    <Stat
                                        label={t("missing_days")}
                                        value={
                                            resource.data.missing_days ??
                                            resource.data.requested_days -
                                                resource.data.matched_days
                                        }
                                    />
                                    {(resource.data.excluded_days ?? 0) > 0 && (
                                        <Stat
                                            label={t("excluded_days")}
                                            value={resource.data.excluded_days}
                                        />
                                    )}
                                    <Stat
                                        label={t("daylight_energy_error")}
                                        value={
                                            resource.data.metrics?.daily_mae_mwh
                                        }
                                        unit="MWh"
                                    />
                                    <Stat
                                        label={t("daylight_energy_bias")}
                                        value={
                                            resource.data.metrics
                                                ?.daily_bias_mwh
                                        }
                                        unit="MWh"
                                    />
                                </div>
                                <details>
                                    <summary>
                                        {t("additional_metrics_interpretation")}
                                    </summary>
                                    <div className="stats">
                                        <Stat
                                            label={t(
                                                "baseline_daylight_energy_error",
                                            )}
                                            value={
                                                resource.data.baseline_metrics
                                                    ?.daily_mae_mwh
                                            }
                                            unit="MWh"
                                        />
                                        <Stat
                                            label={t(
                                                "generatinginterval_error",
                                            )}
                                            value={
                                                resource.data.metrics
                                                    ?.source_positive_mae_mw
                                            }
                                            unit="MW"
                                        />
                                    </div>
                                    {resource.data.scored_intervals != null && (
                                        <p className="fine">
                                            {t("scored_coverage", {
                                                count: resource.data
                                                    .scored_intervals,
                                                hours: format(
                                                    resource.data.scored_hours,
                                                ),
                                            })}
                                        </p>
                                    )}
                                    <p className="fine">
                                        {t(
                                            "bias_is_predicted_minus_measured_daily_energy",
                                        )}
                                    </p>
                                </details>
                            </>
                        )}
                        {historical && matched.length > 0 && (
                            <p className="fine">
                                {t(
                                    "current_configuration_calibration_from_its_training_period",
                                )}
                                {matched.some(
                                    (day) => day.provisional_production,
                                ) &&
                                    t(
                                        "_includes_provisional_reported_production_meter_boundary",
                                    )}
                            </p>
                        )}
                        {!resource.data.metrics && (
                            <Notice>
                                {t(
                                    "no_complete_production_and_reference_pairs_are",
                                )}
                            </Notice>
                        )}
                        {matched.length > 0 && (
                            <details open>
                                <summary>
                                    {t("daily_comparison", {
                                        count: matched.length,
                                    })}
                                </summary>
                                <ScrollRegion
                                    label={t("daily_forecast_comparison")}
                                >
                                    <table>
                                        <caption>
                                            {historical
                                                ? t("historical_daylight_model")
                                                : t("daylight_replay")}{" "}
                                            {t("date_range", {
                                                start: resource.data.start_date,
                                                end: resource.data.end_date,
                                            })}
                                        </caption>
                                        <thead>
                                            <tr>
                                                <th>{t("date")}</th>
                                                <th>{t("status")}</th>
                                                <th>
                                                    {historical
                                                        ? t(
                                                              "measured_reported_mwh",
                                                          )
                                                        : t("measured_mwh")}
                                                </th>
                                                <th>
                                                    {historical
                                                        ? t("model_mwh")
                                                        : t("predicted_mwh")}
                                                </th>
                                                <th>{t("error_mwh")}</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {matched.map((day) => (
                                                <tr key={day.date}>
                                                    <td>{day.date}</td>
                                                    <td>
                                                        {day.provisional_production
                                                            ? t(
                                                                  "provisional_production",
                                                              )
                                                            : t("matched")}
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
                                    {t("unavailable_dates", {
                                        count:
                                            resource.data.requested_days -
                                            resource.data.matched_days,
                                    })}
                                </summary>
                                <ul>
                                    {[...missingReasons].map(
                                        ([reason, count]) => (
                                            <li key={reason}>
                                                {t("reason_count", {
                                                    reason,
                                                    count,
                                                })}
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
