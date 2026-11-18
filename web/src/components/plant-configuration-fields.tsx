"use client";
import { useTranslations } from "next-intl";

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

function choice<T extends string>(
    value: FormDataEntryValue | null,
    options: readonly [T, ...T[]],
): T {
    const selected = options.find((option) => option === value);
    if (selected === undefined)
        throw new Error("Select a valid plant configuration.");
    return selected;
}

export function configurationInput(data: FormData) {
    const optional = (name: string) => {
        const value = data.get(name);
        return value === null || value === "" ? null : Number(value);
    };
    const mounting = choice(data.get("mounting_type"), [
        "unknown",
        "fixed",
        "tracking",
    ]);
    return {
        name: String(data.get("name") ?? ""),
        dc_capacity_mw: Number(data.get("dc_capacity_mw")),
        inverter_capacity_mw: optional("inverter_capacity_mw"),
        export_limit_mw: optional("export_limit_mw"),
        measurement_basis: choice(data.get("measurement_basis"), [
            "net_export",
            "ac_generation",
        ]),
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
    const t = useTranslations("PlantConfiguration");

    const [mounting, setMounting] = useState<string>(
        configuration?.mounting_type ?? "unknown",
    );
    return (
        <>
            <div className="form-grid">
                {includeName && (
                    <Field
                        label={t("plantName")}
                        name="name"
                        defaultValue={name}
                    />
                )}
                <Field
                    label={t("dcCapacityLabel")}
                    name="dc_capacity_mw"
                    type="number"
                    step="any"
                    min={0.000001}
                    defaultValue={configuration?.dc_capacity_mw}
                />
                <Field
                    label={t("inverterCapacityLabel")}
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
                    label={t("exportLimitLabel")}
                    name="export_limit_mw"
                    type="number"
                    step="any"
                    min={0.000001}
                    required={false}
                    defaultValue={configuration?.export_limit_mw ?? undefined}
                />
                <Select
                    label={t("measurementPoint")}
                    name="measurement_basis"
                    defaultValue={configuration?.measurement_basis}
                >
                    <option value="net_export">{t("netExport")}</option>
                    <option value="ac_generation">{t("acGeneration")}</option>
                </Select>
                <label className="field">
                    {t("mountingType")}
                    <select
                        name="mounting_type"
                        value={mounting}
                        onChange={(event) => setMounting(event.target.value)}
                    >
                        <option value="unknown">{t("unknown")}</option>
                        <option value="fixed">{t("fixed")}</option>
                        <option value="tracking">{t("tracking")}</option>
                    </select>
                </label>
                {mounting === "fixed" && (
                    <>
                        <Field
                            label={t("surfaceTiltDegreesOptional")}
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
                            {t("surfaceAzimuthDegreesOptional")}
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
                                            ? t(
                                                  "azimuthMustBeLessThan360Degrees",
                                              )
                                            : "",
                                    )
                                }
                            />
                        </label>
                    </>
                )}
            </div>
            <details>
                <summary>{t("capacityAndMountingGuide")}</summary>
                <p className="fine">{t("capacityGuide")}</p>
                <p className="fine">{t("geometryGuide")}</p>
            </details>
        </>
    );
}
