"use client";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import { useState } from "react";
import type { Forecast } from "@/lib/api";
import { CheckboxField } from "@/ui/form";
import {
    ForecastIntervals,
    type ForecastInterval as Interval,
} from "./forecast-intervals";
import { ForecastChart } from "./panel/forecast-chart";
import { ForecastProvenance } from "./panel/forecast-provenance";
import { ForecastSummary } from "./panel/forecast-summary";
import { ForecastUnavailable } from "./panel/forecast-unavailable";
import {
    chartIntervals,
    forecastCsvRows,
    scenarioIntervals,
    type ForecastResult,
    type OutageScenario,
    type ProductionDiagnostic,
} from "./panel/model";

export function ForecastPanel({
    forecast,
    electrical = false,
}: {
    forecast: Forecast;
    electrical?: boolean;
}) {
    const t = useTranslations("Forecast");
    const format = useFormat();
    const [showFullDay, setShowFullDay] = useState(false);
    if (
        !forecast.result ||
        typeof forecast.result !== "object" ||
        Array.isArray(forecast.result)
    )
        return (
            <ForecastUnavailable forecast={forecast} electrical={electrical} />
        );
    const scenarios = forecast as Forecast & {
        outage_scenario?: OutageScenario | null;
        production_diagnostic?: ProductionDiagnostic | null;
    };
    const scenario = (electrical ? scenarios.outage_scenario : null) ?? null;
    const diagnostic = electrical ? scenarios.production_diagnostic : null;
    const result = forecast.result as ForecastResult;
    const affectedRows = (
        scenario && Array.isArray(scenario.result.intervals)
            ? scenario.result.intervals
            : []
    ) as Interval[];
    const allRows = scenarioIntervals(
        (Array.isArray(result.intervals) ? result.intervals : []) as Interval[],
        scenario && affectedRows,
    );
    const metric = electrical ? "mean_power_mw" : "ghi_wm2",
        unit = electrical ? "MW" : "W/m²";
    const rows = chartIntervals(allRows, metric, showFullDay, result);
    const intervalTime = format.intervals(
        forecast.timezone,
        allRows.map((row) => row.start),
    );
    const time = (value: string) => intervalTime.format(new Date(value));
    const scenarioLabel = t("with_outages_title");
    return (
        <>
            <ForecastSummary
                forecast={forecast}
                result={result}
                electrical={electrical}
                scenario={scenario}
                diagnostic={diagnostic}
                affectedRows={affectedRows}
                csvRows={forecastCsvRows(allRows, Boolean(scenario))}
            />
            <CheckboxField
                label={t("show_full_day")}
                checked={showFullDay}
                onChange={setShowFullDay}
            />
            {rows.length === 0 ? (
                <p>{t("no_interval_data_available")}</p>
            ) : (
                <>
                    <ForecastChart
                        rows={rows}
                        metric={metric}
                        unit={unit}
                        electrical={electrical}
                        scenario={Boolean(scenario)}
                        scenarioLabel={scenarioLabel}
                        time={time}
                    />
                    <ForecastIntervals
                        rows={rows}
                        electrical={electrical}
                        scenarioLabel={scenario ? scenarioLabel : undefined}
                        showSources={forecast.source_kind === "mixed"}
                        label={
                            forecast.source_kind === "mixed"
                                ? t("updated_estimates_and_forecast")
                                : forecast.source_kind === "estimated_actuals"
                                  ? t("estimated_actual_intervals")
                                  : t("forecast_intervals")
                        }
                        formatTime={time}
                    />
                </>
            )}
            <ForecastProvenance
                forecast={forecast}
                result={result}
                electrical={electrical}
                scenario={Boolean(scenario)}
                diagnostic={diagnostic}
            />
        </>
    );
}
