"use client";
import { useTranslations } from "next-intl";
import type { PlantScope } from "@/lib/api";
import { cadence } from "@/lib/api/query";
import type { PlantAddress, PlantView } from "@/lib/routes";
import { DataSourceControl } from "@/features/forecast/data-source";
import {
    clampDate,
    dateBounds,
    type DataSource,
} from "@/features/forecast/date-bounds";
import { ViewNav } from "@/ui/action";
import { plantDay } from "@/lib/format/time";
import { ControlField, Select } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { PlantForecast } from "./forecast-reading";

export type ForecastOptions = { resolution: "30" | "60"; source: DataSource };

export function ForecastSection({
    scope,
    timezone,
    configurationId,
    view,
    date,
    baseDate,
    leaving,
    options,
    onOptions,
    show,
}: {
    scope: PlantScope;
    timezone: string;
    configurationId: number | undefined;
    view: PlantView<"forecast">;
    date: string;
    baseDate: string | undefined;
    leaving: boolean;
    options: ForecastOptions;
    onOptions: (options: ForecastOptions) => void;
    show: (next: PlantAddress) => void;
}) {
    const t = useTranslations("PlantWorkspace");
    const { resolution, source } = options;
    const today = plantDay(timezone);
    const bounds = dateBounds(source, today);
    const leavingGeneration = leaving && view === "generation";
    const setDate = (next: string) => show({ date: next });
    return (
        <>
            <ViewNav
                label={t("forecastViews")}
                views={[
                    ["generation", t("generation")],
                    ["irradiance", t("irradiance")],
                ]}
                current={view}
                onSelect={(next) => show({ view: next })}
            />
            <div className="forecast-controls" aria-busy={leaving || undefined}>
                <ControlField label={t("forecastDate")}>
                    <input
                        type="date"
                        value={date}
                        min={bounds.min}
                        max={bounds.max}
                        onChange={(event) => setDate(event.target.value)}
                    />
                </ControlField>
                <DataSourceControl
                    value={source}
                    onChange={(next) => {
                        onOptions({ ...options, source: next });
                        const current = baseDate || plantDay(timezone, 1);
                        const clamped = clampDate(
                            current,
                            dateBounds(next, today),
                        );
                        if (clamped !== current) setDate(clamped);
                    }}
                />
                {view === "generation" && (
                    <Select
                        label={t("resolution")}
                        value={resolution}
                        disabled={leavingGeneration}
                        onChange={(event) =>
                            onOptions({
                                ...options,
                                resolution:
                                    event.target.value === "30" ? "30" : "60",
                            })
                        }
                    >
                        <option value="30">{t("minutes30")}</option>
                        <option value="60">{t("minutes60")}</option>
                    </Select>
                )}
            </div>
            {clampDate(date, bounds) !== date ? (
                <Notice>{t("selectDateInRange", bounds)}</Notice>
            ) : (
                <PlantForecast
                    key={`${view}-${date}-${resolution}-${source}-${configurationId}`}
                    scope={scope}
                    electrical={view === "generation"}
                    date={date}
                    resolution={resolution === "30" ? 30 : 60}
                    source={source}
                    poll={
                        source !== "archived_forecast" &&
                        date >= plantDay(timezone, -1)
                            ? cadence.live
                            : false
                    }
                />
            )}
        </>
    );
}
