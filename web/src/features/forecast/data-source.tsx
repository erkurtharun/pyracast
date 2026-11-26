"use client";

import { useTranslations } from "next-intl";
import { ChoiceGroup } from "@/ui/choice-group";
import type { DataSource } from "./date-bounds";

export function DataSourceControl({
    value,
    onChange,
}: {
    value: DataSource;
    onChange: (source: DataSource) => void;
}) {
    const t = useTranslations("DataSource");
    return (
        <ChoiceGroup
            label={t("data_source")}
            choices={[
                ["forecast", t("forecast")],
                ["satellite", t("satellite")],
            ]}
            value={value}
            onChange={onChange}
        />
    );
}
