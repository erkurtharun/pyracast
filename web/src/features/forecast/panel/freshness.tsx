"use client";
import { useTranslations } from "next-intl";
import type { Forecast } from "@/lib/api";
import { StatusTag } from "@/ui/vocabulary";

export function Freshness({ forecast }: { forecast: Forecast }) {
    const t = useTranslations("Forecast");
    return (
        <>
            <StatusTag group="Freshness" value={forecast.freshness} />
            {forecast.stale_reason && (
                <span className="fine">
                    {t(`stale_reason.${forecast.stale_reason}`)}
                </span>
            )}
        </>
    );
}
