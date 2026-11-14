"use client";
import { useLocale, useTranslations } from "next-intl";
import { useState } from "react";
import {
    ForecastIntervals,
    type ForecastInterval as Interval,
} from "./forecast-intervals";
import { WeatherReferenceCard } from "./weather-reference";
import type { Forecast } from "@/lib/api";
import { Empty, JsonDetails, Notice, Stat, Tag } from "./ui";
import { CsvDownload } from "./csv-download";
import { dataIntervalRange } from "@/lib/intervals";
type DaylightSummary = {
    status: "normal" | "polar_day" | "polar_night";
    complete: boolean;
    coverage_fraction: number | null;
    input_coverage_fraction?: number | null;
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
type ProductionDiagnostic = {
    status: "suspected" | "normal" | "insufficient";
    performance_ratio: number | null;
    reference_kind: "active_calibration" | "uncalibrated_reference";
    window_start: string | null;
    window_end: string | null;
    days: {
        day: string;
        performance_index: number;
        hours: number;
        status: "shortfall" | "within_threshold";
        observed_zero: boolean;
    }[];
    latest_matched_day: string | null;
    latest_day_status:
        "shortfall" | "within_threshold" | "insufficient" | "unavailable";
    latest_performance_index: number | null;
    evidence_state: "current" | "stale";
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

export function ForecastPanel({
    forecast,
    electrical = false,
}: {
    forecast: Forecast;
    electrical?: boolean;
}) {
    const t = useTranslations("Forecast");
    const quality = useTranslations("Forecast.qualityFlags");
    const flagLabel = (flag: string) =>
        quality.has(flag)
            ? quality(flag)
            : quality("unknown", {
                  value: locale === "en" ? flag.replaceAll("_", " ") : flag,
              });

    const locale = useLocale();
    function number(value: unknown): string {
        return typeof value === "number"
            ? new Intl.NumberFormat(locale, {
                  maximumFractionDigits: 3,
              }).format(value)
            : "—";
    }

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
                    <Empty title={t("no_estimated_actuals_for_this_day")}>
                        {t("estimated_actuals_are_not_available_for_this")}
                    </Empty>
                ) : electrical &&
                  forecast.unavailable_reason ===
                      "configuration_unavailable" ? (
                    <Empty title={t("no_configuration_for_this_day")}>
                        {t("no_configuration_existed_before_the_selected_day")}
                    </Empty>
                ) : (
                    <Empty title={t("no_forecast_for_this_day_yet")}>
                        {t("a_missing_forecast_is_not_zero_production")}
                    </Empty>
                )}
            </>
        );
    const scenarios = forecast as Forecast & {
        outage_scenario?: OutageScenario | null;
        production_diagnostic?: ProductionDiagnostic | null;
    };
    const outage = electrical ? scenarios.outage_scenario : null;
    const diagnostic = electrical ? scenarios.production_diagnostic : null;
    const scenario = outage ?? null;
    const Affected = t("with_outages_title");
    const uncalibrated =
        diagnostic?.reference_kind === "uncalibrated_reference";
    const latestShortfall = diagnostic?.latest_day_status === "shortfall";
    const diagnosticDays = diagnostic?.days.map((day) => day.day).sort() ?? [];
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
    const partial = primary.complete !== true && !noDaylight;
    const affectedPartial =
        affectedSummary?.complete !== true &&
        affectedDaylight?.status !== "polar_night";
    const coverage =
        !electrical && daylight?.input_coverage_fraction !== undefined
            ? daylight.input_coverage_fraction
            : primary.coverage_fraction;
    const dayHours =
        typeof result.start === "string" && typeof result.end === "string"
            ? (Date.parse(result.end) - Date.parse(result.start)) / 3_600_000
            : Number.NaN;
    const coveredHours =
        typeof result.coverage_fraction === "number"
            ? result.coverage_fraction * dayHours
            : Number.NaN;
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
        new Intl.DateTimeFormat(locale, {
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
                            ? t("electrical_generation")
                            : t("irradiance")}
                    </h2>
                    <p className="fine">
                        {forecast.date} · {forecast.timezone}
                    </p>
                    {forecast.source_kind === "estimated_actuals" && (
                        <p className="fine">
                            {t("satellitemodel_estimates_not_meter_readings")}
                        </p>
                    )}
                    {forecast.source_kind === "forecast" &&
                        Array.isArray(result.quality_flags) &&
                        result.quality_flags.includes("forecast_fallback") && (
                            <p className="fine">
                                {t("saved_forecast_pending_history")}
                            </p>
                        )}
                </div>
                <div className="button-group">
                    <CsvDownload
                        filename={`${electrical ? "plant" : "region"}-${forecast.date}-${forecast.source_kind}`}
                        label={
                            scenario
                                ? t("download_both_scenarios_csv")
                                : t("download_csv")
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
                    {mixedVintages && <Tag>{t("mixed_forecast_updates")}</Tag>}
                    {Array.isArray(result.quality_flags) &&
                        result.quality_flags.includes("archived_forecast") && (
                            <Tag>{t("archived_forecast")}</Tag>
                        )}
                    {Array.isArray(result.quality_flags) &&
                        result.quality_flags.includes(
                            "provisional_production_calibration",
                        ) && (
                            <Tag tone="warning">
                                {t("provisional_production_calibration")}
                            </Tag>
                        )}
                    {Array.isArray(result.quality_flags) &&
                        result.quality_flags.includes(
                            "unvalidated_reference_transfer",
                        ) && <Tag tone="warning">{t("needs_validation")}</Tag>}
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
            {diagnostic?.evidence_state === "stale" ? (
                <Notice>{t("production_comparison_is_out_of_date")}</Notice>
            ) : diagnostic &&
              (latestShortfall || diagnostic.status === "suspected") ? (
                <Notice>
                    {latestShortfall &&
                    diagnostic.latest_performance_index !== null &&
                    diagnostic.latest_matched_day
                        ? t(
                              uncalibrated
                                  ? "production_below_uncalibrated"
                                  : "production_below_model",
                              {
                                  percent: number(
                                      Math.round(
                                          (1 -
                                              diagnostic.latest_performance_index) *
                                              1000,
                                      ) / 10,
                                  ),
                                  date: diagnostic.latest_matched_day,
                              },
                          )
                        : t(
                              uncalibrated
                                  ? "earlier_below_uncalibrated"
                                  : "earlier_below_model",
                          )}
                </Notice>
            ) : null}
            {scenario?.status === "unknown_impact" && (
                <Notice>
                    {t("outage_impact_is_unknown_for_some_intervals")}
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
                        {t("update_remaining_capacity_in_outages_after_this")}
                    </Notice>
                )}
            {electrical &&
                Array.isArray(result.quality_flags) &&
                result.quality_flags.includes(
                    "historical_configuration_assumed",
                ) && (
                    <Notice>
                        {t("estimated_using_settings_recorded_after_this_date")}
                    </Notice>
                )}
            {daylight &&
                (noDaylight ? (
                    <p className="fine">{t("no_daylight_on_this_date")}</p>
                ) : !daylight.complete ? (
                    <Notice>
                        {coverage === 1
                            ? electrical
                                ? t("some_interval_values_are_unavailable")
                                : t(
                                      "some_irradiance_values_could_not_be_calculated",
                                  )
                            : t("some_forecast_data_is_missing")}
                    </Notice>
                ) : null)}
            {!daylight && result.complete !== true && (
                <Notice>{t("some_forecast_data_is_missing")}</Notice>
            )}
            <div className="stats forecast-stats">
                <Stat
                    label={
                        scenario
                            ? t(
                                  partial
                                      ? "normal_capacity_partial"
                                      : "normal_capacity",
                              )
                            : partial
                              ? t("partial_estimate")
                              : t("estimated_energy")
                    }
                    value={
                        partial && primary.coverage_fraction === 0
                            ? undefined
                            : primary[
                                  partial ? partialEnergyField : energyField
                              ]
                    }
                    unit={electrical ? "MWh" : "kWh/m²"}
                />
                {scenario && (
                    <Stat
                        label={t(
                            affectedPartial
                                ? "outage_partial"
                                : "with_outages_title",
                        )}
                        value={
                            affectedPartial &&
                            affectedSummary?.coverage_fraction === 0
                                ? undefined
                                : affectedSummary?.[
                                      affectedPartial
                                          ? "partial_energy_mwh"
                                          : "energy_mwh"
                                  ]
                        }
                        unit="MWh"
                    />
                )}
                <Stat
                    label={t("coverage")}
                    value={
                        typeof coverage === "number"
                            ? coverage * 100
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
                    {t("show_full_day")}
                </span>
            </label>
            {rows.length === 0 && <p>{t("no_interval_data_available")}</p>}
            {rows.length > 0 && (
                <>
                    <div className="chart-title">
                        <strong>
                            {electrical
                                ? t("power_across_the_day")
                                : t("irradiance_across_the_day")}
                        </strong>
                        <span>{unit}</span>
                    </div>
                    {scenario && (
                        <div className="chart-legend">
                            <span>
                                <i className="normal" aria-hidden="true" />
                                {t("normal_capacity")}
                            </span>
                            <span>
                                <i className="affected" aria-hidden="true" />
                                {Affected}
                            </span>
                        </div>
                    )}
                    <div
                        className="chart"
                        role="img"
                        aria-label={t(
                            scenario
                                ? "scenario_chart"
                                : electrical
                                  ? "power_chart"
                                  : "irradiance_chart",
                        )}
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
                                title={
                                    scenario
                                        ? t("scenario_tooltip", {
                                              time: time(row.start),
                                              normal: number(row[metric]),
                                              affected: number(
                                                  row.affected_mean_power_mw,
                                              ),
                                              unit,
                                          })
                                        : t("interval_tooltip", {
                                              time: time(row.start),
                                              value: number(row[metric]),
                                              unit,
                                          })
                                }
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
                        <span>
                            {t("local_time", { timezone: forecast.timezone })}
                        </span>
                        <span>{time(rows[rows.length - 1].end)}</span>
                    </div>
                    <ForecastIntervals
                        rows={rows}
                        electrical={electrical}
                        scenarioLabel={scenario ? Affected : undefined}
                        label={
                            forecast.source_kind === "estimated_actuals"
                                ? t("estimated_actual_intervals")
                                : t("forecast_intervals")
                        }
                        timezone={forecast.timezone}
                        formatTime={time}
                    />
                </>
            )}
            <details className="provenance">
                <summary>{t("source_quality")}</summary>
                {diagnostic && (
                    <p>
                        {diagnostic.reference_kind === "uncalibrated_reference"
                            ? t("uncalibrated_reference_model")
                            : t("active_calibration_reference")}
                        .
                        {diagnosticDays.length > 0 &&
                            t("qualifying_days", {
                                count: diagnosticDays.length,
                                start: diagnosticDays[0],
                                end: diagnosticDays[diagnosticDays.length - 1],
                            })}
                        {diagnostic.performance_ratio !== null &&
                            t("median_ratio", {
                                ratio: number(diagnostic.performance_ratio),
                            })}
                        {diagnostic.latest_day_status === "within_threshold" &&
                            diagnostic.latest_matched_day &&
                            t("latest_within_threshold", {
                                date: diagnostic.latest_matched_day,
                            })}
                        {diagnostic.latest_day_status === "insufficient" &&
                            t("_latest_day_has_insufficient_comparable_data")}
                        {diagnostic.latest_day_status === "unavailable" &&
                            t("_latest_comparison_is_unavailable")}
                    </p>
                )}
                {daylight && (
                    <p>
                        {t(
                            "energy_includes_whole_source_intervals_overlapping_daylight",
                        )}
                    </p>
                )}
                {!electrical &&
                    daylight?.input_coverage_fraction !== undefined && (
                        <p>
                            {t("source_coverage", {
                                percent: number(
                                    daylight.input_coverage_fraction === null
                                        ? null
                                        : daylight.input_coverage_fraction *
                                              100,
                                ),
                            })}
                        </p>
                    )}
                {!electrical && daylight && (
                    <p>
                        {t("irradiance_coverage", {
                            percent: number(
                                daylight.coverage_fraction === null
                                    ? null
                                    : daylight.coverage_fraction * 100,
                            ),
                        })}
                    </p>
                )}
                <p>
                    {t("full_day_coverage", {
                        percent: number(
                            typeof result.coverage_fraction === "number"
                                ? result.coverage_fraction * 100
                                : undefined,
                        ),
                    })}
                    {Number.isFinite(coveredHours) && Number.isFinite(dayHours)
                        ? t("covered_hours", {
                              covered: number(coveredHours),
                              total: number(dayHours),
                          })
                        : ""}
                </p>
                <p>
                    {t(
                        result.complete === true
                            ? "full_day_energy"
                            : "full_day_partial_energy",
                        {
                            energy: number(
                                result.complete !== true &&
                                    result.coverage_fraction === 0
                                    ? undefined
                                    : result[
                                          result.complete === true
                                              ? energyField
                                              : partialEnergyField
                                      ],
                            ),
                            unit: electrical ? "MWh" : "kWh/m²",
                        },
                    )}
                </p>
                {!electrical && typeof result.coverage_seconds === "number" && (
                    <p>
                        {t("received_intervals", {
                            hours: number(result.coverage_seconds / 3600),
                        })}
                    </p>
                )}
                {!electrical &&
                    Array.isArray(result.quality_flags) &&
                    result.quality_flags.includes("ambiguous_zero_power") && (
                        <p>
                            {t(
                                "zero_pv_output_cannot_uniquely_determine_irradiance",
                            )}
                        </p>
                    )}
                {scenario && (
                    <p className="fine">
                        {t(
                            "the_outage_scenario_uses_reported_remaining_capacity",
                        )}
                    </p>
                )}
                <p>
                    {forecast.collected_at
                        ? t("collected_at", {
                              date: new Date(
                                  forecast.collected_at,
                              ).toLocaleString(locale, {
                                  timeZone: forecast.timezone,
                              }),
                          })
                        : ""}
                    {t("model_version", {
                        version: String(result.model_version ?? "—"),
                    })}{" "}
                    {forecast.configuration_id
                        ? t("configuration_id", {
                              id: forecast.configuration_id,
                          })
                        : ""}
                    {electrical &&
                    forecast.source_kind === "estimated_actuals" &&
                    !forecast.calibration_id
                        ? t(
                              "baseline_estimate_using_the_current_configuration_no",
                          )
                        : electrical
                          ? forecast.calibration_id
                              ? forecast.source_kind === "estimated_actuals"
                                  ? t("retrospective_calibration", {
                                        id: forecast.calibration_id,
                                    })
                                  : t("calibration_id", {
                                        id: forecast.calibration_id,
                                    })
                              : t("baseline_no_active_calibration")
                          : ""}
                </p>
                {mixedVintages &&
                    Array.isArray(provenance?.source_snapshots) && (
                        <ul>
                            {provenance.source_snapshots.map((source) => (
                                <li key={source.snapshot_id}>
                                    {t("forecast_update", {
                                        id: source.snapshot_id,
                                        date: new Date(
                                            source.collected_at,
                                        ).toLocaleString(locale, {
                                            timeZone: forecast.timezone,
                                        }),
                                    })}
                                </li>
                            ))}
                        </ul>
                    )}
                {Array.isArray(result.quality_flags) &&
                    result.quality_flags.length > 0 && (
                        <div className="tags">
                            {result.quality_flags.map((flag, index) => (
                                <Tag key={index}>{flagLabel(String(flag))}</Tag>
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
                    label={t("source_configuration_details")}
                />
            </details>
        </>
    );
}
