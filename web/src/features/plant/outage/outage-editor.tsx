"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { DisplayError } from "@/lib/api/errors";
import { fromPlantInput, toPlantInput } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";
import { ActionForm, Field, Select, TextAreaField, formText } from "@/ui/form";
import { useLabel } from "@/ui/vocabulary";
import {
    capacityImpact,
    staleVersion,
    type Outage,
    type OutageChange,
} from "./model";
import { StaleChanges, type Stale } from "./stale-changes";

export type Ceilings = {
    dc_capacity_mw: number;
    inverter_capacity_mw: number | null;
    export_limit_mw: number | null;
};

const INCIDENT_KINDS = [
    "inverter",
    "panels",
    "grid",
    "other",
    "multiple",
] as const satisfies readonly components["schemas"]["IncidentKindEnum"][];

const IMPACTS = ["total", "reduced", "unknown"] as const;

type Translate = ReturnType<typeof useTranslations<"Outages">>;

// An entered wall-clock time read in the plant zone; empty gives null.
export function plantInstant(
    data: FormData,
    key: string,
    timezone: string,
    t: Translate,
): string | null {
    const value = formText(data, key);
    if (!value) return null;
    const instant = fromPlantInput(value, timezone);
    if (!instant) throw new DisplayError(t("nonexistentTime"));
    if (Date.parse(instant) > Date.now())
        throw new DisplayError(t("invalidTime"));
    return instant;
}

export function ProposalReason() {
    const t = useTranslations("ChangeRequests");
    return (
        <TextAreaField
            label={t("optionalReason")}
            name="reason"
            maxLength={500}
        />
    );
}

// A form that keeps what the person typed when the outage changed meanwhile and saves it against the version shown.
export function useStaleRecovery(outage: Outage | undefined) {
    const t = useTranslations("Outages");
    const [base, setBase] = useState(outage);
    const [stale, setStale] = useState<Stale | null>(null);
    const [attempts, setAttempts] = useState(0);
    return {
        base,
        stale,
        form: attempts,
        discard: () => {
            setStale(null);
            setAttempts((count) => count + 1);
        },
        async attempt(write: (base: Outage | undefined) => Promise<void>) {
            try {
                await write(base);
            } catch (error) {
                const current = base && staleVersion(error, base.id);
                if (!current) throw error;
                setStale({ before: stale?.before ?? base, current });
                setBase(current);
                throw new DisplayError(t("staleEdit"));
            }
        },
    };
}

export function OutageEditor({
    outage,
    timezone,
    ceilings,
    direct,
    onSubmit,
    onClose,
}: {
    outage?: Outage;
    timezone: string;
    ceilings?: Ceilings;
    direct: boolean;
    onSubmit: (change: OutageChange) => Promise<void>;
    onClose: () => void;
}) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const format = useFormat();
    const label = useLabel();
    const recovery = useStaleRecovery(outage);
    const [impact, setImpact] = useState(capacityImpact(outage));
    const [openedAt] = useState(Date.now);
    const closed = recovery.base?.status === "cancelled";
    const ceiling = (value?: number | null) =>
        value == null
            ? undefined
            : t("capacityCeiling", { value: format.number(value, 6) });
    const heading = outage ? t("editOutage") : t("reportOutage");
    return (
        <section aria-label={heading}>
            <h3>{heading}</h3>
            {recovery.stale && (
                <StaleChanges
                    stale={recovery.stale}
                    closed={closed}
                    timezone={timezone}
                    onDiscard={onClose}
                />
            )}
            <ActionForm
                permitted={!closed}
                submit={
                    direct
                        ? outage
                            ? t("saveOutageChanges")
                            : t("saveOutage")
                        : proposals("send")
                }
                onSubmit={async (data) => {
                    const starts = plantInstant(data, "starts_at", timezone, t);
                    const ends = plantInstant(data, "ends_at", timezone, t);
                    if (!starts)
                        throw new DisplayError(t("enterTheOutageStart"));
                    if (ends && Date.parse(ends) <= Date.parse(starts))
                        throw new DisplayError(t("resolutionAfterStart"));
                    const kind = INCIDENT_KINDS.find(
                        (value) => value === data.get("kind"),
                    );
                    if (!kind)
                        throw new DisplayError(t("chooseAValidOutageType"));
                    const capacity = (key: string) =>
                        impact === "total"
                            ? 0
                            : impact === "unknown" ||
                                formText(data, key).trim() === ""
                              ? null
                              : Number(data.get(key));
                    const capacities = {
                        available_dc_mw: capacity("available_dc_mw"),
                        available_inverter_mw: capacity(
                            "available_inverter_mw",
                        ),
                        available_export_mw: capacity("available_export_mw"),
                    };
                    if (
                        impact === "reduced" &&
                        Object.values(capacities).every(
                            (value) => value === null,
                        )
                    )
                        throw new DisplayError(t("capacityRequired"));
                    const values = {
                        kind,
                        title: formText(data, "title"),
                        notes: formText(data, "notes"),
                        starts_at: starts,
                        ends_at: ends,
                        ...capacities,
                    };
                    const reason = formText(data, "reason");
                    await recovery.attempt((base) =>
                        onSubmit(
                            base
                                ? {
                                      action: "update",
                                      outage: base,
                                      values,
                                      reason,
                                  }
                                : { action: "create", values, reason },
                        ),
                    );
                    onClose();
                }}
            >
                {!direct && <ProposalReason />}
                <div className="form-grid">
                    <Select
                        label={t("outageType")}
                        name="kind"
                        defaultValue={outage?.kind ?? "inverter"}
                        required
                    >
                        {INCIDENT_KINDS.map((kind) => (
                            <option key={kind} value={kind}>
                                {label("IncidentKind", kind)}
                            </option>
                        ))}
                    </Select>
                    <Field
                        label={t("incidentTitle")}
                        name="title"
                        defaultValue={outage?.title}
                        required
                    />
                    <Field
                        label={t("startedAt")}
                        name="starts_at"
                        type="datetime-local"
                        defaultValue={toPlantInput(
                            outage?.starts_at ?? openedAt,
                            timezone,
                        )}
                        required
                    />
                    <Field
                        label={t("resolvedAtOptional")}
                        name="ends_at"
                        type="datetime-local"
                        defaultValue={toPlantInput(outage?.ends_at, timezone)}
                    />
                </div>
                <Select
                    label={t("capacityImpact")}
                    value={impact}
                    onChange={(event) =>
                        setImpact(
                            IMPACTS.find(
                                (value) => value === event.target.value,
                            ) ?? "total",
                        )
                    }
                >
                    <option value="total">{t("totalOutage")}</option>
                    <option value="reduced">{t("reducedCapacity")}</option>
                    <option value="unknown">{t("unknownImpact")}</option>
                </Select>
                {impact === "reduced" && (
                    <>
                        <div className="form-grid">
                            <Field
                                label={t("availablePanelDCCapacityMW")}
                                name="available_dc_mw"
                                type="number"
                                min={0}
                                max={ceilings?.dc_capacity_mw}
                                hint={ceiling(ceilings?.dc_capacity_mw)}
                                step="any"
                                defaultValue={outage?.available_dc_mw ?? ""}
                            />
                            <Field
                                label={t("availableInverterACCapacityMW")}
                                name="available_inverter_mw"
                                type="number"
                                min={0}
                                max={
                                    ceilings?.inverter_capacity_mw ?? undefined
                                }
                                hint={ceiling(ceilings?.inverter_capacity_mw)}
                                step="any"
                                defaultValue={
                                    outage?.available_inverter_mw ?? ""
                                }
                            />
                            <Field
                                label={t("availableGridExportCapacityMW")}
                                name="available_export_mw"
                                type="number"
                                min={0}
                                max={ceilings?.export_limit_mw ?? undefined}
                                hint={ceiling(ceilings?.export_limit_mw)}
                                step="any"
                                defaultValue={outage?.available_export_mw ?? ""}
                            />
                        </div>
                        <details>
                            <summary>{t("capacityEntryHelp")}</summary>
                            <p className="fine">{t("remainingCapacityHelp")}</p>
                            <p className="fine">{t("capacityHelp")}</p>
                        </details>
                    </>
                )}
                <TextAreaField
                    label={t("notesOptional")}
                    name="notes"
                    defaultValue={outage?.notes}
                />
            </ActionForm>
            <div className="button-group">
                <button
                    className="button secondary"
                    type="button"
                    onClick={onClose}
                >
                    {t("closeEditor")}
                </button>
            </div>
        </section>
    );
}
