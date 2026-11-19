"use client";
import { useTranslations, useFormatter } from "next-intl";
import { useRouter } from "next/navigation";
import { startTransition, useOptimistic, useState } from "react";
import { useCanManageWorkspace } from "./workspace-access";
import { Reminders } from "./reminders";
import { CollectionSchedule } from "./collection-schedule";
import { RegionSettings } from "./region-settings";
import { CsvDownload } from "./csv-download";
import type { Forecast, Plant, PlantScope, Region } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiQuery, useRefresh, type Loaded } from "@/lib/api/query";
import {
    plantView,
    routes,
    type PlantAddress,
    type PlantSection,
} from "@/lib/routes";
import { Calibrations } from "./calibrations";
import { ActionForm, Notice, RefreshFailed, Tag } from "./ui";
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
    section: tab,
    address,
}: {
    plant: Plant;
    scope: PlantScope;
    timezone: string;
    region: Region;
    section: PlantSection;
    address: PlantAddress;
}) {
    const t = useTranslations("PlantWorkspace");
    const format = useFormatter();
    const canManage = useCanManageWorkspace();
    const router = useRouter();
    const refresh = useRefresh();
    const date = address.date ?? localDay(timezone, 1);
    const forecastView = plantView("forecast", address);
    const productionView = plantView("production", address);
    const settingsView = plantView("settings", address);
    const [resolution, setResolution] = useState("60"),
        [source, setSource] = useState<DataSource>("auto");
    // Archived forecasts exist only for generation; the view also changes through Back and Forward.
    if (forecastView === "irradiance" && source === "archived_forecast")
        setSource("auto");
    const [savedPlant, setSavedPlant] = useState<Plant>();
    const currentPlant = latestPlant(plant, savedPlant);
    if (savedPlant && currentPlant === plant) setSavedPlant(undefined);
    const portfolio = routes.portfolio(scope.tenant_id);
    // Lives as long as the router transition: a committed, superseded, restored or failed
    // navigation returns it to the displayed address.
    const [pending, setPending] = useOptimistic({ section: tab, address });
    // Controls belong to the displayed section; only a pending change of that section is their base.
    const base = pending.section === tab ? pending.address : address;
    const navigating =
        routes.plant(
            scope.tenant_id,
            scope.region_id,
            pending.section,
            pending.address,
        ) !== routes.plant(scope.tenant_id, scope.region_id, tab, address);
    // Controls only the outgoing view has rest until the pending view change commits. A disabled
    // fieldset stops only form controls, so the outgoing body is also inert (disclosures, links).
    const leaving = (section: "forecast" | "production" | "settings") =>
        pending.section === tab &&
        tab === section &&
        plantView(section, pending.address) !== plantView(section, address);
    const leavingGeneration =
        leaving("forecast") && forecastView === "generation";
    const navigate = (
        section: PlantSection,
        next: PlantAddress,
        mode: "push" | "replace",
    ) =>
        startTransition(() => {
            setPending({ section, address: next });
            router[mode](
                routes.plant(scope.tenant_id, scope.region_id, section, next),
            );
        });
    const selectTab = (next: PlantSection) =>
        navigate(next, next === tab ? base : {}, "push");
    const show = (next: PlantAddress) =>
        navigate(tab, { ...base, ...next }, "replace");
    const setDate = (next: string) => show({ date: next });
    const changed = () => refresh.workspace(scope.tenant_id);
    const today = localDay(timezone);
    const configuration = currentPlant.current_configuration;
    const metadata = configuration ?? currentPlant.submission;
    return (
        <>
            <button
                className="back-button"
                onClick={() => router.push(portfolio)}
            >
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
            <nav
                className="tabs"
                aria-label={t("plantSections")}
                aria-busy={navigating || undefined}
            >
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
                                onClick={() => show({ view: key })}
                            >
                                {label}
                            </button>
                        ))}
                    </nav>
                    <div
                        className="forecast-controls"
                        aria-busy={leaving("forecast") || undefined}
                    >
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
                            archivedForecastDisabled={leavingGeneration}
                            value={source}
                            onChange={(next) => {
                                setSource(next);
                                if (
                                    (next === "estimated_actuals" ||
                                        next === "archived_forecast") &&
                                    (base.date || localDay(timezone, 1)) > today
                                )
                                    setDate(today);
                            }}
                        />
                        {forecastView === "generation" && (
                            <label className="field">
                                {t("resolution")}
                                <select
                                    value={resolution}
                                    disabled={leavingGeneration}
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
                            key={`${forecastView}-${date}-${resolution}-${source}-${configuration?.id}`}
                            scope={scope}
                            electrical={forecastView === "generation"}
                            date={date}
                            resolution={resolution === "30" ? 30 : 60}
                            source={source}
                            poll={
                                source !== "archived_forecast" &&
                                date >= localDay(timezone, -1)
                                    ? cadence.live
                                    : false
                            }
                        />
                    )}
                </>
            ) : tab === "outages" ? (
                <Outages
                    configuration={configuration}
                    scope={scope}
                    timezone={timezone}
                    onChanged={changed}
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
                                onClick={() => show({ view: key })}
                            >
                                {label}
                            </button>
                        ))}
                    </nav>
                    <fieldset
                        disabled={leaving("settings")}
                        inert={leaving("settings")}
                        aria-busy={leaving("settings") || undefined}
                    >
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
                                            await changed();
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
                                    <Notice>
                                        {t("approvalDetailsRequired")}
                                    </Notice>
                                )}
                            </section>
                        ) : settingsView === "location" ? (
                            <RegionSettings region={region} scope={scope} />
                        ) : settingsView === "collection" ? (
                            <CollectionSchedule scope={scope} region={region} />
                        ) : (
                            <Reminders scope={scope} region={region} />
                        )}
                    </fieldset>
                    {canManage && (
                        <section className="panel">
                            <h2>{t("deletePlant")}</h2>
                            <p>{t("deletionHelp")}</p>
                            <MoveToRecycleBin
                                name={currentPlant.name}
                                scope={scope}
                                onDeleted={async () => {
                                    await changed();
                                    router.push(portfolio);
                                }}
                            />
                        </section>
                    )}
                </>
            ) : tab === "production" ? (
                <>
                    <nav className="subnav" aria-label={t("productionViews")}>
                        {(
                            [
                                ["actuals", t("actuals")],
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
                                onClick={() => show({ view: key })}
                            >
                                {label}
                            </button>
                        ))}
                    </nav>
                    <fieldset
                        disabled={leaving("production")}
                        inert={leaving("production")}
                        aria-busy={leaving("production") || undefined}
                    >
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
                                day={address.date}
                                baseDay={base.date}
                                onDay={setDate}
                                onSaved={changed}
                            />
                        )}
                    </fieldset>
                </>
            ) : !configuration ? (
                <Notice>{t("calibrationUnavailable")}</Notice>
            ) : (
                <Calibrations
                    key={`calibration-${configuration.id}`}
                    scope={scope}
                    currentConfigurationId={configuration.id}
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
                "surface_tilt_degrees",
                "surface_azimuth_degrees",
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
    poll: number | false;
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
            {resource.loading ? (
                <p role="status">
                    {electrical
                        ? t("loadingGeneration")
                        : t("loadingIrradiance")}
                </p>
            ) : resource.data ? (
                <>
                    <RefreshFailed resource={resource} />
                    <ForecastPanel
                        forecast={resource.data}
                        electrical={electrical}
                    />
                </>
            ) : (
                <Notice error>{resource.error}</Notice>
            )}
        </section>
    );
}
