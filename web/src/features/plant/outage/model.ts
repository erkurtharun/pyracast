import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { components } from "@/lib/schema";

export type Outage = components["schemas"]["IncidentOutput"];
export type OutageInput = components["schemas"]["IncidentInput"];

export type OutageChange =
    | { action: "create"; values: OutageInput; reason: string }
    | { action: "update"; outage: Outage; values: OutageInput; reason: string }
    | { action: "resolve"; outage: Outage; ends_at: string; reason: string }
    | { action: "cancel"; outage: Outage; reason: string };

type ProposalKey = { forPayload(payload: unknown): string; reset(): void };

function proposal(change: OutageChange) {
    const target =
        change.action === "create"
            ? {}
            : {
                  incident_id: change.outage.id,
                  expected_revision: change.outage.revision,
              };
    return {
        action: change.action,
        reason: change.reason,
        ...target,
        ...(change.action === "create" || change.action === "update"
            ? { values: change.values }
            : {}),
        ...(change.action === "resolve" ? { ends_at: change.ends_at } : {}),
    };
}

// An administrator writes the outage; any other member proposes the same change for approval.
export async function submitOutageChange(
    scope: PlantScope,
    change: OutageChange,
    { proposalKey, direct }: { proposalKey: ProposalKey; direct: boolean },
) {
    if (!direct) {
        const payload = proposal(change);
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/change-requests/incidents/",
            {
                path: scope,
                body: {
                    ...payload,
                    idempotency_key: proposalKey.forPayload(payload),
                },
            },
        );
        proposalKey.reset();
        return;
    }
    if (change.action === "create") {
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/",
            { path: scope, body: change.values },
        );
        return;
    }
    const path = { ...scope, incident_id: change.outage.id };
    const expected_revision = change.outage.revision;
    if (change.action === "update")
        await api.put(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/{incident_id}/",
            {
                path,
                body: { ...change.values, expected_revision },
            },
        );
    else if (change.action === "resolve")
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/{incident_id}/resolve/",
            {
                path,
                body: { expected_revision, ends_at: change.ends_at },
            },
        );
    else
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/{incident_id}/cancel/",
            {
                path,
                body: { expected_revision },
            },
        );
}

function isOutage(value: unknown): value is Outage {
    return (
        !!value &&
        typeof value === "object" &&
        "id" in value &&
        typeof value.id === "number" &&
        "revision" in value &&
        typeof value.revision === "number"
    );
}

export function staleVersion(error: unknown, id: number): Outage | null {
    if (
        !(error instanceof ApiError) ||
        error.code !== "incident_revision_changed" ||
        !error.body ||
        typeof error.body !== "object" ||
        !("current" in error.body)
    )
        return null;
    const current = error.body.current;
    return isOutage(current) && current.id === id ? current : null;
}

export const OUTAGE_FIELDS = [
    "status",
    "kind",
    "title",
    "starts_at",
    "ends_at",
    "available_dc_mw",
    "available_inverter_mw",
    "available_export_mw",
    "notes",
] as const satisfies readonly (keyof Outage)[];

export type OutageField = (typeof OUTAGE_FIELDS)[number];

export function changedFields(before: Outage, after: Outage): OutageField[] {
    return OUTAGE_FIELDS.filter((field) => before[field] !== after[field]);
}

export function capacityImpact(
    outage?: Outage,
): "total" | "reduced" | "unknown" {
    if (!outage) return "total";
    const values = [
        outage.available_dc_mw,
        outage.available_inverter_mw,
        outage.available_export_mw,
    ];
    if (values.every((value) => value === null)) return "unknown";
    return values.every((value) => value === 0) ? "total" : "reduced";
}
