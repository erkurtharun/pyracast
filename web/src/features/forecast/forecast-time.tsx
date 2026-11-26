"use client";
import { useTranslations } from "next-intl";
import type { Forecast } from "@/lib/api";
import { useFormat } from "@/lib/format/use-format";
import { Select } from "@/ui/form";
import { Tag } from "@/ui/ui";

type Options = NonNullable<Forecast["selection"]>["options"];

export type ForecastTime = "auto" | "best" | number;

export function ForecastTimeControl({
    timezone,
    options,
    offersBest,
    value,
    onChoose,
}: {
    timezone: string;
    options: Options;
    offersBest: boolean;
    value: ForecastTime;
    onChoose: (next: ForecastTime) => void;
}) {
    const t = useTranslations("DataSource");
    const format = useFormat();
    return (
        <div className="forecast-time">
            {options.length > 0 && (
                <Select
                    label={t("forecast_time")}
                    value={value === "auto" ? "" : String(value)}
                    onChange={(event) =>
                        onChoose(
                            event.target.value === ""
                                ? "auto"
                                : event.target.value === "best"
                                  ? "best"
                                  : Number(event.target.value),
                        )
                    }
                >
                    <option value="">{t("automatic")}</option>
                    {offersBest && <option value="best">{t("best")}</option>}
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
            {typeof value === "number" && (
                <>
                    <Tag tone="warning">{t("manual")}</Tag>
                    <button
                        type="button"
                        className="button secondary"
                        onClick={() => onChoose("auto")}
                    >
                        {t("back_to_automatic")}
                    </button>
                </>
            )}
        </div>
    );
}
