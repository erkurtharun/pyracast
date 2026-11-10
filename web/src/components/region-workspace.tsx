"use client";
import { useState } from "react";
import { request, type Region, type Plant, type Forecast } from "@/lib/api";
import {
    ActionForm,
    Empty,
    Field,
    Notice,
    Select,
    Tag,
    useResource,
    useCollection,
} from "./ui";
import { ForecastPanel } from "./forecast-panel";
import { PlantWorkspace } from "./plant-workspace";
import { Reminders } from "./reminders";
export function RegionWorkspace({
    region,
    tenant,
    token,
    onBack,
}: {
    region: Region;
    tenant: number;
    token: string;
    onBack: () => void;
}) {
    const [tab, setTab] = useState("forecast"),
        [date, setDate] = useState(() =>
            new Date().toLocaleDateString("en-CA", {
                timeZone: region.timezone,
            }),
        );
    const base = `/api/tenants/${tenant}/regions/${region.id}/`;
    return (
        <>
            <button className="back-button" onClick={onBack}>
                ← All regions
            </button>
            <div className="page-heading">
                <div>
                    <span className="eyebrow">REGION WORKSPACE</span>
                    <h1>{region.name}</h1>
                    <p>
                        {region.latitude}°, {region.longitude}°{" "}
                        <span className="separator">·</span> {region.timezone}
                    </p>
                </div>
                <Tag>{region.status}</Tag>
            </div>
            <nav className="tabs" aria-label="Region sections">
                {[
                    ["forecast", "Regional forecast"],
                    ["plants", "Solar plants"],
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
            {tab === "forecast" ? (
                <>
                    <div className="section-heading">
                        <div>
                            <h2>See the day ahead</h2>
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
                                onChange={(event) =>
                                    setDate(event.target.value)
                                }
                            />
                        </label>
                    </div>
                    <RegionalForecast
                        key={date}
                        url={`${base}forecast/?date=${date}`}
                    />
                </>
            ) : tab === "plants" ? (
                <Plants base={base} region={region} token={token} />
            ) : (
                <Reminders base={base} region={region} token={token} />
            )}
        </>
    );
}
function RegionalForecast({ url }: { url: string }) {
    const [version, setVersion] = useState(0);
    const resource = useResource<Forecast>(url, version);
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
}: {
    base: string;
    region: Region;
    token: string;
}) {
    const [version, setVersion] = useState(0),
        [creating, setCreating] = useState(false),
        [selected, setSelected] = useState<Plant>();
    const plants = useCollection<Plant>(`${base}plants/`, version);
    if (selected)
        return (
            <PlantWorkspace
                key={selected.id}
                plant={selected}
                base={`${base}plants/${selected.id}/`}
                timezone={region.timezone}
                token={token}
                onBack={() => setSelected(undefined)}
            />
        );
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>Your solar plants</h2>
                    <p>
                        Turn regional irradiance into an electrical generation
                        outlook.
                    </p>
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
                        const optional = (name: string) =>
                            data.get(name) === ""
                                ? null
                                : Number(data.get(name));
                        const created = await request<Plant>(
                            `${base}plants/`,
                            "POST",
                            {
                                name: data.get("name"),
                                dc_capacity_mw: Number(
                                    data.get("dc_capacity_mw"),
                                ),
                                inverter_capacity_mw: optional(
                                    "inverter_capacity_mw",
                                ),
                                export_limit_mw: optional("export_limit_mw"),
                                measurement_basis:
                                    data.get("measurement_basis"),
                            },
                            token,
                        );
                        setVersion(version + 1);
                        setSelected(created);
                    }}
                >
                    <div className="form-grid">
                        <Field label="Plant name" name="name" />
                        <Field
                            label="DC nameplate capacity (MW)"
                            name="dc_capacity_mw"
                            type="number"
                            step="any"
                            min={0.000001}
                        />
                        <Field
                            label="Inverter AC capacity (MW, optional)"
                            name="inverter_capacity_mw"
                            type="number"
                            step="any"
                            min={0.000001}
                            required={false}
                        />
                        <Field
                            label="Grid export limit (MW, optional)"
                            name="export_limit_mw"
                            type="number"
                            step="any"
                            min={0.000001}
                            required={false}
                        />
                        <Select
                            label="Measurement point"
                            name="measurement_basis"
                        >
                            <option value="net_export">Net export</option>
                            <option value="ac_generation">AC generation</option>
                        </Select>
                    </div>
                    <p className="fine">
                        DC nameplate is the total panel capacity in MW. Inverter
                        AC capacity is the total inverter rating in MW. Grid
                        export limit is the permitted grid limit in MW. Leave
                        either optional value blank if unknown.
                    </p>
                </ActionForm>
            )}
            {plants.loading ? (
                <p role="status">Loading plants…</p>
            ) : plants.error ? (
                <Notice error>{plants.error}</Notice>
            ) : plants.data?.results.length ? (
                <div className="region-grid">
                    {plants.data.results.map((plant) => (
                        <button
                            className="region-card"
                            key={plant.id}
                            onClick={() => setSelected(plant)}
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
                <Empty title="Room for your solar ambitions">
                    Add a plant to explore electrical generation, import
                    production and calibrate your estimates.
                </Empty>
            )}
        </section>
    );
}
