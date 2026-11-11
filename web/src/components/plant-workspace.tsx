"use client";
import { useState } from "react";
import { CsvDownload } from "./csv-download";
import { request, type Plant, type Forecast } from "@/lib/api";
import { ProductionImport } from "./production-import";
import { Calibrations } from "./calibrations";
import { ActionForm, Notice, Stat, useResource } from "./ui";
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
    onBack,
    onDeleted,
}: {
    plant: Plant;
    base: string;
    timezone: string;
    token: string;
    onBack: () => void;
    onDeleted: () => void;
}) {
    const [tab, setTab] = useState("forecast"),
        [date, setDate] = useState(() => localDay(timezone, 1)),
        [resolution, setResolution] = useState("30"),
        [source, setSource] = useState<DataSource>("auto");
    const [productionRevision, setProductionRevision] = useState(0);
    const [outageRevision, setOutageRevision] = useState(0);
    const [currentPlant, setCurrentPlant] = useState(plant);
    const [editing, setEditing] = useState(false);
    const yesterday = localDay(timezone, -1);
    const today = localDay(timezone);
    const configuration = currentPlant.current_configuration;
    return (
        <>
            <button className="back-button" onClick={onBack}>
                ← All plants
            </button>
            <div className="section-heading">
                <div>
                    <h1>{currentPlant.name}</h1>
                    <p>
                        Configuration {configuration?.revision ?? "—"} ·{" "}
                        {configuration?.measurement_basis.replaceAll("_", " ")}
                    </p>
                </div>
                <div className="button-group">
                    <CsvDownload
                        label="Download plant CSV"
                        filename={`plant-${currentPlant.id}`}
                        records={[currentPlant]}
                        metadata={{ timezone }}
                    />
                    <button
                        className="button secondary"
                        onClick={() => setEditing(!editing)}
                    >
                        {editing ? "Close configuration" : "Edit configuration"}
                    </button>
                    <details>
                        <summary>Plant actions</summary>
                        <div className="button-group">
                            <MoveToRecycleBin
                                kind="plant"
                                name={currentPlant.name}
                                url={base}
                                token={token}
                                onDeleted={onDeleted}
                            />
                        </div>
                    </details>
                </div>
            </div>
            <div className="stats capacity-stats">
                <Stat
                    label="DC nameplate"
                    value={configuration?.dc_capacity_mw}
                    unit="MW"
                />
                <Stat
                    label="Inverter AC capacity"
                    value={configuration?.inverter_capacity_mw}
                    unit="MW"
                />
                <Stat
                    label="Grid export limit"
                    value={configuration?.export_limit_mw}
                    unit="MW"
                />
            </div>
            {editing && configuration && (
                <section className="panel">
                    <h2>Edit plant configuration</h2>
                    <p>
                        Saving creates a new configuration revision and may
                        supersede pending calibration work.
                    </p>
                    <ActionForm
                        submit="Save configuration"
                        onSubmit={async (data) => {
                            const updated = await request<Plant>(
                                base,
                                "PUT",
                                configurationInput(data),
                                token,
                            );
                            setCurrentPlant(updated);
                            setEditing(false);
                        }}
                    >
                        <PlantConfigurationFields
                            name={currentPlant.name}
                            configuration={configuration}
                        />
                    </ActionForm>
                </section>
            )}
            <nav className="tabs" aria-label="Plant sections">
                {[
                    ["forecast", "Generation forecast"],
                    ["outages", "Outages"],
                    ["production", "Production import"],
                    ["automatic", "Automatic production"],
                    ["calibration", "Calibration"],
                ].map(([key, label]) => (
                    <button
                        key={key}
                        className={tab === key ? "active" : ""}
                        aria-current={tab === key ? "page" : undefined}
                        onClick={() => setTab(key)}
                    >
                        {label}
                    </button>
                ))}
            </nav>
            {tab === "forecast" ? (
                <>
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
                            allowArchivedForecast
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
                            key={`${date}-${resolution}-${source}-${configuration?.id}-${outageRevision}`}
                            url={`${base}forecast/?date=${date}&resolution_minutes=${resolution}&data_source=${source}`}
                        />
                    )}
                    <ForecastPerformance
                        key={configuration?.id}
                        base={base}
                        timezone={timezone}
                    />
                </>
            ) : tab === "outages" ? (
                <Outages
                    currentConfigurationId={configuration?.id}
                    base={base}
                    token={token}
                    timezone={timezone}
                    onChanged={() => setOutageRevision((value) => value + 1)}
                />
            ) : tab === "automatic" ? (
                <EpiasProduction base={base} token={token} />
            ) : !configuration ? (
                <Notice>No configuration is available for this plant.</Notice>
            ) : tab === "production" ? (
                <ProductionImport
                    key={configuration.id}
                    base={base}
                    configuration={configuration.id}
                    token={token}
                />
            ) : (
                <>
                    <ProductionHistory
                        key={`history-${configuration.id}`}
                        base={base}
                        configuration={configuration.id}
                        token={token}
                        onSaved={() =>
                            setProductionRevision((current) => current + 1)
                        }
                    />
                    <Calibrations
                        key={`calibration-${configuration.id}`}
                        base={base}
                        token={token}
                        productionRevision={productionRevision}
                    />
                </>
            )}
        </>
    );
}
function PlantForecast({ url }: { url: string }) {
    const resource = useResource<Forecast>(url, 0, 30_000);
    return (
        <section className="panel">
            {resource.loading ? (
                <p role="status">Loading generation forecast…</p>
            ) : resource.error ? (
                <Notice error>{resource.error}</Notice>
            ) : (
                resource.data && (
                    <ForecastPanel forecast={resource.data} electrical />
                )
            )}
        </section>
    );
}
