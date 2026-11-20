"use client";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import type { Forecast } from "@/lib/api";
import { JsonDetails, Tag } from "@/ui/ui";
import { useFlagLabel } from "./format";
import {
    hasFlag,
    type DaylightSummary,
    type ForecastResult,
    type ProductionDiagnostic,
} from "./model";

type Provenance = {
    source_snapshots?: {
        snapshot_id: number;
        collected_at: string;
        source_kind?: string;
    }[];
};

export function ForecastProvenance({
    forecast,
    result,
    electrical,
    scenario,
    diagnostic,
}: {
    forecast: Forecast;
    result: ForecastResult;
    electrical: boolean;
    scenario: boolean;
    diagnostic: ProductionDiagnostic | null | undefined;
}) {
    const t = useTranslations("Forecast");
    const format = useFormat();
    const number = format.number;
    const flagLabel = useFlagLabel();
    const localTime = (value: string) => format.time(value, forecast.timezone);
    const diagnosticDays = diagnostic?.days.map((day) => day.day).sort() ?? [];
    const [firstDay] = diagnosticDays;
    const lastDay = diagnosticDays.at(-1);
    const daylight = result.daylight as DaylightSummary | undefined;
    const energyField = electrical ? "energy_mwh" : "energy_kwh_m2";
    const partialEnergyField = electrical
        ? "partial_energy_mwh"
        : "partial_energy_kwh_m2";
    const dayHours =
        typeof result.start === "string" && typeof result.end === "string"
            ? (Date.parse(result.end) - Date.parse(result.start)) / 3_600_000
            : Number.NaN;
    const coveredHours =
        typeof result.coverage_fraction === "number"
            ? result.coverage_fraction * dayHours
            : Number.NaN;
    const provenance = result.provenance as Provenance | undefined;
    return (
        <details className="provenance">
            <summary>{t("source_quality")}</summary>
            {diagnostic && (
                <p>
                    {diagnostic.reference_kind === "uncalibrated_reference"
                        ? t("uncalibrated_reference_model")
                        : t("active_calibration_reference")}
                    .
                    {firstDay &&
                        lastDay &&
                        t("qualifying_days", {
                            count: diagnosticDays.length,
                            start: firstDay,
                            end: lastDay,
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
            {!electrical && daylight?.input_coverage_fraction !== undefined && (
                <p>
                    {t("source_coverage", {
                        percent: number(
                            daylight.input_coverage_fraction === null
                                ? null
                                : daylight.input_coverage_fraction * 100,
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
            {!electrical && hasFlag(result, "ambiguous_zero_power") && (
                <p>
                    {t("zero_pv_output_cannot_uniquely_determine_irradiance")}
                </p>
            )}
            {scenario && (
                <p className="fine">
                    {t("the_outage_scenario_uses_reported_remaining_capacity")}
                </p>
            )}
            <p>
                {forecast.collected_at
                    ? t("collected_at", {
                          date: localTime(forecast.collected_at),
                      })
                    : ""}
                {t("model_version", {
                    version: String(result.model_version ?? "—"),
                })}{" "}
                {forecast.configuration_id
                    ? t("configuration_id", { id: forecast.configuration_id })
                    : ""}
                {electrical &&
                forecast.source_kind === "estimated_actuals" &&
                !forecast.calibration_id
                    ? t("baseline_estimate_using_the_current_configuration_no")
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
            {(hasFlag(result, "mixed_forecast_vintages") ||
                forecast.source_kind === "mixed") &&
                Array.isArray(provenance?.source_snapshots) && (
                    <ul>
                        {provenance.source_snapshots.map((source) => (
                            <li key={source.snapshot_id}>
                                {t(
                                    source.source_kind === "estimated_actuals"
                                        ? "estimate_update"
                                        : "forecast_update",
                                    {
                                        id: source.snapshot_id,
                                        date: localTime(source.collected_at),
                                    },
                                )}
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
    );
}
