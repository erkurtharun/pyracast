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
import { useShownDays } from "@/features/plant/shown-days";
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
                </div>
            </div>
            <ViewNav
                label={t("production_comparison")}
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
            <form className="forecast-controls" onSubmit={load}>
                <ControlField
                    label={t("start_date")}
                    hint={t("daylight_rules")}
                >
                    {(describedBy) => (
                        <input
                            type="date"
                            required
                            value={start}
                            max={end}
                            aria-describedby={describedBy}
                            onChange={(event) => setStart(event.target.value)}
                        />
                    )}
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
    const signed = (value: number | null | undefined) =>
        formatter.signed(value, 2);
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
    useShownDays(resource.data?.start_date, resource.data?.end_date);
    const matched =
        resource.data?.days.filter((day) => day.status === "matched") ?? [];
    const historical = resource.data?.comparison === "historical_model";
    const mode = historical ? "historical" : "forecast";
    const modeName = t(historical ? "model_check" : "forecast_accuracy");
    const realtimeDay = (day: (typeof matched)[number]) =>
        day.provisional_snapshot_ids.length > 0;
    const realtime = matched.some(realtimeDay);
    const notCompared =
        (resource.data?.requested_days ?? 0) -
        (resource.data?.matched_days ?? 0);
    const unavailableDates = t("unavailable_dates", { count: notCompared });
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
                                build={() =>
                                    performanceExport(plant, data, {
                                        file: t("file"),
                                        comparison: modeName,
                                        forecast: t("forecast_kind"),
                                        satellite: t("satellite_kind"),
                                    })
                                }
                            />
                        </div>
                        {realtime && (
                            <p className="fine">
                                {t(
                                    "_includes_provisional_reported_production_meter_boundary",
                                )}
                            </p>
                        )}
                        {data.metrics && (
                            <>
                                <div className="stats">
                                    <Stat
                                        label={t("relative_error")}
                                        value={percent(data.relative_error)}
                                    />
                                    <Stat
                                        label={t("matched_days")}
                                        value={data.matched_days}
                                    />
                                </div>
                                <div className="stats">
                                    <Stat
                                        label={t("daylight_energy_error")}
                                        value={data.metrics?.daily_mae_mwh}
                                        unit="MWh"
                                    />
                                    <DifferenceStat
                                        label={t("daylight_energy_bias")}
                                        value={signed(
                                            data.metrics?.daily_bias_mwh,
                                        )}
                                        meaning={(direction) =>
                                            t("difference_meaning", {
                                                mode,
                                                direction,
                                            })
                                        }
                                    />
                                </div>
                            </>
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
                                <ScrollRegion label={t("matched_days")}>
                                    <table>
                                        <caption>
                                            {t("date_range", {
                                                mode: modeName,
                                                start: data.start_date,
                                                end: data.end_date,
                                            })}
                                        </caption>
                                        <thead>
                                            <tr>
                                                <th>{t("date")}</th>
                                                <th>{t("status")}</th>
                                                <th>{t("measured_mwh")}</th>
                                                <th>
                                                    {historical
                                                        ? t("model_mwh")
                                                        : t("predicted_mwh")}
                                                </th>
                                                <th>
                                                    {t("error_mwh", { mode })}
                                                </th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {matched.map((day) => (
                                                <tr key={day.date}>
                                                    <td>{day.date}</td>
                                                    <td>
                                                        {realtimeDay(day)
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
                        {data.requested_days > 0 &&
                            (missingReasons.size > 0 ? (
                                <details>
                                    <summary>{unavailableDates}</summary>
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
                            ) : (
                                <p>{unavailableDates}</p>
                            ))}
                    </>
                )}
            </Resource>
        </>
    );
}

function DifferenceStat({
    label,
    value,
    meaning,
}: {
    label: string;
    value: string;
    meaning: (direction: "high" | "low") => string;
}) {
    const direction = value.startsWith("+")
        ? "high"
        : value.startsWith("−")
          ? "low"
          : null;
    return (
        <Stat
            label={label}
            value={value}
            unit="MWh"
            note={direction ? meaning(direction) : undefined}
        />
    );
}
