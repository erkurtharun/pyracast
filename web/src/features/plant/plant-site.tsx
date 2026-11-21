"use client";
import { useTranslations } from "next-intl";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import type { ReactNode } from "react";
import { cadence, useApiList, useRefresh } from "@/lib/api/query";
import { isPlantSection, plantAddress, routes } from "@/lib/routes";
import { PlantOnboarding } from "@/features/portfolio/plant-onboarding";
import { PlantWorkspace } from "./plant-workspace";
import { Notice, RefreshFailed } from "@/ui/ui";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { useWorkspace } from "@/features/workspace/workspace-shell";

export function PlantSite({
    siteId,
    children,
}: {
    siteId: number;
    children: ReactNode;
}) {
    const t = useTranslations("RegionWorkspace");
    const router = useRouter();
    const refresh = useRefresh();
    const canManage = useCanManageWorkspace();
    const { tenant } = useWorkspace();
    const params = useParams<{ section?: string }>();
    const section = isPlantSection(params.section)
        ? params.section
        : "forecast";
    const address = plantAddress(section, useSearchParams());
    const sites = useApiList(
        "/api/tenants/{tenant_id}/sites/",
        { path: { tenant_id: tenant.id } },
        { poll: cadence.background },
    );
    const site = sites.data?.results.find((row) => row.id === siteId);
    const back = () => router.push(routes.portfolio(tenant.id));
    if (site?.plant)
        return (
            <>
                <RefreshFailed resource={sites} />
                <PlantWorkspace
                    key={site.plant.id}
                    plant={site.plant}
                    scope={{
                        tenant_id: tenant.id,
                        region_id: site.id,
                        plant_id: site.plant.id,
                    }}
                    region={site}
                    timezone={site.timezone}
                    section={section}
                    address={address}
                />
                {children}
            </>
        );
    return (
        <>
            <button className="back-button" onClick={back}>
                {t("allPlants")}
            </button>
            {sites.loading ? (
                <p role="status">{t("loadingPlant")}</p>
            ) : !sites.data ? (
                <Notice error>{sites.error}</Notice>
            ) : !site ? (
                <Notice>{t("siteUnavailable")}</Notice>
            ) : canManage ? (
                <PlantOnboarding
                    existing={site}
                    tenant={tenant.id}
                    onCancel={back}
                    onCreated={() => void refresh.workspace(tenant.id)}
                />
            ) : (
                <Notice>{t("plantUnavailable")}</Notice>
            )}
            {children}
        </>
    );
}
