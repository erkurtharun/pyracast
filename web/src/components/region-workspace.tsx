"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import type { Region, Plant } from "@/lib/api";
import { Notice, useCollection } from "./ui";
import { PlantWorkspace } from "./plant-workspace";
import { useCanManageWorkspace } from "./workspace-access";
import { PlantOnboarding } from "./plant-onboarding";

export function RegionWorkspace({
    region,
    tenant,
    onBack,
    onDeleted,
    initialPlant,
}: {
    region: Region;
    tenant: number;
    onBack: () => void;
    onDeleted: () => void;
    initialPlant?: Plant;
}) {
    const t = useTranslations("RegionWorkspace");
    const canManage = useCanManageWorkspace();

    const scope = { tenant_id: tenant, region_id: region.id };
    const plants = useCollection(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/",
        { path: scope },
        0,
        30_000,
    );
    const [created, setCreated] = useState<Plant>();
    const plant = plants.data?.results[0] ?? created ?? initialPlant;
    if (plant)
        return (
            <>
                {plants.error && (
                    <Notice error>
                        {t("refreshFailed", { error: plants.error })}
                    </Notice>
                )}
                <PlantWorkspace
                    key={plant.id}
                    plant={plant}
                    scope={{ ...scope, plant_id: plant.id }}
                    region={region}
                    timezone={region.timezone}
                    onBack={onBack}
                    onDeleted={onDeleted}
                />
            </>
        );
    return (
        <>
            <button className="back-button" onClick={onBack}>
                {t("allPlants")}
            </button>
            {plants.loading ? (
                <p role="status">{t("loadingPlant")}</p>
            ) : plants.error ? (
                <Notice error>{plants.error}</Notice>
            ) : canManage ? (
                <PlantOnboarding
                    existing={region}
                    tenant={tenant}
                    onCancel={onBack}
                    onCreated={(site) => {
                        if (site.plant) setCreated(site.plant);
                    }}
                />
            ) : (
                <Notice>{t("plantUnavailable")}</Notice>
            )}
        </>
    );
}
