"use client";

import { useTranslations } from "next-intl";
import { Select } from "@/ui/form";
import type { DataChoice } from "./date-bounds";

export type { DataSource } from "./date-bounds";

const CHOICES = [
    "forecast",
    "archived_forecast",
    "estimated_actuals",
] as const satisfies readonly DataChoice[];

export function DataSourceControl({
    value,
    electrical,
    onChange,
}: {
    value: DataChoice;
    electrical: boolean;
    onChange: (choice: DataChoice) => void;
}) {
    const t = useTranslations("DataSource");
    const label = (choice: DataChoice) =>
        choice === "estimated_actuals" && !electrical
            ? t("estimated_irradiance")
            : t(choice);
    return (
        <Select
            label={t("data_source")}
            value={value}
            onChange={(event) =>
                onChange(
                    CHOICES.find((choice) => choice === event.target.value) ??
                        "forecast",
                )
            }
        >
            {CHOICES.map((choice) => (
                <option key={choice} value={choice}>
                    {label(choice)}
                </option>
            ))}
        </Select>
    );
}
