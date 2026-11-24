"use client";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import { Suspense, useState } from "react";
import type { Plant, PlantScope, Site } from "@/lib/api";
import { useRefresh } from "@/lib/api/query";
import { plantDay } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import { useNavigate } from "@/lib/leave-guard";
import { sectionUnavailable } from "@/lib/on-demand";
import {
    plantView,
    routes,
    type PlantAddress,
    type PlantSection,
} from "@/lib/routes";
import { OwnPendingRequests } from "@/features/inbox/operational-inbox";
import { ViewNav } from "@/ui/action";
import { ExportPlant } from "@/ui/export-plant";
import { usePageTitle } from "@/ui/page-title";
import { Notice } from "@/ui/ui";
import { StatusTag } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { ReviewNotice } from "./review-notice";
import {
    ForecastSection,
    type ForecastOptions,
} from "./workspace/forecast-section";
import { latestPlant } from "./workspace/latest-plant";
import { usePlantNavigation } from "./workspace/navigation";
import { SettingsSection } from "./workspace/settings-section";

const Calibrations = dynamic(() =>
    import("@/features/calibration/calibrations").then(
        (module) => module.Calibrations,
        sectionUnavailable,
    ),
);
const Outages = dynamic(() =>
    import("./outages").then((module) => module.Outages, sectionUnavailable),
);
const ProductionSection = dynamic(() =>
    import("./workspace/production-section").then(
        (module) => module.ProductionSection,
        sectionUnavailable,
    ),
);

export function PlantWorkspace({
    plant,
    scope,
    timezone,
    region,
    section: tab,
    address,
}: {
    plant: Plant;
    scope: PlantScope;
    timezone: string;
    region: Site;
    section: PlantSection;
    address: PlantAddress;
}) {
    const t = useTranslations("PlantWorkspace");
    const format = useFormat();
    const canManage = useCanManageWorkspace();
    const navigate = useNavigate();
    const refresh = useRefresh();
    const navigation = usePlantNavigation(scope, tab, address);
    const forecastView = plantView("forecast", address);
    const [forecastOptions, setForecastOptions] = useState<ForecastOptions>({
        resolution: "60",
        source: "auto",
    });
    const [savedPlant, setSavedPlant] = useState<Plant>();
    const currentPlant = latestPlant(plant, savedPlant);
    if (savedPlant && currentPlant === plant) setSavedPlant(undefined);
    const portfolio = routes.portfolio(scope.tenant_id);
    const changed = () => refresh.workspace(scope.tenant_id);
    const configuration = currentPlant.current_configuration;
    // The review notice lists what happens next; an empty forecast would only repeat it.
    const awaitsFirstApproval = region.status === "pending" && !configuration;
    usePageTitle(`${t(tab)} · ${currentPlant.name}`);
    return (
        <ExportPlant
            value={{ id: currentPlant.id, name: currentPlant.name, timezone }}
        >
            <button className="back-button" onClick={() => navigate(portfolio)}>
                {t("allPlants")}
            </button>
            {!canManage && (tab === "production" || tab === "outages") && (
                <OwnPendingRequests
                    tenantId={scope.tenant_id}
                    plantId={plant.id}
                />
            )}
            <div className="page-heading plant-identity">
                <div>
                    <p className="eyebrow">{t("solarPlant")}</p>
                    <h1>{currentPlant.name}</h1>
                </div>
                <div className="button-group">
                    <StatusTag group="RegionStatus" value={region.status} />
                </div>
            </div>
            <p className="fine plant-time">
                {t("plantTime", { zone: format.zone(timezone) })}
            </p>
            <ReviewNotice region={region} scope={scope} timezone={timezone} />
            <ViewNav
                tabs
                label={t("plantSections")}
                busy={navigation.navigating}
                views={[
                    ["forecast", t("forecast")],
                    ["production", t("production")],
                    ["calibration", t("calibration")],
                    ["outages", t("outages")],
                    ["settings", t("settings")],
                ]}
                current={tab}
                onSelect={navigation.selectTab}
            />
            <Suspense fallback={<p role="status">{t("loadingSection")}</p>}>
                {tab === "forecast" ? (
                    awaitsFirstApproval ? null : (
                        <ForecastSection
                            scope={scope}
                            timezone={timezone}
                            configurationId={configuration?.id}
                            view={forecastView}
                            date={address.date ?? plantDay(timezone, 1)}
                            baseDate={navigation.base.date}
                            leaving={navigation.leaving("forecast")}
                            options={forecastOptions}
                            onOptions={setForecastOptions}
                            show={navigation.show}
                        />
                    )
                ) : tab === "outages" ? (
                    <Outages
                        configuration={configuration}
                        scope={scope}
                        timezone={timezone}
                        onChanged={changed}
                    />
                ) : tab === "settings" ? (
                    <SettingsSection
                        plant={currentPlant}
                        scope={scope}
                        region={region}
                        timezone={timezone}
                        view={plantView("settings", address)}
                        leaving={navigation.leaving("settings")}
                        show={navigation.show}
                        onSaved={setSavedPlant}
                        onChanged={changed}
                        onDeleted={async () => {
                            await changed();
                            navigate(portfolio);
                        }}
                    />
                ) : tab === "production" ? (
                    <ProductionSection
                        scope={scope}
                        timezone={timezone}
                        configurationId={configuration?.id}
                        view={plantView("production", address)}
                        day={address.date}
                        baseDay={navigation.base.date}
                        leaving={navigation.leaving("production")}
                        show={navigation.show}
                        keep={navigation.keep}
                        onSaved={changed}
                    />
                ) : !configuration ? (
                    <Notice>{t("calibrationUnavailable")}</Notice>
                ) : (
                    <Calibrations
                        key={`calibration-${configuration.id}`}
                        scope={scope}
                    />
                )}
            </Suspense>
        </ExportPlant>
    );
}
