"use client";
import { useTranslations } from "next-intl";
import { useParams, useSearchParams } from "next/navigation";
import type { ReactNode } from "react";
import { cadence, useApiList, useRefresh } from "@/lib/api/query";
import { useNavigate } from "@/lib/leave-guard";
import { isPlantSection, plantAddress, routes } from "@/lib/routes";
import { PlantOnboarding } from "@/features/portfolio/plant-onboarding";
import { PlantWorkspace } from "./plant-workspace";
import { ReviewNotice } from "./review-notice";
import { Notice, ReadFailed, RefreshFailed } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";
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
    const navigate = useNavigate();
    const refresh = useRefresh();
    const canManage = useCanManageWorkspace();
    const label = useLabel();
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
    const back = () => navigate(routes.portfolio(tenant.id));
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
            <RefreshFailed resource={sites} />
            {sites.loading ? (
                <p role="status">{t("loadingPlant")}</p>
            ) : !sites.data ? (
                <ReadFailed resource={sites} />
            ) : !site ? (
                <Notice>{t("siteUnavailable")}</Notice>
            ) : site.edit_blocked_reason ? (
                <Notice>
                    {label("PlantEditBlock", site.edit_blocked_reason)}
                </Notice>
            ) : canManage ? (
                <PlantOnboarding
                    existing={site}
                    tenant={tenant.id}
                    onCancel={back}
                    onCreated={() => void refresh.workspace(tenant.id)}
                    onOpen={(id) => navigate(routes.plant(tenant.id, id))}
                />
            ) : site.status === "rejected" ? (
                <ReviewNotice
                    region={site}
                    scope={{ tenant_id: tenant.id, region_id: site.id }}
                />
            ) : (
                <Notice>{t("plantUnavailable")}</Notice>
            )}
            {children}
        </>
    );
}
