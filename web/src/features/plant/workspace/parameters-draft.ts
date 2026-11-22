import type { Plant } from "@/lib/api";
import type { Configuration } from "@/features/portfolio/plant-configuration-fields";

export type Values = Configuration & { name: string };
export type ValueField = keyof Values;

const VALUE_FIELDS = [
    "name",
    "dc_capacity_mw",
    "inverter_capacity_mw",
    "export_limit_mw",
    "measurement_basis",
    "mounting_type",
    "surface_tilt_degrees",
    "surface_azimuth_degrees",
] as const satisfies readonly ValueField[];

const ANGLES = [
    "surface_tilt_degrees",
    "surface_azimuth_degrees",
] as const satisfies readonly ValueField[];

// What a save from an older revision met: the server's values when the person started
// editing (`before`) and now (`current`), the fields that differ, those the person also
// edited and kept (`contested`) and the edits that no longer apply (`dropped`).
export type Conflict = {
    before: Values;
    current: Values;
    typed: Values;
    changed: ValueField[];
    contested: ValueField[];
    dropped: ValueField[];
};

// The fields show `values` and remount when `key` changes; `server` holds the server's
// values of revision `base`, so an edit is what differs from them.
export type Draft = {
    key: number;
    base: number;
    server: Values;
    values: Values;
    edited: boolean;
    conflict: Conflict | null;
};

function plantValues(plant: Plant, parameters: Configuration): Values {
    return { ...parameters, name: plant.name };
}

export function loadedDraft(
    plant: Plant,
    parameters: Configuration,
    key: number,
): Draft {
    const server = plantValues(plant, parameters);
    return {
        key,
        base: plant.parameters_revision,
        server,
        values: server,
        edited: false,
        conflict: null,
    };
}

export function conflictDraft(
    draft: Draft,
    typed: Values,
    current: Plant,
    parameters: Configuration,
): Draft {
    const now = plantValues(current, parameters);
    const before = draft.conflict?.before ?? draft.server;
    const edits = VALUE_FIELDS.filter(
        (field) => typed[field] !== draft.server[field],
    );
    const values: Values = { ...now };
    for (const field of edits) Object.assign(values, { [field]: typed[field] });
    const fixed = values.mounting_type === "fixed";
    const dropped: ValueField[] = fixed
        ? []
        : ANGLES.filter(
              (field) => edits.includes(field) && typed[field] !== null,
          );
    if (!fixed) for (const field of ANGLES) values[field] = null;
    const changed = VALUE_FIELDS.filter(
        (field) => before[field] !== now[field],
    );
    return {
        key: draft.key + 1,
        base: current.parameters_revision,
        server: now,
        values,
        edited: true,
        conflict: {
            before,
            current: now,
            typed,
            changed,
            contested: changed.filter(
                (field) =>
                    edits.includes(field) &&
                    !dropped.includes(field) &&
                    typed[field] !== now[field],
            ),
            dropped,
        },
    };
}
