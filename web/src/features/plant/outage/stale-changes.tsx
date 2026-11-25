"use client";
import { useTranslations } from "next-intl";
import { useFormat } from "@/lib/format/use-format";
import { MISSING } from "@/lib/format/number";
import { ChangedFields } from "@/ui/changed-fields";
import { useLabel } from "@/ui/vocabulary";
import {
    changedFields,
    type Outage,
    type OutageControls,
    type OutageField,
    type Stale,
} from "./model";

const FIELD_LABELS = {
    status: "status",
    kind: "outageType",
    title: "incidentTitle",
    starts_at: "startedAt",
    ends_at: "resolvedAt",
    available_dc_mw: "availablePanelDCCapacityMW",
    available_inverter_mw: "availableInverterACCapacityMW",
    available_export_mw: "availableGridExportCapacityMW",
    notes: "notes",
} as const satisfies Record<OutageField, string>;

// Says what changed; why a held workflow cannot send any more is its hold notice's to say.
export function StaleChanges({
    stale,
    controls,
    timezone,
    onDiscard,
}: {
    stale: Stale;
    controls: OutageControls;
    timezone: string;
    onDiscard: () => void;
}) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const format = useFormat();
    const label = useLabel();
    const { before, current } = stale;
    const show = (outage: Outage, field: OutageField) => {
        if (field === "status") return label("IncidentStatus", outage.status);
        if (field === "kind") return label("IncidentKind", outage.kind);
        if (field === "starts_at")
            return format.time(outage.starts_at, timezone);
        if (field === "ends_at")
            return outage.ends_at
                ? format.time(outage.ends_at, timezone)
                : proposals("ongoing");
        if (field === "title" || field === "notes")
            return outage[field] || MISSING;
        const value = outage[field];
        return value === null
            ? t("unknown")
            : t("power", { value: format.number(value, 6) });
    };
    return (
        <div className="notice" role="status">
            <p>{controls.editable ? t("staleApply") : t("staleChanged")}</p>
            <ChangedFields
                changes={changedFields(before, current).map((field) => ({
                    field,
                    label: t(FIELD_LABELS[field]),
                    before: show(before, field),
                    after: show(current, field),
                }))}
            />
            <button
                className="button secondary"
                type="button"
                disabled={!controls.close}
                onClick={onDiscard}
            >
                {t("discardEdit")}
            </button>
        </div>
    );
}
