"use client";
import { useTranslations } from "next-intl";
import type { PlantScope } from "@/lib/api";
import { cadence } from "@/lib/api/query";
import type { PlantAddress, PlantView } from "@/lib/routes";
import {
    DataSourceControl,
    type DataSource,
} from "@/features/forecast/data-source";
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
    const pastOnly =
        source === "estimated_actuals" || source === "archived_forecast";
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
                        max={pastOnly ? today : undefined}
                        onChange={(event) => setDate(event.target.value)}
                    />
                </ControlField>
                <DataSourceControl
                    allowArchivedForecast={view === "generation"}
                    archivedForecastDisabled={leavingGeneration}
                    value={source}
                    onChange={(next) => {
                        onOptions({ ...options, source: next });
                        if (
                            (next === "estimated_actuals" ||
                                next === "archived_forecast") &&
                            (baseDate || plantDay(timezone, 1)) > today
                        )
                            setDate(today);
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
                <span className="fine">{t("localDates")}</span>
            </div>
            {source === "estimated_actuals" && date > today ? (
                <Notice>{t("selectTodayOrPastDay")}</Notice>
            ) : source === "archived_forecast" && date > today ? (
                <Notice>{t("selectArchivedDay")}</Notice>
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
