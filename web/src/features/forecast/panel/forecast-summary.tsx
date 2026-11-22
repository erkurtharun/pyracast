"use client";
import { useTranslations } from "next-intl";
import type { Forecast } from "@/lib/api";
import { forecastExport } from "@/lib/exports";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import { Notice, Stat, Tag } from "@/ui/ui";
import type { ForecastInterval as Interval } from "../forecast-intervals";
import { useFormat } from "@/lib/format/use-format";
import { Freshness } from "./freshness";
import {
    dayEnergy,
    hasFlag,
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
    rows,
}: {
    forecast: Forecast;
    result: ForecastResult;
    electrical: boolean;
    scenario: OutageScenario | null;
    diagnostic: ProductionDiagnostic | null | undefined;
    affectedRows: Interval[];
    rows: Interval[];
}) {
    const t = useTranslations("Forecast");
    const { number } = useFormat();
    const plant = useExportPlant();
    const daylight = result.daylight;
    const primary = daylight ?? result;
    const affectedDaylight = scenario?.result.daylight;
    const affectedSummary = affectedDaylight ?? scenario?.result;
    const noDaylight = daylight?.status === "polar_night";
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
                    <p className="fine">{forecast.date}</p>
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
                        label={
                            scenario
                                ? t("download_both_scenarios_csv")
                                : t("download_csv")
                        }
                        build={() =>
                            forecastExport(plant, forecast, rows, {
                                electrical,
                                scenario: Boolean(scenario),
                            })
                        }
                    />
                    <Freshness forecast={forecast} />
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
                            : dayEnergy(primary, electrical, partial)
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
                                : dayEnergy(
                                      affectedSummary,
                                      true,
                                      affectedPartial,
                                  )
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
