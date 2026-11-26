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
import { ForecastTimeControl } from "@/features/forecast/forecast-time";
import { ViewNav } from "@/ui/action";
import { plantDay } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import { ControlField, Select } from "@/ui/form";
import { Notice } from "@/ui/ui";
import {
    PlantForecast,
    useForecastRead,
    type ForecastQuery,
} from "./forecast-reading";

export type ForecastOptions = {
    resolution: "30" | "60";
    source: DataSource;
    chosen?: { date: string; forecastId: number };
};

export function ForecastSection({
    scope,
    timezone,
    configurationId,
    view,
    date,
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
    leaving: boolean;
    options: ForecastOptions;
    onOptions: (options: ForecastOptions) => void;
    show: (next: PlantAddress) => void;
}) {
    const t = useTranslations("PlantWorkspace");
    const format = useFormat();
    const { resolution, source, chosen } = options;
    const today = plantDay(timezone);
    const bounds = dateBounds(source, today);
    const leavingGeneration = leaving && view === "generation";
    const read: ForecastQuery = {
        scope,
        electrical: view === "generation",
        date,
        resolution: resolution === "30" ? 30 : 60,
        source,
        poll: date >= plantDay(timezone, -1) ? cadence.live : false,
    };
    const chosenId = source === "forecast" ? chosen?.forecastId : undefined;
    const refused = useForecastRead(
        { ...read, forecastId: chosenId },
        false,
    ).invalidFields.includes("forecast_id");
    const forecastId = refused ? undefined : chosenId;
    const query: ForecastQuery = { ...read, forecastId };
    const shown = useForecastRead(query, false).data;
    const automatic = useForecastRead(
        { ...query, forecastId: undefined },
        false,
    ).data;
    const selection = (shown ?? automatic)?.selection;
    const times = source === "forecast" ? (selection?.options ?? []) : [];
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
                        onChange={(event) => show({ date: event.target.value })}
                    />
                </ControlField>
                <DataSourceControl
                    value={source}
                    onChange={(next) => onOptions({ ...options, source: next })}
                />
                {(times.length > 0 || forecastId !== undefined) && (
                    <ForecastTimeControl
                        timezone={timezone}
                        options={times}
                        forecastId={forecastId}
                        onChoose={(next) =>
                            onOptions({
                                ...options,
                                chosen:
                                    next === undefined
                                        ? undefined
                                        : { date, forecastId: next },
                            })
                        }
                    />
                )}
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
                <Notice>
                    {t("selectDateInRange", {
                        min: format.day(bounds.min),
                        max: format.day(bounds.max),
                    })}
                </Notice>
            ) : (
                <PlantForecast
                    key={`${view}-${date}-${resolution}-${source}-${forecastId}-${configurationId}`}
                    {...query}
                />
            )}
        </>
    );
}
