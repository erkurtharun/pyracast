"use client";

import { useState } from "react";
import type { components } from "@/lib/schema";
import { Field, Select } from "./ui";

type Configuration = Pick<
    components["schemas"]["ConfigurationOutput"],
    | "dc_capacity_mw"
    | "inverter_capacity_mw"
    | "export_limit_mw"
    | "measurement_basis"
    | "mounting_type"
    | "surface_tilt"
    | "surface_azimuth"
>;

export function configurationInput(data: FormData) {
    const optional = (name: string) => {
        const value = data.get(name);
        return value === null || value === "" ? null : Number(value);
    };
    const mounting = data.get("mounting_type");
    return {
        name: data.get("name"),
        dc_capacity_mw: Number(data.get("dc_capacity_mw")),
        inverter_capacity_mw: optional("inverter_capacity_mw"),
        export_limit_mw: optional("export_limit_mw"),
        measurement_basis: data.get("measurement_basis"),
        mounting_type: mounting,
        surface_tilt: mounting === "fixed" ? optional("surface_tilt") : null,
        surface_azimuth:
            mounting === "fixed" ? optional("surface_azimuth") : null,
    };
}

export function PlantConfigurationFields({
    name,
    configuration,
    includeName = true,
}: {
    name?: string;
    configuration?: Configuration;
    includeName?: boolean;
}) {
    const [mounting, setMounting] = useState<string>(
        configuration?.mounting_type ?? "unknown",
    );
    return (
        <>
            <div className="form-grid">
                {includeName && (
                    <Field label="Plant name" name="name" defaultValue={name} />
                )}
                <Field
                    label="DC nameplate capacity (MW)"
                    name="dc_capacity_mw"
                    type="number"
                    step="any"
                    min={0.000001}
                    defaultValue={configuration?.dc_capacity_mw}
                />
                <Field
                    label="Inverter AC capacity (MW, optional)"
                    name="inverter_capacity_mw"
                    type="number"
                    step="any"
                    min={0.000001}
                    required={false}
                    defaultValue={
                        configuration?.inverter_capacity_mw ?? undefined
                    }
                />
                <Field
                    label="Grid export limit (MW, optional)"
                    name="export_limit_mw"
                    type="number"
                    step="any"
                    min={0.000001}
                    required={false}
                    defaultValue={configuration?.export_limit_mw ?? undefined}
                />
                <Select
                    label="Measurement point"
                    name="measurement_basis"
                    defaultValue={configuration?.measurement_basis}
                >
                    <option value="net_export">Net export</option>
                    <option value="ac_generation">AC generation</option>
                </Select>
                <label className="field">
                    Mounting type
                    <select
                        name="mounting_type"
                        value={mounting}
                        onChange={(event) => setMounting(event.target.value)}
                    >
                        <option value="unknown">Unknown</option>
                        <option value="fixed">Fixed</option>
                        <option value="tracking">Tracking</option>
                    </select>
                </label>
                {mounting === "fixed" && (
                    <>
                        <Field
                            label="Surface tilt (degrees, optional)"
                            name="surface_tilt"
                            type="number"
                            step="any"
                            min={0}
                            max={90}
                            required={false}
                            defaultValue={
                                configuration?.surface_tilt ?? undefined
                            }
                        />
                        <label className="field">
                            Surface azimuth (degrees, optional)
                            <input
                                name="surface_azimuth"
                                type="number"
                                step="any"
                                min={0}
                                max={360}
                                defaultValue={
                                    configuration?.surface_azimuth ?? undefined
                                }
                                onChange={(event) =>
                                    event.currentTarget.setCustomValidity(
                                        event.currentTarget.value !== "" &&
                                            event.currentTarget.valueAsNumber >=
                                                360
                                            ? "Azimuth must be less than 360 degrees."
                                            : "",
                                    )
                                }
                            />
                        </label>
                    </>
                )}
            </div>
            <p className="fine">Leave unknown optional capacities blank.</p>
            <details>
                <summary>Capacity and mounting guide</summary>
                <p className="fine">
                    DC nameplate is the panel rating. Inverter AC capacity is
                    the inverter rating; grid export limit is the permitted grid
                    cap. The measurement point identifies AC generation or net
                    export.
                </p>
                <p className="fine">
                    For fixed mounting, tilt is 0–90 degrees; azimuth is
                    clockwise from true north, from 0 up to but excluding 360
                    degrees. Tracking details are not captured. The current
                    estimate uses a proxy that does not use these geometry
                    fields.
                </p>
            </details>
        </>
    );
}
