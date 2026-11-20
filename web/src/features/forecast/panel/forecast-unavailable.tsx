"use client";
import { useTranslations } from "next-intl";
import type { Forecast } from "@/lib/api";
import { Empty, Tag } from "@/ui/ui";

export function ForecastUnavailable({
    forecast,
    electrical,
}: {
    forecast: Forecast;
    electrical: boolean;
}) {
    const t = useTranslations("Forecast");
    return (
        <>
            <Tag>{forecast.freshness}</Tag>
            {forecast.unavailable_reason === "estimated_actuals_unavailable" ? (
                <Empty title={t("no_estimated_actuals_for_this_day")}>
                    {t("estimated_actuals_are_not_available_for_this")}
                </Empty>
            ) : electrical &&
              forecast.unavailable_reason === "configuration_unavailable" ? (
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
}
