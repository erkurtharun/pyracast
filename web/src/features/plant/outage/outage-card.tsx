"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { DisplayError } from "@/lib/api/errors";
import { toPlantInput } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import { ActionForm, CheckboxField, Field, formText } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { StatusTag, useLabel } from "@/ui/vocabulary";
import type { Outage, OutageChange } from "./model";
import {
    plantInstant,
    ProposalReason,
    useStaleRecovery,
} from "./outage-editor";
import { StaleChanges } from "./stale-changes";

type Submit = (change: OutageChange) => Promise<void>;

function Capacity({ label, value }: { label: string; value: number | null }) {
    const t = useTranslations("Outages");
    const format = useFormat();
    return (
        <div>
            <dt>{label}</dt>
            <dd>
                {value == null
                    ? t("unknown")
                    : t("power", { value: format.number(value, 6) })}
            </dd>
        </div>
    );
}

export function OutageCard({
    outage,
    timezone,
    direct,
    configurationChanged,
    resolving,
    onEdit,
    onResolve,
    onSubmit,
}: {
    outage: Outage;
    timezone: string;
    direct: boolean;
    configurationChanged: boolean;
    resolving: boolean;
    onEdit: () => void;
    onResolve: (open: boolean) => void;
    onSubmit: Submit;
}) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const format = useFormat();
    const label = useLabel();
    const time = (value: string) => format.time(value, timezone);
    return (
        <article className="provenance">
            <div className="section-heading">
                <h3>{outage.title}</h3>
                <StatusTag group="IncidentStatus" value={outage.status} />
            </div>
            <p>
                {t("started", {
                    kind: label("IncidentKind", outage.kind),
                    time: time(outage.starts_at),
                })}
                {outage.status === "cancelled"
                    ? t("cancelledEntry")
                    : outage.ends_at
                      ? t("resolved", { time: time(outage.ends_at) })
                      : t("ongoing")}
            </p>
            <dl className="stats">
                <Capacity
                    label={t("availablePanels")}
                    value={outage.available_dc_mw}
                />
                <Capacity
                    label={t("availableInverter")}
                    value={outage.available_inverter_mw}
                />
                <Capacity
                    label={t("availableExport")}
                    value={outage.available_export_mw}
                />
            </dl>
            {outage.notes && <p>{outage.notes}</p>}
            {configurationChanged && (
                <Notice>{t("configurationChanged")}</Notice>
            )}
            {outage.status !== "cancelled" && (
                <div className="button-group">
                    <button className="button secondary" onClick={onEdit}>
                        {direct ? t("editReport") : proposals("proposeUpdate")}
                    </button>
                    {outage.status === "active" && (
                        <button
                            className="button secondary"
                            onClick={() => onResolve(true)}
                        >
                            {direct
                                ? t("resolveOutage")
                                : proposals("proposeResolution")}
                        </button>
                    )}
                </div>
            )}
            {resolving && (
                <ResolveOutageForm
                    outage={outage}
                    timezone={timezone}
                    direct={direct}
                    onSubmit={onSubmit}
                    onClose={() => onResolve(false)}
                />
            )}
            {outage.status !== "cancelled" && (
                <details>
                    <summary>
                        {direct
                            ? t("cancelAnIncorrectEntry")
                            : proposals("proposeCancellation")}
                    </summary>
                    <CancelOutageForm
                        key={outage.id}
                        outage={outage}
                        timezone={timezone}
                        direct={direct}
                        onSubmit={onSubmit}
                    />
                </details>
            )}
        </article>
    );
}

function ResolveOutageForm({
    outage,
    timezone,
    direct,
    onSubmit,
    onClose,
}: {
    outage: Outage;
    timezone: string;
    direct: boolean;
    onSubmit: Submit;
    onClose: () => void;
}) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const recovery = useStaleRecovery(outage);
    const [openedAt] = useState(Date.now);
    const closed = recovery.base?.status !== "active";
    return (
        <>
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
                submit={direct ? t("saveResolution") : proposals("send")}
                onSubmit={async (data) => {
                    const ends = plantInstant(data, "ends_at", timezone, t);
                    if (!ends) throw new DisplayError(t("chooseEndTime"));
                    const reason = formText(data, "reason");
                    await recovery.attempt((base) => {
                        const target = base ?? outage;
                        if (Date.parse(ends) <= Date.parse(target.starts_at))
                            throw new DisplayError(t("resolutionAfterStart"));
                        return onSubmit({
                            action: "resolve",
                            outage: target,
                            ends_at: ends,
                            reason,
                        });
                    });
                    onClose();
                }}
            >
                {!direct && <ProposalReason />}
                <Field
                    label={t("resolvedAt")}
                    name="ends_at"
                    type="datetime-local"
                    defaultValue={toPlantInput(openedAt, timezone)}
                    required
                />
            </ActionForm>
        </>
    );
}

function CancelOutageForm({
    outage,
    timezone,
    direct,
    onSubmit,
}: {
    outage: Outage;
    timezone: string;
    direct: boolean;
    onSubmit: Submit;
}) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const recovery = useStaleRecovery(outage);
    const closed = recovery.base?.status === "cancelled";
    return (
        <>
            {recovery.stale && (
                <StaleChanges
                    stale={recovery.stale}
                    closed={closed}
                    timezone={timezone}
                    onDiscard={recovery.discard}
                />
            )}
            <ActionForm
                key={recovery.form}
                permitted={!closed}
                submit={direct ? t("cancelOutage") : proposals("send")}
                onSubmit={async (data) => {
                    const reason = formText(data, "reason");
                    await recovery.attempt((base) =>
                        onSubmit({
                            action: "cancel",
                            outage: base ?? outage,
                            reason,
                        }),
                    );
                }}
            >
                {!direct && <ProposalReason />}
                <CheckboxField label={t("confirmationIncorrect")} required />
            </ActionForm>
        </>
    );
}
