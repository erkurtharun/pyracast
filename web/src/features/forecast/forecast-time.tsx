"use client";
import { useTranslations } from "next-intl";
import type { Forecast } from "@/lib/api";
import { useFormat } from "@/lib/format/use-format";
import { Select } from "@/ui/form";
import { Tag } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";

type Selection = NonNullable<Forecast["selection"]>;

export function ForecastTimeControl({
    timezone,
    options,
    rule,
    forecastId,
    onChoose,
}: {
    timezone: string;
    options: Selection["options"];
    rule: Selection["rule"] | undefined;
    forecastId: number | undefined;
    onChoose: (forecastId: number | undefined) => void;
}) {
    const t = useTranslations("DataSource");
    const format = useFormat();
    const label = useLabel();
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
                    <option value="">{t("auto")}</option>
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
            {forecastId === undefined ? (
                rule && <Tag>{label("SelectionRule", rule)}</Tag>
            ) : (
                <>
                    <Tag tone="warning">{t("manual")}</Tag>
                    <button
                        type="button"
                        className="button secondary"
                        onClick={() => onChoose(undefined)}
                    >
                        {t("back_to_auto")}
                    </button>
                </>
            )}
        </div>
    );
}
