"use client";

import { useTranslations } from "next-intl";
import { Select } from "@/ui/form";
export type DataSource =
    "auto" | "forecast" | "estimated_actuals" | "archived_forecast";

export function DataSourceControl({
    value,
    onChange,
    allowArchivedForecast = false,
    archivedForecastDisabled = false,
}: {
    value: DataSource;
    onChange: (source: DataSource) => void;
    allowArchivedForecast?: boolean;
    archivedForecastDisabled?: boolean;
}) {
    const t = useTranslations("DataSource");

    return (
        <>
            <Select
                label={t("data_source")}
                value={value}
                onChange={(event) => onChange(event.target.value as DataSource)}
            >
                <option value="auto">{t("automatic")}</option>
                <option value="forecast">
                    {allowArchivedForecast
                        ? t("forecast_outlook")
                        : t("archived_forecast")}
                </option>
                {allowArchivedForecast && (
                    <option
                        value="archived_forecast"
                        disabled={archivedForecastDisabled}
                    >
                        {t("archived_forecast")}
                    </option>
                )}
                <option value="estimated_actuals">
                    {t("estimated_actuals")}
                </option>
            </Select>
            {value === "estimated_actuals" && (
                <span className="fine">
                    {t("elapsed_intervals_model_estimates_not_meter_readings")}
                </span>
            )}
            {value === "archived_forecast" && (
                <span className="fine">
                    {t("archived_forecast_description")}
                </span>
            )}
        </>
    );
}
