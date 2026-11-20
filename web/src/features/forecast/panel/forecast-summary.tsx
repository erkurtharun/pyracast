"use client";
import { useTranslations } from "next-intl";
import type { Forecast } from "@/lib/api";
import { CsvDownload } from "@/ui/csv-download";
import { Notice, Stat, Tag } from "@/ui/ui";
import type { ForecastInterval as Interval } from "../forecast-intervals";
import { WeatherReferenceCard } from "../weather-reference";
import { useFormat } from "@/lib/format/use-format";
import {
    hasFlag,
    type DaylightSummary,
    type ForecastResult,
    type OutageScenario,
    type ProductionDiagnostic,
} from "./model";

export function ForecastSummary({
    forecast,
    result,
    electrical,
    scenario,
    diagnostic,
    affectedRows,
    csvRows,
}: {
    forecast: Forecast;
    result: ForecastResult;
    electrical: boolean;
    scenario: OutageScenario | null;
    diagnostic: ProductionDiagnostic | null | undefined;
    affectedRows: Interval[];
    csvRows: Record<string, unknown>[];
}) {
    const t = useTranslations("Forecast");
    const { number } = useFormat();
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
    const uncalibrated =
        diagnostic?.reference_kind === "uncalibrated_reference";
    const latestShortfall = diagnostic?.latest_day_status === "shortfall";
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
                    {forecast.source_kind === "mixed" && (
                        <p className="fine">
                            {t("updated_estimates_and_forecast_explanation")}
                        </p>
                    )}
                    {forecast.source_kind === "forecast" &&
                        hasFlag(result, "forecast_fallback") && (
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
                    {forecast.source_kind === "mixed" && (
                        <Tag>{t("updated_estimates_and_forecast")}</Tag>
                    )}
                    {hasFlag(result, "mixed_forecast_vintages") && (
                        <Tag>{t("mixed_forecast_updates")}</Tag>
                    )}
                    {hasFlag(result, "archived_forecast") && (
                        <Tag>{t("archived_forecast")}</Tag>
                    )}
                    {hasFlag(result, "provisional_production_calibration") && (
                        <Tag tone="warning">
                            {t("provisional_production_calibration")}
                        </Tag>
                    )}
                    {hasFlag(result, "unvalidated_reference_transfer") && (
                        <Tag tone="warning">{t("needs_validation")}</Tag>
                    )}
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
                (hasFlag(scenario.result, "outage_configuration_changed") ||
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
                hasFlag(result, "historical_configuration_assumed") && (
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
        </>
    );
}
