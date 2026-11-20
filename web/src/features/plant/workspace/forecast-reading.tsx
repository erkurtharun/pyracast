"use client";
import { useTranslations } from "next-intl";
import type { Forecast, PlantScope } from "@/lib/api";
import { useApiQuery, type Loaded } from "@/lib/api/query";
import { ForecastPanel } from "@/features/forecast/forecast-panel";
import type { DataSource } from "@/features/forecast/data-source";
import { Resource } from "@/ui/resource";

type ForecastQuery = {
    scope: PlantScope;
    date: string;
    resolution: 30 | 60;
    source: DataSource;
    poll: number | false;
};

export function PlantForecast({
    electrical,
    ...query
}: ForecastQuery & { electrical: boolean }) {
    return electrical ? (
        <GenerationForecast {...query} />
    ) : (
        <IrradianceForecast {...query} />
    );
}

function GenerationForecast({
    scope,
    date,
    resolution,
    source,
    poll,
}: ForecastQuery) {
    const resource = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/forecast/",
        {
            path: scope,
            query: {
                date,
                resolution_minutes: resolution,
                data_source: source,
            },
        },
        { poll },
    );
    return <ForecastResource resource={resource} electrical />;
}

function IrradianceForecast({ scope, date, source, poll }: ForecastQuery) {
    const resource = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/forecast/",
        {
            path: {
                tenant_id: scope.tenant_id,
                region_id: scope.region_id,
            },
            query: {
                date,
                data_source: source === "archived_forecast" ? "auto" : source,
            },
        },
        { poll },
    );
    return <ForecastResource resource={resource} electrical={false} />;
}

function ForecastResource({
    resource,
    electrical,
}: {
    resource: Loaded<Forecast>;
    electrical: boolean;
}) {
    const t = useTranslations("PlantWorkspace");
    return (
        <section className="panel">
            <Resource
                resource={resource}
                loading={
                    electrical ? t("loadingGeneration") : t("loadingIrradiance")
                }
            >
                {(forecast) => (
                    <ForecastPanel
                        forecast={forecast}
                        electrical={electrical}
                    />
                )}
            </Resource>
        </section>
    );
}
