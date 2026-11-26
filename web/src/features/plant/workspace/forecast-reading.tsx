"use client";
import { useTranslations } from "next-intl";
import type { Forecast, PlantScope } from "@/lib/api";
import { useApiQuery, type Loaded } from "@/lib/api/query";
import { ForecastPanel } from "@/features/forecast/forecast-panel";
import type { DataSource } from "@/features/forecast/date-bounds";
import { Resource } from "@/ui/resource";

export type ForecastQuery = {
    scope: PlantScope;
    electrical: boolean;
    date: string;
    resolution: 30 | 60;
    source: DataSource;
    forecastId?: number;
    poll: number | false;
};

export function useForecastRead(
    {
        scope,
        electrical,
        date,
        resolution,
        source,
        forecastId,
        poll,
    }: ForecastQuery,
    enabled = true,
): Loaded<Forecast> {
    const generation = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/forecast/",
        {
            path: scope,
            query: {
                date,
                resolution_minutes: resolution,
                data_source: source,
                forecast_id: forecastId,
            },
        },
        { poll, enabled: enabled && electrical },
    );
    const irradiance = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/forecast/",
        {
            path: {
                tenant_id: scope.tenant_id,
                region_id: scope.region_id,
            },
            query: {
                date,
                data_source: source,
                forecast_id: forecastId,
            },
        },
        { poll, enabled: enabled && !electrical },
    );
    return electrical ? generation : irradiance;
}

export function PlantForecast(query: ForecastQuery) {
    const t = useTranslations("PlantWorkspace");
    const resource = useForecastRead(query);
    return (
        <section className="panel">
            <Resource
                resource={resource}
                loading={
                    query.electrical
                        ? t("loadingGeneration")
                        : t("loadingIrradiance")
                }
            >
                {(forecast) => (
                    <ForecastPanel
                        forecast={forecast}
                        electrical={query.electrical}
                    />
                )}
            </Resource>
        </section>
    );
}
