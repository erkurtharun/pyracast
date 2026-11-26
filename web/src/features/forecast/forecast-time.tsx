"use client";
import { useTranslations } from "next-intl";
import type { Forecast } from "@/lib/api";
import { useFormat } from "@/lib/format/use-format";
import { Select } from "@/ui/form";
import { Tag } from "@/ui/ui";

type Options = NonNullable<Forecast["selection"]>["options"];

export function ForecastTimeControl({
    timezone,
    options,
    forecastId,
    onChoose,
}: {
    timezone: string;
    options: Options;
    forecastId: number | undefined;
    onChoose: (forecastId: number | undefined) => void;
}) {
    const t = useTranslations("DataSource");
    const format = useFormat();
    return (
        <div className="forecast-time">
            {options.length > 0 && (
                <Select
                    label={t("forecast_time")}
                    value={forecastId === undefined ? "" : String(forecastId)}
                    onChange={(event) =>
                        onChoose(
                            event.target.value
                                ? Number(event.target.value)
                                : undefined,
                        )
                    }
                >
                    <option value="">{t("automatic")}</option>
                    {options.map((option) => (
                        <option
                            key={option.forecast_id}
                            value={option.forecast_id}
                        >
                            {format.time(option.fetched_at, timezone)}
                        </option>
                    ))}
                </Select>
            )}
            {forecastId !== undefined && (
                <>
                    <Tag tone="warning">{t("manual")}</Tag>
                    <button
                        type="button"
                        className="button secondary"
                        onClick={() => onChoose(undefined)}
                    >
                        {t("back_to_automatic")}
                    </button>
                </>
            )}
        </div>
    );
}
