"use client";
import { useState } from "react";
import { request, type Region, type Plant, type Forecast } from "@/lib/api";
import {
    ActionForm,
    Empty,
    Notice,
    Tag,
    useResource,
    useCollection,
} from "./ui";
import { ForecastPanel } from "./forecast-panel";
import { PlantWorkspace } from "./plant-workspace";
import {
    PlantConfigurationFields,
    configurationInput,
} from "./plant-configuration-fields";
import { MoveToRecycleBin } from "./recycle-bin";
import { Reminders } from "./reminders";
import { CollectionSchedule } from "./collection-schedule";
import { DataSourceControl, localDay, type DataSource } from "./data-source";
import { RegionSettings } from "./region-settings";
export function RegionWorkspace({
    region,
    tenant,
    token,
    onBack,
    onDeleted,
    onUpdated,
}: {
    region: Region;
    tenant: number;
    token: string;
    onBack: () => void;
    onDeleted: () => void;
    onUpdated: (region: Region) => void;
}) {
    const [tab, setTab] = useState("forecast"),
        [plantOpen, setPlantOpen] = useState(false),
        [editing, setEditing] = useState(false),
        [date, setDate] = useState(() =>
            new Date().toLocaleDateString("en-CA", {
                timeZone: region.timezone,
            }),
        ),
        [source, setSource] = useState<DataSource>("auto");
    const yesterday = localDay(region.timezone, -1);
    const base = `/api/tenants/${tenant}/regions/${region.id}/`;
    return (
        <>
            {plantOpen ? (
                <button
                    className="context-label"
                    aria-label={`Back to region: ${region.name}`}
                    onClick={() => {
                        setTab("forecast");
                        setPlantOpen(false);
                    }}
                >
                    {region.name}
                </button>
            ) : (
                <>
                    <button className="back-button" onClick={onBack}>
                        ← All regions
                    </button>
                    <div className="page-heading">
                        <div>
                            <h1>{region.name}</h1>
                            <p>
                                {region.latitude}°, {region.longitude}°{" "}
                                <span className="separator">·</span>{" "}
                                {region.timezone}
                            </p>
                        </div>
                        <div className="button-group">
                            <Tag>{region.status}</Tag>
                            <button
                                className="button secondary"
                                onClick={() => setEditing(!editing)}
                            >
                                {editing ? "Close editor" : "Edit region"}
                            </button>
                            <MoveToRecycleBin
                                kind="region"
                                name={region.name}
                                url={base}
                                token={token}
                                onDeleted={onDeleted}
                            />
                        </div>
                    </div>
                    {editing && (
                        <RegionSettings
                            region={region}
                            base={base}
                            token={token}
                            onUpdated={(updated) => {
                                onUpdated(updated);
                                setEditing(false);
                            }}
                        />
                    )}
                    <nav className="tabs" aria-label="Region sections">
                        {[
                            ["forecast", "Regional forecast"],
                            ["plants", "Solar plants"],
                            ["collection", "Data collection"],
                            ["reminders", "Email reminders"],
                        ].map(([key, label]) => (
                            <button
                                key={key}
                                aria-current={tab === key ? "page" : undefined}
                                className={tab === key ? "active" : ""}
                                onClick={() => setTab(key)}
                            >
                                {label}
                            </button>
                        ))}
                    </nav>
                </>
            )}
            {tab === "forecast" ? (
                <>
                    <div className="section-heading">
                        <div>
                            <h2>Regional forecast</h2>
                            <p>
                                Daily irradiance at your region’s surface. Dates
                                follow {region.timezone}.
                            </p>
                        </div>
                        <label className="field date-field">
                            Forecast date
                            <input
                                type="date"
                                value={date}
                                max={
                                    source === "estimated_actuals"
                                        ? yesterday
                                        : undefined
                                }
                                onChange={(event) =>
                                    setDate(event.target.value)
                                }
                            />
                        </label>
                        <DataSourceControl
                            value={source}
                            onChange={(next) => {
                                setSource(next);
                                if (
                                    next === "estimated_actuals" &&
                                    date > yesterday
                                )
                                    setDate(yesterday);
                            }}
                        />
                    </div>
                    {source === "estimated_actuals" && date > yesterday ? (
                        <Notice>
                            Choose a completed local day for estimated actuals.
                        </Notice>
                    ) : (
                        <RegionalForecast
                            key={`${date}-${source}`}
                            url={`${base}forecast/?date=${date}&data_source=${source}`}
                        />
                    )}
                </>
            ) : tab === "plants" ? (
                <Plants
                    base={base}
                    region={region}
                    token={token}
                    onSelectionChange={setPlantOpen}
                />
            ) : tab === "collection" ? (
                <CollectionSchedule base={base} region={region} token={token} />
            ) : (
                <Reminders base={base} region={region} token={token} />
            )}
        </>
    );
}
function RegionalForecast({ url }: { url: string }) {
    const [version, setVersion] = useState(0);
    const resource = useResource<Forecast>(url, version, 30_000);
    return (
        <section className="panel">
            {resource.loading ? (
                <p role="status">Loading saved forecast…</p>
            ) : resource.error ? (
                <>
                    <Notice error>{resource.error}</Notice>
                    <button
                        className="button secondary"
                        onClick={() => setVersion(version + 1)}
                    >
                        Try again
                    </button>
                </>
            ) : (
                resource.data && <ForecastPanel forecast={resource.data} />
            )}
        </section>
    );
}
function Plants({
    base,
    region,
    token,
    onSelectionChange,
}: {
    base: string;
    region: Region;
    token: string;
    onSelectionChange: (selected: boolean) => void;
}) {
    const [version, setVersion] = useState(0),
        [creating, setCreating] = useState(false),
        [selected, setSelected] = useState<Plant>(),
        [deleted, setDeleted] = useState<number[]>([]);
    const plants = useCollection<Plant>(`${base}plants/`, version);
    const rows =
        plants.data?.results.filter((row) => !deleted.includes(row.id)) ?? [];
    if (selected)
        return (
            <PlantWorkspace
                key={selected.id}
                plant={selected}
                base={`${base}plants/${selected.id}/`}
                timezone={region.timezone}
                token={token}
                onBack={() => {
                    setVersion((current) => current + 1);
                    setSelected(undefined);
                    onSelectionChange(false);
                }}
                onDeleted={() => {
                    setDeleted((current) => [...current, selected.id]);
                    setVersion((current) => current + 1);
                    setSelected(undefined);
                    onSelectionChange(false);
                }}
            />
        );
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>Solar plants</h2>
                </div>
                <button
                    className="button"
                    disabled={region.status !== "approved"}
                    onClick={() => setCreating(!creating)}
                >
                    {creating ? "Close form" : "+ Add a plant"}
                </button>
            </div>
            {region.status !== "approved" && (
                <Notice>
                    Plant configuration becomes available after the region is
                    approved and connected.
                </Notice>
            )}
            {creating && (
                <ActionForm
                    submit="Create plant"
                    onSubmit={async (data) => {
                        const created = await request<Plant>(
                            `${base}plants/`,
                            "POST",
                            configurationInput(data),
                            token,
                        );
                        setVersion(version + 1);
                        setSelected(created);
                        onSelectionChange(true);
                    }}
                >
                    <PlantConfigurationFields />
                </ActionForm>
            )}
            {plants.loading ? (
                <p role="status">Loading plants…</p>
            ) : plants.error ? (
                <Notice error>{plants.error}</Notice>
            ) : rows.length ? (
                <div className="region-grid">
                    {rows.map((plant) => (
                        <button
                            className="region-card"
                            key={plant.id}
                            onClick={() => {
                                setSelected(plant);
                                onSelectionChange(true);
                            }}
                        >
                            <div className="region-card-top">
                                <span
                                    className="location-icon"
                                    aria-hidden="true"
                                >
                                    ▥
                                </span>
                                <span aria-hidden="true">↗</span>
                            </div>
                            <h3>{plant.name}</h3>
                            <p>
                                {plant.current_configuration?.dc_capacity_mw ??
                                    "—"}{" "}
                                MW DC
                            </p>
                            <div className="region-card-bottom">
                                {plant.current_configuration?.measurement_basis.replaceAll(
                                    "_",
                                    " ",
                                )}
                            </div>
                        </button>
                    ))}
                </div>
            ) : (
                <Empty title="No plants yet">
                    Add a plant to see its generation forecast.
                </Empty>
            )}
        </section>
    );
}
