"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { type Region, type Plant } from "@/lib/api";
import { Notice, useCollection } from "./ui";
import { PlantWorkspace } from "./plant-workspace";
import { PlantOnboarding } from "./plant-onboarding";

export function RegionWorkspace({
    region,
    tenant,
    token,
    onBack,
    onDeleted,
    initialPlant,
}: {
    region: Region;
    tenant: number;
    token: string;
    onBack: () => void;
    onDeleted: () => void;
    initialPlant?: Plant;
}) {
    const t = useTranslations("RegionWorkspace");

    const base = `/api/tenants/${tenant}/regions/${region.id}/`;
    const plants = useCollection<Plant>(`${base}plants/`, 0, 30_000);
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
                    base={`${base}plants/${plant.id}/`}
                    region={region}
                    regionBase={base}
                    timezone={region.timezone}
                    token={token}
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
            ) : (
                <PlantOnboarding
                    existing={region}
                    tenant={tenant}
                    token={token}
                    onCancel={onBack}
                    onCreated={(site) => {
                        if (site.plant) setCreated(site.plant);
                    }}
                />
            )}
        </>
    );
}
