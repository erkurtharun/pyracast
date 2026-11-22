"use client";
import { useTranslations } from "next-intl";
import { MISSING } from "@/lib/format/number";
import { useFormat } from "@/lib/format/use-format";
import { ChangedFields } from "@/ui/changed-fields";
import type { Conflict, ValueField, Values } from "./parameters-draft";

const LABELS = {
    name: "plantName",
    dc_capacity_mw: "dcCapacityLabel",
    inverter_capacity_mw: "inverterCapacityLabel",
    export_limit_mw: "exportLimitLabel",
    measurement_basis: "measurementPoint",
    mounting_type: "mountingType",
    surface_tilt_degrees: "surfaceTiltDegreesOptional",
    surface_azimuth_degrees: "surfaceAzimuthDegreesOptional",
} as const satisfies Record<ValueField, string>;

const CHOICES = {
    net_export: "netExport",
    ac_generation: "acGeneration",
    unknown: "unknown",
    fixed: "fixed",
    tracking: "tracking",
} as const;

export function ParameterConflict({ conflict }: { conflict: Conflict }) {
    const t = useTranslations("PlantWorkspace");
    const fields = useTranslations("PlantConfiguration");
    const format = useFormat();
    const label = (field: ValueField) => fields(LABELS[field]);
    const show = (values: Values, field: ValueField): string => {
        if (field === "name") return values.name || MISSING;
        if (field === "measurement_basis")
            return fields(CHOICES[values.measurement_basis]);
        if (field === "mounting_type")
            return fields(CHOICES[values.mounting_type]);
        const value = values[field];
        return value === null ? fields("unknown") : format.number(value, 6);
    };
    if (conflict.changed.length === 0 && conflict.dropped.length === 0)
        return null;
    return (
        <div className="notice">
            <p>{t("serverChanges")}</p>
            <ChangedFields
                changes={conflict.changed.map((field) => ({
                    field,
                    label: label(field),
                    before: show(conflict.before, field),
                    after: show(conflict.current, field),
                    note: conflict.contested.includes(field)
                        ? t("editKept", { value: show(conflict.typed, field) })
                        : undefined,
                }))}
            />
            {conflict.dropped.map((field) => (
                <p key={field}>
                    {t("editDropped", {
                        field: label(field),
                        value: show(conflict.typed, field),
                    })}
                </p>
            ))}
        </div>
    );
}
