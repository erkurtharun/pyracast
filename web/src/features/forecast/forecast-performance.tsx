"use client";
import { useTranslations } from "next-intl";
import { ScrollRegion } from "@/ui/scroll-region";

import { useId, useState, type SubmitEvent } from "react";
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
type Source = NonNullable<NonNullable<PerformanceQuery>["data_source"]>;

export function ForecastPerformance({
    scope,
    timezone,
}: {
    scope: PlantScope;
    timezone: string;
}) {
    const t = useTranslations("ForecastPerformance");
    const sourceName = useTranslations("DataSource");
    const limitId = useId();

    const [start, setStart] = useState(() => plantDay(timezone, -30));
    const [end, setEnd] = useState(() => plantDay(timezone, -1));
    const [source, setSource] = useState<Source>("forecast");
    const [query, setQuery] = useState<PerformanceQuery>({
        data_source: "forecast",
    });
    function request(chosen: Source): PerformanceQuery {
        return { start_date: start, end_date: end, data_source: chosen };
    }
    function load(event: SubmitEvent) {
        event.preventDefault();
        setQuery(request(source));
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
                    ["forecast", sourceName("forecast")],
                    ["satellite", sourceName("satellite")],
                ]}
                current={source}
                onSelect={(chosen) => {
                    setSource(chosen);
                    setQuery(request(chosen));
                }}
            />
            <form className="forecast-controls" onSubmit={load}>
                <ControlField label={t("start_date")}>
                    <input
                        type="date"
                        required
                        value={start}
                        max={end}
                        aria-describedby={limitId}
                        onChange={(event) => setStart(event.target.value)}
                    />
                </ControlField>
                <ControlField label={t("end_date")}>
                    <input
                        type="date"
                        required
                        value={end}
                        max={plantDay(timezone, -1)}
                        aria-describedby={limitId}
                        onChange={(event) => setEnd(event.target.value)}
                    />
                </ControlField>
                <button className="button secondary" type="submit">
                    {t("load_performance")}
                </button>
                <p className="fine" id={limitId}>
                    {t("range_limit")}
                </p>
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
    const sourceName = useTranslations("DataSource");
    const plant = useExportPlant();
    const format = useFormat();
    const energy = (value: number | null | undefined) =>
        format.rounded(value, "MWh");
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
    const mode = resource.data?.data_source ?? "forecast";
    const modeName = sourceName(mode);
    const realtime = matched.some((day) => day.provisional_production);
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
            format.day(day.date),
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
                        {data.matched_days > 0 && (
                            <>
                                <div className="stats">
                                    <Stat
                                        label={t("relative_error")}
                                        value={format.percent(
                                            data.relative_error,
                                        )}
                                    />
                                    <Stat
                                        label={t("matched_days")}
                                        value={data.matched_days}
                                    />
                                </div>
                                <div className="stats">
                                    <Stat
                                        label={t("daylight_energy_error")}
                                        value={energy(data.daily_mae_mwh)}
                                        unit="MWh"
                                    />
                                    <DifferenceStat
                                        label={t("daylight_energy_bias")}
                                        value={format.signed(
                                            data.daily_bias_mwh,
                                            2,
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
                        {data.matched_days === 0 && (
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
                                                start: format.day(
                                                    data.start_date,
                                                ),
                                                end: format.day(data.end_date),
                                            })}
                                        </caption>
                                        <thead>
                                            <tr>
                                                <th>{t("date")}</th>
                                                <th>{t("status")}</th>
                                                <th>{t("measured_mwh")}</th>
                                                <th>
                                                    {t("shown_mwh", { mode })}
                                                </th>
                                                <th>
                                                    {t("error_mwh", { mode })}
                                                </th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {matched.map((day) => (
                                                <tr key={day.date}>
                                                    <td>
                                                        {format.day(day.date)}
                                                    </td>
                                                    <td>
                                                        {day.provisional_production
                                                            ? t(
                                                                  "provisional_production",
                                                              )
                                                            : t("matched")}
                                                    </td>
                                                    <td>
                                                        {energy(
                                                            day.measured_energy_mwh,
                                                        )}
                                                    </td>
                                                    <td>
                                                        {energy(
                                                            day.predicted_energy_mwh,
                                                        )}
                                                    </td>
                                                    <td>
                                                        {energy(day.error_mwh)}
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
