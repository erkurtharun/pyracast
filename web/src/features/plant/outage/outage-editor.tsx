"use client";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { DisplayError } from "@/lib/api/errors";
import { fromPlantInput, toPlantInput } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import { serverTrim } from "@/lib/idempotency";
import type { components } from "@/lib/schema";
import { ActionForm, Field, Select, TextAreaField, formText } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";
import {
    capacityImpact,
    type Outage,
    type OutageChange,
    type OutageControls,
    type Stale,
} from "./model";
import { StaleChanges } from "./stale-changes";

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

export type Repeats = (change: OutageChange) => boolean;

export function repeatsWith(
    repeats: Repeats,
    changeOf: (data: FormData) => OutageChange,
) {
    return (data: FormData) => {
        try {
            return repeats(changeOf(data));
        } catch {
            return false;
        }
    };
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

// A held form is disabled, so a focus inside it, or one lost with a control it disabled, moves to the one control left.
export function useHeldFocus(
    hold: string | null,
    container: RefObject<HTMLElement | null>,
    control: RefObject<HTMLElement | null>,
) {
    useEffect(() => {
        const active = document.activeElement;
        if (
            hold &&
            (active === document.body || container.current?.contains(active))
        )
            control.current?.focus();
    }, [hold, container, control]);
}

export function OutageEditor({
    outage,
    stale,
    timezone,
    ceilings,
    direct,
    controls,
    hold,
    onSubmit,
    attempt,
    onClose,
}: {
    outage?: Outage;
    stale: Stale | null;
    timezone: string;
    ceilings?: Ceilings;
    direct: boolean;
    controls: OutageControls;
    // Why this editor cannot send any more; it can still be closed.
    hold: string | null;
    onSubmit: (change: OutageChange) => Promise<void>;
    attempt: Repeats | null | undefined;
    onClose: () => void;
}) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const format = useFormat();
    const label = useLabel();
    const [impact, setImpact] = useState(capacityImpact(outage));
    const blankHelp = useId();
    const [openedAt] = useState(Date.now);
    const section = useRef<HTMLElement>(null);
    const closeButton = useRef<HTMLButtonElement>(null);
    useHeldFocus(hold, section, closeButton);
    const ceiling = (value?: number | null) =>
        value == null
            ? undefined
            : t("capacityCeiling", { value: format.number(value, 6) });
    const heading = outage ? t("editOutage") : t("reportOutage");
    const changeOf = (data: FormData): OutageChange => {
        const starts = plantInstant(data, "starts_at", timezone, t);
        const ends = plantInstant(data, "ends_at", timezone, t);
        if (!starts) throw new DisplayError(t("enterTheOutageStart"));
        if (ends && Date.parse(ends) <= Date.parse(starts))
            throw new DisplayError(t("resolutionAfterStart"));
        const kind = INCIDENT_KINDS.find((value) => value === data.get("kind"));
        if (!kind) throw new DisplayError(t("chooseAValidOutageType"));
        const capacity = (key: string) =>
            impact === "total"
                ? 0
                : impact === "unknown" || formText(data, key).trim() === ""
                  ? null
                  : Number(data.get(key));
        const capacities = {
            available_dc_mw: capacity("available_dc_mw"),
            available_inverter_mw: capacity("available_inverter_mw"),
            available_export_mw: capacity("available_export_mw"),
        };
        if (
            impact === "reduced" &&
            Object.values(capacities).every((value) => value === null)
        )
            throw new DisplayError(t("capacityRequired"));
        const values = {
            kind,
            title: serverTrim(formText(data, "title")),
            notes: serverTrim(formText(data, "notes")),
            starts_at: starts,
            ends_at: ends,
            ...capacities,
        };
        const reason = formText(data, "reason");
        return outage
            ? { action: "update", outage, values, reason }
            : { action: "create", values, reason };
    };
    return (
        <section aria-label={heading} ref={section}>
            <h3>{heading}</h3>
            {stale && (
                <StaleChanges
                    stale={stale}
                    controls={controls}
                    timezone={timezone}
                    onDiscard={onClose}
                />
            )}
            {hold && <Notice>{hold}</Notice>}
            <ActionForm
                permitted={controls.editable}
                pending={!controls.acts}
                success={null}
                submit={
                    direct
                        ? outage
                            ? t("saveOutageChanges")
                            : t("saveOutage")
                        : proposals("send")
                }
                attempt={attempt && { repeats: repeatsWith(attempt, changeOf) }}
                onSubmit={(data) => onSubmit(changeOf(data))}
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
                        <div
                            className="form-grid"
                            role="group"
                            aria-label={t("reducedCapacity")}
                            aria-describedby={blankHelp}
                        >
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
                        <p id={blankHelp} className="fine">
                            {t("remainingCapacityHelp")}
                        </p>
                    </>
                )}
                <p className="fine">{t("reportingHelp")}</p>
                <p className="fine">{t("editingHelp")}</p>
                <TextAreaField
                    label={t("notesOptional")}
                    name="notes"
                    defaultValue={outage?.notes}
                />
            </ActionForm>
            <div className="button-group">
                <button
                    ref={closeButton}
                    className="button secondary"
                    type="button"
                    disabled={!controls.close}
                    onClick={onClose}
                >
                    {t("closeEditor")}
                </button>
            </div>
        </section>
    );
}
