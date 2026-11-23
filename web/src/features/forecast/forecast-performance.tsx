"use client";
import { useTranslations } from "next-intl";
import { ScrollRegion } from "@/ui/scroll-region";

import { useState, type SubmitEvent } from "react";
import { ViewNav } from "@/ui/action";
import type { PlantScope } from "@/lib/api";
import type { RequestOptions } from "@/lib/api/client";
import { useApiQuery } from "@/lib/api/query";
import { plantDay } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import { ControlField } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { useCodeLabel } from "@/ui/vocabulary";
import { Notice, Stat } from "@/ui/ui";
import { CsvDownload } from "@/ui/csv-download";
import { DailyAccuracyChart } from "./daily-accuracy-chart";
import { useExportPlant } from "@/ui/export-plant";
import { performanceExport } from "@/lib/exports";

type PerformanceQuery = RequestOptions<
    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/performance/",
    "get"
>["query"];
type Comparison = NonNullable<NonNullable<PerformanceQuery>["comparison"]>;

export function ForecastPerformance({
    scope,
    timezone,
}: {
    scope: PlantScope;
    timezone: string;
}) {
    const t = useTranslations("ForecastPerformance");

    const [start, setStart] = useState(() => plantDay(timezone, -30));
    const [end, setEnd] = useState(() => plantDay(timezone, -1));
    const [comparison, setComparison] =
        useState<Comparison>("archived_forecast");
    const [issuanceCutoff, setIssuanceCutoff] = useState("");
    const [query, setQuery] = useState<PerformanceQuery>({
        daylight_only: true,
        comparison: "archived_forecast",
    });
    function request(mode: Comparison): PerformanceQuery {
        return {
            start_date: start,
            end_date: end,
            daylight_only: true,
            comparison: mode,
            ...(mode === "archived_forecast" && issuanceCutoff
                ? { issuance_cutoff: issuanceCutoff }
                : {}),
        };
    }
    function load(event: SubmitEvent) {
        event.preventDefault();
        setQuery(request(comparison));
    }
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>{t("production_comparison")}</h2>
                    <p className="fine">{t("comparison_description")}</p>
                </div>
            </div>
            <ViewNav
                label={t("performance_views")}
                tabs
                views={[
                    ["archived_forecast", t("forecast_accuracy")],
                    ["historical_model", t("model_check")],
                ]}
                current={comparison}
                onSelect={(mode) => {
                    setComparison(mode);
                    setQuery(request(mode));
                }}
            />
            <details>
                <summary>{t("how_comparison_is_measured")}</summary>
                <p>
                    {comparison === "archived_forecast"
                        ? t("archived_forecast_description")
                        : t(
                              "historical_model_uses_saved_weather_estimates_and",
                          )}
                </p>
                <p>{t("daylight_rules")}</p>
            </details>
            <form className="forecast-controls" onSubmit={load}>
                <ControlField label={t("start_date")}>
                    <input
                        type="date"
                        required
                        value={start}
                        max={end}
                        onChange={(event) => setStart(event.target.value)}
                    />
                </ControlField>
                <ControlField label={t("end_date")}>
                    <input
                        type="date"
                        required
                        value={end}
                        max={plantDay(timezone, -1)}
                        onChange={(event) => setEnd(event.target.value)}
                    />
                </ControlField>
                <button className="button secondary" type="submit">
                    {t("load_performance")}
                </button>
                {comparison === "archived_forecast" && (
                    <details>
                        <summary>{t("advanced")}</summary>
                        <ControlField label={t("previousday_cutoff")}>
                            <input
                                type="time"
                                value={issuanceCutoff}
                                onChange={(event) =>
                                    setIssuanceCutoff(event.target.value)
                                }
                            />
                        </ControlField>
                    </details>
                )}
            </form>
            <PerformanceResults
                key={JSON.stringify(query)}
                scope={scope}
                query={query}
            />
        </section>
    );
}

function PerformanceResults({
    scope,
    query,
}: {
    scope: PlantScope;
    query: PerformanceQuery;
}) {
    const t = useTranslations("ForecastPerformance");
    const plant = useExportPlant();
    const formatter = useFormat();
    const format = (value: number | null) => formatter.number(value);
    const percent = (value: number | null) =>
        formatter.quantity(value === null ? null : value * 100, "%", 1);
    const reasonLabel = useCodeLabel("ForecastPerformance.reasons");
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
    const missingReasons = new Map<string, string[]>();
    for (const day of resource.data?.days ?? []) {
        if (day.status === "matched") continue;
        const unavailable = t("production_or_reference_data_is_unavailable");
        const reason = day.reason
            ? reasonLabel(day.reason, unavailable)
            : unavailable;
        missingReasons.set(reason, [
            ...(missingReasons.get(reason) ?? []),
            day.date,
        ]);
    }
    return (
        <>
            <Resource resource={resource} loading={t("loading_performance")}>
                {(data) => (
                    <>
                        <div className="toolbar">
                            <CsvDownload
                                build={() => performanceExport(plant, data)}
                            />
                        </div>
                        {data.metrics && (
                            <>
                                <div className="stats">
                                    <Stat
                                        label={t("relative_error")}
                                        value={percent(data.relative_error)}
                                    />
                                    <Stat
                                        label={t("skill_score")}
                                        value={percent(data.skill_score)}
                                    />
                                    <Stat
                                        label={t("matched_days")}
                                        value={data.matched_days}
                                    />
                                </div>
                                {historical && (
                                    <p className="fine">
                                        {t("skill_days", {
                                            count: data.skill_days,
                                        })}
                                    </p>
                                )}
                                <div className="stats">
                                    <Stat
                                        label={t("missing_days")}
                                        value={
                                            data.missing_days ??
                                            data.requested_days -
                                                data.matched_days
                                        }
                                    />
                                    {(data.excluded_days ?? 0) > 0 && (
                                        <Stat
                                            label={t("excluded_days")}
                                            value={data.excluded_days}
                                        />
                                    )}
                                    <Stat
                                        label={t("daylight_energy_error")}
                                        value={data.metrics?.daily_mae_mwh}
                                        unit="MWh"
                                    />
                                    <Stat
                                        label={t("daylight_energy_bias")}
                                        value={data.metrics?.daily_bias_mwh}
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
                                                data.baseline_metrics
                                                    ?.daily_mae_mwh
                                            }
                                            unit="MWh"
                                        />
                                        <Stat
                                            label={t(
                                                "generatinginterval_error",
                                            )}
                                            value={
                                                data.metrics
                                                    ?.source_positive_mae_mw
                                            }
                                            unit="MW"
                                        />
                                    </div>
                                    {data.scored_intervals != null && (
                                        <p className="fine">
                                            {t("scored_coverage", {
                                                count: data.scored_intervals,
                                                hours: format(
                                                    data.scored_hours,
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
                        {!data.metrics && (
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
                                <DailyAccuracyChart days={matched} />
                                <ScrollRegion
                                    label={t("daily_forecast_comparison")}
                                >
                                    <table>
                                        <caption>
                                            {historical
                                                ? t("historical_daylight_model")
                                                : t("daylight_replay")}{" "}
                                            {t("date_range", {
                                                start: data.start_date,
                                                end: data.end_date,
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
                                            data.requested_days -
                                            data.matched_days,
                                    })}
                                </summary>
                                <ul>
                                    {[...missingReasons].map(
                                        ([reason, dates]) => (
                                            <li key={reason}>
                                                {t("reason_dates", {
                                                    reason,
                                                    dates: dates.join(", "),
                                                })}
                                            </li>
                                        ),
                                    )}
                                </ul>
                            </details>
                        )}
                    </>
                )}
            </Resource>
        </>
    );
}
