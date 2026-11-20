"use client";
import { useLocale, useTranslations } from "next-intl";
import { useCodeLabel } from "@/ui/vocabulary";

export function useFlagLabel() {
    const t = useTranslations("Forecast.qualityFlags");
    const locale = useLocale();
    const code = useCodeLabel("Forecast.qualityFlags");
    return (flag: string) =>
        code(
            flag,
            t("unknown", {
                value: locale === "en" ? flag.replaceAll("_", " ") : flag,
            }),
        );
}
