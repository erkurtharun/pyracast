"use client";
import { useEffect, useState } from "react";
import { Reminders } from "./reminders";
import { CollectionSchedule } from "./collection-schedule";
import { RegionSettings } from "./region-settings";
import { CsvDownload } from "./csv-download";
import { request, type Plant, type Forecast, type Region } from "@/lib/api";
import { ProductionImport } from "./production-import";
import { Calibrations } from "./calibrations";
import { ActionForm, Notice, Tag, useResource } from "./ui";
import { ProductionHistory } from "./production-history";
import { MoveToRecycleBin } from "./recycle-bin";
import { ForecastPanel } from "./forecast-panel";
import { ForecastPerformance } from "./forecast-performance";
import { EpiasProduction } from "./epias-production";
import { Outages } from "./outages";
import {
    PlantConfigurationFields,
    configurationInput,
} from "./plant-configuration-fields";
import { DataSourceControl, localDay, type DataSource } from "./data-source";
export function PlantWorkspace({
    plant,
    base,
    timezone,
    token,
    region,
    regionBase,
    onBack,
    onDeleted,
}: {
    plant: Plant;
    base: string;
    timezone: string;
    token: string;
    region: Region;
    regionBase: string;
    onBack: () => void;
    onDeleted: () => void;
}) {
    const [tab, setTab] = useState("forecast"),
        [forecastView, setForecastView] = useState("generation"),
        [productionView, setProductionView] = useState("journal"),
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
    const yesterday = localDay(timezone, -1);
    const today = localDay(timezone);
    const configuration = currentPlant.current_configuration;
    const metadata = configuration ?? currentPlant.submission;
    return (
        <>
            <button className="back-button" onClick={onBack}>
                ← All plants
            </button>
            <div className="page-heading plant-identity">
                <div>
                    <p className="eyebrow">Solar plant</p>
                    <h1>{currentPlant.name}</h1>
                    <p>
                        {region.latitude}°, {region.longitude}° · {timezone}
                    </p>
                </div>
                <div className="button-group">
                    <Tag>{region.status}</Tag>
                    <CsvDownload
                        label="Download plant CSV"
                        filename={`plant-${currentPlant.id}`}
                        records={[currentPlant]}
                        metadata={{ timezone }}
                    />
                </div>
            </div>
            {region.status !== "approved" && (
                <Notice>
                    {region.status === "pending"
                        ? "Awaiting administrator approval. Your plant details are saved; forecasts become available after the data connection is approved and collection succeeds."
                        : `Plant approval is ${region.status}. Contact your administrator to resume collection.`}
                </Notice>
            )}
            <nav className="tabs" aria-label="Plant sections">
                {[
                    ["forecast", "Forecast"],
                    ["production", "Production"],
                    ["calibration", "Calibration"],
                    ["outages", "Outages"],
                    ["settings", "Settings"],
                ].map(([key, label]) => (
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
                    <nav className="subnav" aria-label="Forecast views">
                        {[
                            ["generation", "Generation"],
                            ["irradiance", "Irradiance"],
                        ].map(([key, label]) => (
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
                            Forecast date
                            <input
                                type="date"
                                value={date}
                                max={
                                    source === "estimated_actuals"
                                        ? yesterday
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
                                    date > yesterday
                                )
                                    setDate(yesterday);
                                if (
                                    next === "archived_forecast" &&
                                    date > today
                                )
                                    setDate(today);
                            }}
                        />
                        {forecastView === "generation" && (
                            <label className="field">
                                Resolution
                                <select
                                    value={resolution}
                                    onChange={(event) =>
                                        setResolution(event.target.value)
                                    }
                                >
                                    <option value="30">30 minutes</option>
                                    <option value="60">60 minutes</option>
                                </select>
                            </label>
                        )}
                        <span className="fine">Local dates in {timezone}</span>
                    </div>
                    {source === "estimated_actuals" && date > yesterday ? (
                        <Notice>
                            Choose a completed local day for estimated actuals.
                        </Notice>
                    ) : source === "archived_forecast" && date > today ? (
                        <Notice>
                            Choose today or an earlier day for archived
                            forecasts.
                        </Notice>
                    ) : (
                        <PlantForecast
                            key={`${forecastView}-${date}-${resolution}-${source}-${configuration?.id}-${outageRevision}`}
                            electrical={forecastView === "generation"}
                            url={
                                forecastView === "generation"
                                    ? `${base}forecast/?date=${date}&resolution_minutes=${resolution}&data_source=${source}`
                                    : `${regionBase}forecast/?date=${date}&data_source=${source}`
                            }
                        />
                    )}
                </>
            ) : tab === "outages" ? (
                <Outages
                    currentConfigurationId={configuration?.id}
                    base={base}
                    token={token}
                    timezone={timezone}
                    onChanged={() => setOutageRevision((value) => value + 1)}
                />
            ) : tab === "settings" ? (
                <>
                    <nav className="subnav" aria-label="Plant settings">
                        {[
                            ["configuration", "Technical details"],
                            ["location", "Location"],
                            ["collection", "Data collection"],
                            ["reminders", "Email reminders"],
                        ].map(([key, label]) => (
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
                            <h2>Technical details</h2>
                            {metadata ? (
                                <ActionForm
                                    submit="Save configuration"
                                    onSubmit={async (data) => {
                                        setSavedPlant(
                                            await request<Plant>(
                                                base,
                                                "PUT",
                                                configurationInput(data),
                                                token,
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
                                            Changes may replace pending
                                            calibration.
                                        </p>
                                    )}
                                </ActionForm>
                            ) : (
                                <Notice>
                                    Complete technical details to request
                                    approval.
                                </Notice>
                            )}
                        </section>
                    ) : settingsView === "location" ? (
                        <RegionSettings
                            region={region}
                            base={regionBase}
                            token={token}
                        />
                    ) : settingsView === "collection" ? (
                        <CollectionSchedule
                            base={regionBase}
                            region={region}
                            token={token}
                        />
                    ) : (
                        <Reminders
                            base={regionBase}
                            region={region}
                            token={token}
                        />
                    )}
                    <section className="panel">
                        <h2>Delete plant</h2>
                        <p>
                            Move this plant, its location and associated history
                            to the recycle bin for 30 days.
                        </p>
                        <MoveToRecycleBin
                            kind="plant"
                            name={currentPlant.name}
                            url={regionBase}
                            token={token}
                            onDeleted={onDeleted}
                        />
                    </section>
                </>
            ) : tab === "production" ? (
                <>
                    <nav className="subnav" aria-label="Production views">
                        {[
                            ["journal", "Manual journal"],
                            ["automatic", "Automatic production"],
                            ["import", "Production import"],
                            ["comparison", "Comparison"],
                        ].map(([key, label]) => (
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
                    {productionView === "automatic" ? (
                        <EpiasProduction base={base} />
                    ) : productionView === "comparison" ? (
                        <ForecastPerformance
                            key={configuration?.id}
                            base={base}
                            timezone={timezone}
                        />
                    ) : !configuration ? (
                        <Notice>
                            Production entry becomes available after
                            administrator approval and matching historical data
                            collection.
                        </Notice>
                    ) : productionView === "import" ? (
                        <ProductionImport
                            key={configuration.id}
                            base={base}
                            configuration={configuration.id}
                            token={token}
                        />
                    ) : (
                        <ProductionHistory
                            key={`history-${configuration.id}`}
                            base={base}
                            configuration={configuration.id}
                            token={token}
                            onSaved={() =>
                                setProductionRevision((current) => current + 1)
                            }
                        />
                    )}
                </>
            ) : !configuration ? (
                <Notice>
                    Calibration becomes available after administrator approval
                    and matched production history.
                </Notice>
            ) : (
                <Calibrations
                    key={`calibration-${configuration.id}`}
                    base={base}
                    token={token}
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
function PlantForecast({
    url,
    electrical,
}: {
    url: string;
    electrical: boolean;
}) {
    const resource = useResource<Forecast>(url, 0, 30_000);
    return (
        <section className="panel">
            {resource.loading ? (
                <p role="status">
                    Loading {electrical ? "generation" : "irradiance"} forecast…
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
