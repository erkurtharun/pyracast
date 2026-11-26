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
import { ForecastSummary } from "./panel/forecast-summary";
import { ForecastUnavailable } from "./panel/forecast-unavailable";
import {
    chartIntervals,
    hourly,
    scenarioIntervals,
    type ForecastResult,
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
    if (!forecast.result)
        return (
            <ForecastUnavailable forecast={forecast} electrical={electrical} />
        );
    const scenario = (electrical ? forecast.outage_scenario : null) ?? null;
    const diagnostic = electrical ? forecast.production_diagnostic : null;
    const result: ForecastResult = forecast.result;
    const affectedRows: Interval[] = scenario?.result.intervals ?? [];
    const intervals: Interval[] = result.intervals ?? [];
    const allRows = scenarioIntervals(intervals, scenario && affectedRows);
    const metric = electrical ? "mean_power_mw" : "ghi_wm2",
        unit = electrical ? "MW" : "W/m²";
    const rows = chartIntervals(allRows, metric, showFullDay, result);
    const time = format.intervals(
        forecast.timezone,
        allRows.map((row) => row.start),
    );
    const scenarioLabel = t("with_outages_title");
    const satellite = forecast.source_kind === "estimated_actuals";
    return (
        <>
            <ForecastSummary
                forecast={forecast}
                result={result}
                electrical={electrical}
                scenario={scenario}
                diagnostic={diagnostic}
                affectedRows={affectedRows}
                rows={allRows}
            />
            <CheckboxField
                label={t("show_full_day")}
                checked={showFullDay}
                onChange={setShowFullDay}
            />
            {rows.length > 0 && (
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
                        satellite={satellite}
                        scenarioLabel={scenario ? scenarioLabel : undefined}
                        label={
                            hourly(result, intervals)
                                ? t("hourly_values")
                                : t("half_hourly_values")
                        }
                        formatTime={time}
                    />
                </>
            )}
        </>
    );
}
