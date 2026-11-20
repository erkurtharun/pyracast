import type { Plant } from "@/lib/api";

const SUBMISSION_FIELDS = [
    "dc_capacity_mw",
    "inverter_capacity_mw",
    "export_limit_mw",
    "measurement_basis",
    "mounting_type",
    "surface_tilt_degrees",
    "surface_azimuth_degrees",
] as const;

export function latestPlant(server: Plant, saved?: Plant): Plant {
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
        SUBMISSION_FIELDS.every(
            (field) => incomingSubmission[field] === editedSubmission[field],
        )
    )
        return server;
    return saved;
}
