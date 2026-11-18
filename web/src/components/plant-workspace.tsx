"use client";
import { useTranslations, useFormatter } from "next-intl";
import { useEffect, useState } from "react";
import { useCanManageWorkspace } from "./workspace-access";
import { Reminders } from "./reminders";
import { CollectionSchedule } from "./collection-schedule";
import { RegionSettings } from "./region-settings";
import { CsvDownload } from "./csv-download";
import type { Forecast, Plant, PlantScope, Region } from "@/lib/api";
import { api } from "@/lib/api/client";
import { Calibrations } from "./calibrations";
import { ActionForm, Notice, Tag, useResource } from "./ui";
import { ProductionHistory } from "./production-history";
import { MoveToRecycleBin } from "./recycle-bin";
import { ForecastPanel } from "./forecast-panel";
import { ForecastPerformance } from "./forecast-performance";
import { Outages } from "./outages";
import { OwnPendingRequests } from "./operational-inbox";
import {
    PlantConfigurationFields,
    configurationInput,
} from "./plant-configuration-fields";
import { DataSourceControl, localDay, type DataSource } from "./data-source";
export function PlantWorkspace({
    plant,
    scope,
    timezone,
    region,
    onBack,
    onDeleted,
}: {
    plant: Plant;
    scope: PlantScope;
    timezone: string;
    region: Region;
    onBack: () => void;
    onDeleted: () => void;
}) {
    const t = useTranslations("PlantWorkspace");
    const format = useFormatter();
    const canManage = useCanManageWorkspace();

    const [tab, setTab] = useState("forecast"),
        [forecastView, setForecastView] = useState("generation"),
        [productionView, setProductionView] = useState("production"),
        [settingsView, setSettingsView] = useState("configuration"),
        [date, setDate] = useState(() => localDay(timezone, 1)),
        [resolution, setResolution] = useState("60"),
        [source, setSource] = useState<DataSource>("auto");
    const [productionRevision, setProductionRevision] = useState(0);
    const [outageRevision, setOutageRevision] = useState(0);
    const [savedPlant, setSavedPlant] = useState<Plant>();
    const currentPlant = latestPlant(plant, savedPlant);
    if (savedPlant && currentPlant === plant) setSavedPlant(undefined);
    useEffect(() => {
        const sync = () => {
            const match = window.location.hash.match(
                /^#plant-\d+\/(forecast|production|calibration|outages|settings)/,
            );
            setTab(match?.[1] ?? "forecast");
        };
        sync();
        window.addEventListener("hashchange", sync);
        return () => window.removeEventListener("hashchange", sync);
    }, []);
    const selectTab = (next: string) => {
        setTab(next);
        window.history.pushState(null, "", `#plant-${region.id}/${next}`);
    };
    const today = localDay(timezone);
    const configuration = currentPlant.current_configuration;
    const metadata = configuration ?? currentPlant.submission;
    return (
        <>
            <button className="back-button" onClick={onBack}>
                {t("allPlants")}
            </button>
            {!canManage && (tab === "production" || tab === "outages") && (
                <OwnPendingRequests
                    tenantId={scope.tenant_id}
                    plantId={plant.id}
                    revision={productionRevision + outageRevision}
                />
            )}
            <div className="page-heading plant-identity">
                <div>
                    <p className="eyebrow">{t("solarPlant")}</p>
                    <h1>{currentPlant.name}</h1>
                </div>
                <div className="button-group">
                    <Tag>{region.status}</Tag>
                </div>
            </div>
            {region.status !== "approved" && (
                <Notice>
                    {region.status === "pending"
                        ? t("awaitingApproval")
                        : t("approvalStatus", {
                              status: t(`approval_${region.status}`),
                          })}
                </Notice>
            )}
            <nav className="tabs" aria-label={t("plantSections")}>
                {(
                    [
                        ["forecast", t("forecast")],
                        ["production", t("production")],
                        ["calibration", t("calibration")],
                        ["outages", t("outages")],
                        ["settings", t("settings")],
                    ] as const
                ).map(([key, label]) => (
                    <button
                        key={key}
                        className={tab === key ? "active" : ""}
                        aria-current={tab === key ? "page" : undefined}
                        onFocus={(event) =>
                            event.currentTarget.scrollIntoView({
                                block: "nearest",
                                inline: "nearest",
                            })
                        }
                        onClick={() => selectTab(key)}
                    >
                        {label}
                    </button>
                ))}
            </nav>
            {tab === "forecast" ? (
                <>
                    <nav className="subnav" aria-label={t("forecastViews")}>
                        {(
                            [
                                ["generation", t("generation")],
                                ["irradiance", t("irradiance")],
                            ] as const
                        ).map(([key, label]) => (
                            <button
                                key={key}
                                className={forecastView === key ? "active" : ""}
                                aria-current={
                                    forecastView === key ? "page" : undefined
                                }
                                onClick={() => {
                                    setForecastView(key);
                                    if (
                                        key === "irradiance" &&
                                        source === "archived_forecast"
                                    )
                                        setSource("auto");
                                }}
                            >
                                {label}
                            </button>
                        ))}
                    </nav>
                    <div className="forecast-controls">
                        <label className="field">
                            {t("forecastDate")}
                            <input
                                type="date"
                                value={date}
                                max={
                                    source === "estimated_actuals"
                                        ? today
                                        : source === "archived_forecast"
                                          ? today
                                          : undefined
                                }
                                onChange={(event) =>
                                    setDate(event.target.value)
                                }
                            />
                        </label>
                        <DataSourceControl
                            allowArchivedForecast={
                                forecastView === "generation"
                            }
                            value={source}
                            onChange={(next) => {
                                setSource(next);
                                if (
                                    next === "estimated_actuals" &&
                                    date > today
                                )
                                    setDate(today);
                                if (
                                    next === "archived_forecast" &&
                                    date > today
                                )
                                    setDate(today);
                            }}
                        />
                        {forecastView === "generation" && (
                            <label className="field">
                                {t("resolution")}
                                <select
                                    value={resolution}
                                    onChange={(event) =>
                                        setResolution(event.target.value)
                                    }
                                >
                                    <option value="30">{t("minutes30")}</option>
                                    <option value="60">{t("minutes60")}</option>
                                </select>
                            </label>
                        )}
                        <span className="fine">
                            {t("localDates", { timezone })}
                        </span>
                    </div>
                    {source === "estimated_actuals" && date > today ? (
                        <Notice>{t("selectTodayOrPastDay")}</Notice>
                    ) : source === "archived_forecast" && date > today ? (
                        <Notice>{t("selectArchivedDay")}</Notice>
                    ) : (
                        <PlantForecast
                            key={`${forecastView}-${date}-${resolution}-${source}-${configuration?.id}-${outageRevision}`}
                            scope={scope}
                            electrical={forecastView === "generation"}
                            date={date}
                            resolution={resolution === "30" ? 30 : 60}
                            source={source}
                        />
                    )}
                </>
            ) : tab === "outages" ? (
                <Outages
                    configuration={configuration}
                    scope={scope}
                    timezone={timezone}
                    onChanged={() => setOutageRevision((value) => value + 1)}
                />
            ) : tab === "settings" ? (
                <>
                    <details>
                        <summary>{t("location")}</summary>
                        <p>
                            {t("locationSummary", {
                                latitude: format.number(region.latitude, {
                                    maximumFractionDigits: 6,
                                }),
                                longitude: format.number(region.longitude, {
                                    maximumFractionDigits: 6,
                                }),
                                timezone,
                            })}
                        </p>
                        <CsvDownload
                            label={t("downloadPlantCSV")}
                            filename={`plant-${currentPlant.id}`}
                            records={[currentPlant]}
                            metadata={{ timezone }}
                        />
                    </details>
                    <nav className="subnav" aria-label={t("plantSettings")}>
                        {(
                            [
                                ["configuration", t("technicalDetails")],
                                ["location", t("location")],
                                ["collection", t("dataCollection")],
                                ["reminders", t("emailReminders")],
                            ] as const
                        ).map(([key, label]) => (
                            <button
                                key={key}
                                className={settingsView === key ? "active" : ""}
                                aria-current={
                                    settingsView === key ? "page" : undefined
                                }
                                onClick={() => setSettingsView(key)}
                            >
                                {label}
                            </button>
                        ))}
                    </nav>
                    {settingsView === "configuration" ? (
                        <section className="panel">
                            <h2>{t("technicalDetails")}</h2>
                            {metadata ? (
                                <ActionForm
                                    submit={t("saveConfiguration")}
                                    onSubmit={async (data) => {
                                        setSavedPlant(
                                            await api.put(
                                                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/",
                                                {
                                                    path: scope,
                                                    body: configurationInput(
                                                        data,
                                                    ),
                                                },
                                            ),
                                        );
                                    }}
                                >
                                    <PlantConfigurationFields
                                        name={currentPlant.name}
                                        configuration={metadata}
                                    />
                                    {configuration && (
                                        <p className="fine">
                                            {t("calibrationChangeNote")}
                                        </p>
                                    )}
                                </ActionForm>
                            ) : (
                                <Notice>{t("approvalDetailsRequired")}</Notice>
                            )}
                        </section>
                    ) : settingsView === "location" ? (
                        <RegionSettings region={region} scope={scope} />
                    ) : settingsView === "collection" ? (
                        <CollectionSchedule scope={scope} region={region} />
                    ) : (
                        <Reminders scope={scope} region={region} />
                    )}
                    {canManage && (
                        <section className="panel">
                            <h2>{t("deletePlant")}</h2>
                            <p>{t("deletionHelp")}</p>
                            <MoveToRecycleBin
                                name={currentPlant.name}
                                scope={scope}
                                onDeleted={onDeleted}
                            />
                        </section>
                    )}
                </>
            ) : tab === "production" ? (
                <>
                    <nav className="subnav" aria-label={t("productionViews")}>
                        {(
                            [
                                ["production", t("actuals")],
                                ["comparison", t("comparison")],
                            ] as const
                        ).map(([key, label]) => (
                            <button
                                key={key}
                                className={
                                    productionView === key ? "active" : ""
                                }
                                aria-current={
                                    productionView === key ? "page" : undefined
                                }
                                onClick={() => setProductionView(key)}
                            >
                                {label}
                            </button>
                        ))}
                    </nav>
                    {productionView === "comparison" ? (
                        <ForecastPerformance
                            key={configuration?.id}
                            scope={scope}
                            timezone={timezone}
                        />
                    ) : (
                        <ProductionHistory
                            key={`history-${configuration?.id}`}
                            scope={scope}
                            configuration={configuration?.id}
                            timezone={timezone}
                            onSaved={() =>
                                setProductionRevision((current) => current + 1)
                            }
                        />
                    )}
                </>
            ) : !configuration ? (
                <Notice>{t("calibrationUnavailable")}</Notice>
            ) : (
                <Calibrations
                    key={`calibration-${configuration.id}`}
                    scope={scope}
                    currentConfigurationId={configuration.id}
                    productionRevision={productionRevision}
                />
            )}
        </>
    );
}
function latestPlant(server: Plant, saved?: Plant): Plant {
    if (!saved) return server;
    const incoming = server.current_configuration;
    const edited = saved.current_configuration;
    if (
        incoming &&
        (!edited ||
            incoming.revision > edited.revision ||
            (incoming.id === edited.id && server.name === saved.name))
    )
        return server;
    const incomingSubmission = server.submission;
    const editedSubmission = saved.submission;
    if (
        !edited &&
        !incoming &&
        incomingSubmission &&
        editedSubmission &&
        server.name === saved.name &&
        (
            [
                "dc_capacity_mw",
                "inverter_capacity_mw",
                "export_limit_mw",
                "measurement_basis",
                "mounting_type",
                "surface_tilt",
                "surface_azimuth",
            ] as const
        ).every(
            (field) => incomingSubmission[field] === editedSubmission[field],
        )
    )
        return server;
    return saved;
}
type ForecastQuery = {
    scope: PlantScope;
    date: string;
    resolution: 30 | 60;
    source: DataSource;
};
function PlantForecast({
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
}: ForecastQuery) {
    const resource = useResource(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/forecast/",
        {
            path: scope,
            query: {
                date,
                resolution_minutes: resolution,
                data_source: source,
            },
        },
        0,
        30_000,
    );
    return <ForecastResource resource={resource} electrical />;
}
function IrradianceForecast({ scope, date, source }: ForecastQuery) {
    const resource = useResource(
        "/api/tenants/{tenant_id}/regions/{region_id}/forecast/",
        {
            path: scope,
            query: {
                date,
                data_source: source === "archived_forecast" ? "auto" : source,
            },
        },
        0,
        30_000,
    );
    return <ForecastResource resource={resource} electrical={false} />;
}
function ForecastResource({
    resource,
    electrical,
}: {
    resource: { data?: Forecast; error?: string; loading: boolean };
    electrical: boolean;
}) {
    const t = useTranslations("PlantWorkspace");
    return (
        <section className="panel">
            {resource.loading ? (
                <p role="status">
                    {electrical
                        ? t("loadingGeneration")
                        : t("loadingIrradiance")}
                </p>
            ) : resource.error ? (
                <Notice error>{resource.error}</Notice>
            ) : (
                resource.data && (
                    <ForecastPanel
                        forecast={resource.data}
                        electrical={electrical}
                    />
                )
            )}
        </section>
    );
}
