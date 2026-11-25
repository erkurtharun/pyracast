"use client";
import { useTranslations } from "next-intl";
import type { Forecast } from "@/lib/api";
import { forecastExport } from "@/lib/exports";
import { contactEmail } from "@/lib/public-metadata";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import { Notice, Stat } from "@/ui/ui";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import type { ForecastInterval as Interval } from "../forecast-intervals";
import { useFormat } from "@/lib/format/use-format";
import {
    collectedAt,
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
    const choice = useTranslations("DataSource");
    const format = useFormat();
    const plant = useExportPlant();
    const canManage = useCanManageWorkspace();
    const satellite = forecast.source_kind === "estimated_actuals";
    const daylight = result.daylight;
    const primary = daylight ?? result;
    const affectedDaylight = scenario?.result.daylight;
    const affectedSummary = affectedDaylight ?? scenario?.result;
    const noDaylight = daylight?.status === "polar_night";
    const partial = primary.complete !== true && !noDaylight;
    const affectedPartial =
        affectedSummary?.complete !== true &&
        affectedDaylight?.status !== "polar_night";
    const latestShortfall = diagnostic?.latest_day_status === "shortfall";
    const collected = collectedAt(forecast, result);
    const unit = electrical ? "MWh" : "kWh/m²";
    return (
        <>
            <div className="section-heading">
                <div>
                    <h2>
                        {satellite
                            ? electrical
                                ? choice("estimated_actuals")
                                : choice("estimated_irradiance")
                            : electrical
                              ? t("electrical_generation")
                              : t("irradiance")}
                    </h2>
                    <p className="fine">{forecast.date}</p>
                    {forecast.freshness === "stale" && (
                        <p className="fine">
                            {forecast.stale_reason === "connection_inactive"
                                ? t("stale_reason.connection_inactive", {
                                      contact: contactEmail,
                                  })
                                : t("stale_reason.collection_overdue")}
                        </p>
                    )}
                    {collected && (
                        <p className="fine">
                            {t(
                                satellite
                                    ? "satellite_collected_at"
                                    : "collected_at",
                                {
                                    time: format.time(
                                        collected,
                                        forecast.timezone,
                                    ),
                                },
                            )}
                        </p>
                    )}
                </div>
                <div className="button-group">
                    <CsvDownload
                        label={t("download_csv")}
                        build={() =>
                            forecastExport(plant, forecast, rows, {
                                electrical,
                                scenario: Boolean(scenario),
                                names: {
                                    file: t(
                                        electrical
                                            ? "generation_file"
                                            : "irradiance_file",
                                    ),
                                    forecast: choice("forecast"),
                                    satellite: electrical
                                        ? choice("estimated_actuals")
                                        : choice("estimated_irradiance"),
                                },
                            })
                        }
                    />
                </div>
            </div>
            {diagnostic?.evidence_state === "stale" ? null : diagnostic &&
              (latestShortfall || diagnostic.status === "suspected") ? (
                <Notice>
                    {latestShortfall &&
                    diagnostic.latest_performance_index !== null &&
                    diagnostic.latest_matched_day
                        ? t("production_below_model", {
                              percent: 1 - diagnostic.latest_performance_index,
                              date: diagnostic.latest_matched_day,
                          })
                        : t("earlier_below_model")}
                </Notice>
            ) : null}
            {scenario?.status === "unknown_impact" && (
                <Notice>
                    {t("outage_impact_is_unknown_for_some_intervals")}
                    {canManage &&
                        ` ${t("enter_capacity_in_the_outage_record")}`}
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
            {noDaylight ? (
                <p className="fine">{t("no_daylight_on_this_date")}</p>
            ) : (
                partial && <Notice>{t("some_forecast_data_is_missing")}</Notice>
            )}
            <div className="stats forecast-stats">
                <Stat
                    label={
                        scenario ? t("normal_capacity") : t("estimated_energy")
                    }
                    value={partial ? undefined : dayEnergy(primary, electrical)}
                    unit={unit}
                />
                {scenario && (
                    <Stat
                        label={t("with_outages_title")}
                        value={
                            affectedPartial
                                ? undefined
                                : dayEnergy(affectedSummary, true)
                        }
                        unit="MWh"
                    />
                )}
            </div>
        </>
    );
}
