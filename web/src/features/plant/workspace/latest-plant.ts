import type { Plant } from "@/lib/api";

export function latestPlant(server: Plant, saved?: Plant): Plant {
    return !saved || server.parameters_revision >= saved.parameters_revision
        ? server
        : saved;
}
