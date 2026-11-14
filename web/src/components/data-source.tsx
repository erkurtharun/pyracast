"use client";

import { useTranslations } from "next-intl";
export type DataSource =
    "auto" | "forecast" | "estimated_actuals" | "archived_forecast";

export function localDay(timezone: string, offset = 0): string {
    const today = new Date().toLocaleDateString("en-CA", {
        timeZone: timezone,
    });
    const date = new Date(`${today}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + offset);
    return date.toISOString().slice(0, 10);
}

export function DataSourceControl({
    value,
    onChange,
    allowArchivedForecast = false,
}: {
    value: DataSource;
    onChange: (source: DataSource) => void;
    allowArchivedForecast?: boolean;
}) {
    const t = useTranslations("DataSource");

    return (
        <>
            <label className="field">
                {t("data_source")}
                <select
                    value={value}
                    onChange={(event) =>
                        onChange(event.target.value as DataSource)
                    }
                >
                    <option value="auto">{t("automatic")}</option>
                    <option value="forecast">
                        {allowArchivedForecast
                            ? t("forecast_outlook")
                            : t("archived_forecast")}
                    </option>
                    {allowArchivedForecast && (
                        <option value="archived_forecast">
                            {t("archived_dayahead_forecast")}
                        </option>
                    )}
                    <option value="estimated_actuals">
                        {t("estimated_actuals")}
                    </option>
                </select>
            </label>
            {value === "estimated_actuals" && (
                <span className="fine">
                    {t("completed_local_days_only_model_estimates_not")}
                </span>
            )}
            {value === "archived_forecast" && (
                <span className="fine">
                    {t(
                        "saved_priorday_forecast_under_its_original_configuration",
                    )}
                </span>
            )}
        </>
    );
}
