"use client";

import { useTranslations } from "next-intl";
import { Select } from "@/ui/form";
import type { DataSource } from "./date-bounds";

export type { DataSource } from "./date-bounds";

const SOURCES = [
    ["auto", "automatic"],
    ["forecast", "forecast_outlook"],
    ["archived_forecast", "archived_forecast"],
    ["estimated_actuals", "estimated_actuals"],
] as const satisfies readonly (readonly [DataSource, string])[];

export function DataSourceControl({
    value,
    onChange,
}: {
    value: DataSource;
    onChange: (source: DataSource) => void;
}) {
    const t = useTranslations("DataSource");

    return (
        <>
            <Select
                label={t("data_source")}
                value={value}
                onChange={(event) =>
                    onChange(
                        SOURCES.find(
                            ([source]) => source === event.target.value,
                        )?.[0] ?? "auto",
                    )
                }
            >
                {SOURCES.map(([source, label]) => (
                    <option key={source} value={source}>
                        {t(label)}
                    </option>
                ))}
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
