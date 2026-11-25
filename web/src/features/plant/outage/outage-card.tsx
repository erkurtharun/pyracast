"use client";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { PendingApproval } from "@/features/inbox/pending-approval";
import { DisplayError } from "@/lib/api/errors";
import { toPlantInput } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import {
    ActionForm,
    CheckboxField,
    Field,
    formText,
    type Submitted,
} from "@/ui/form";
import { Notice } from "@/ui/ui";
import { StatusTag, useLabel } from "@/ui/vocabulary";
import type { Outage, OutageChange, OutageControls, Workflow } from "./model";
import {
    plantInstant,
    ProposalReason,
    repeatsWith,
    useHeldFocus,
    type Repeats,
} from "./outage-editor";
import { StaleChanges } from "./stale-changes";

type Submit = (change: OutageChange) => Promise<Submitted>;

function Capacity({ label, value }: { label: string; value: number | null }) {
    const t = useTranslations("Outages");
    const format = useFormat();
    return (
        <div>
            <dt>{label}</dt>
            <dd>
                {value == null
                    ? t("unknown")
                    : t("power", { value: format.number(value, 2) })}
            </dd>
        </div>
    );
}

export function OutageCard({
    outage,
    tenantId,
    pendingRequestId,
    timezone,
    direct,
    offered,
    controls,
    hold,
    configurationChanged,
    workflow,
    onOpen,
    onClose,
    onDiscard,
    onSubmit,
    attempt,
}: {
    outage: Outage;
    tenantId: number;
    pendingRequestId: number | null;
    timezone: string;
    direct: boolean;
    // Whether this card offers its proposal controls; they act only while nothing on the screen is being sent.
    offered: boolean;
    controls: OutageControls;
    hold: string | null;
    configurationChanged: boolean;
    // The screen's open resolution or cancellation when it belongs to this outage.
    workflow: Workflow | null;
    onOpen: (kind: "update" | "resolve" | "cancel") => void;
    onClose: () => void;
    onDiscard: () => void;
    onSubmit: Submit;
    attempt: Repeats | null | undefined;
}) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const format = useFormat();
    const label = useLabel();
    const cancellation = useRef<HTMLDetailsElement>(null);
    const summary = useRef<HTMLElement>(null);
    const time = (value: string) => format.time(value, timezone);
    const resolving = workflow?.kind === "resolve" ? workflow : null;
    const cancelling = workflow?.kind === "cancel" ? workflow : null;
    useHeldFocus(cancelling && hold, cancellation, summary);
    return (
        <article className="provenance">
            <div className="section-heading">
                <h3 tabIndex={-1} data-outage={outage.id}>
                    {outage.title}
                </h3>
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
            {pendingRequestId != null && (
                <PendingApproval
                    tenantId={tenantId}
                    requestId={pendingRequestId}
                    subject={outage.title}
                />
            )}
            {offered && (
                <div className="button-group">
                    <button
                        className="button secondary"
                        data-opens="update"
                        data-outage={outage.id}
                        disabled={!controls.acts}
                        onClick={() => onOpen("update")}
                    >
                        {direct ? t("editReport") : proposals("proposeUpdate")}
                    </button>
                    {outage.status === "active" && (
                        <button
                            className="button secondary"
                            data-opens="resolve"
                            data-outage={outage.id}
                            disabled={!controls.acts}
                            onClick={() => onOpen("resolve")}
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
                    workflow={resolving}
                    timezone={timezone}
                    direct={direct}
                    controls={controls}
                    hold={hold}
                    onSubmit={onSubmit}
                    attempt={attempt}
                    onClose={onClose}
                />
            )}
            {(offered || cancelling) && (
                <details ref={cancellation} open={!!cancelling}>
                    {/* The screen decides whether the cancellation is open, so the summary asks it instead of toggling. */}
                    <summary
                        ref={summary}
                        data-opens="cancel"
                        data-outage={outage.id}
                        aria-disabled={!controls.acts || undefined}
                        onClick={(event) => {
                            event.preventDefault();
                            if (cancelling) onClose();
                            else onOpen("cancel");
                        }}
                    >
                        {direct
                            ? t("cancelAnIncorrectEntry")
                            : proposals("proposeCancellation")}
                    </summary>
                    {cancelling && (
                        <CancelOutageForm
                            key={cancelling.form}
                            workflow={cancelling}
                            timezone={timezone}
                            direct={direct}
                            controls={controls}
                            hold={hold}
                            onSubmit={onSubmit}
                            attempt={attempt}
                            onDiscard={onDiscard}
                        />
                    )}
                </details>
            )}
        </article>
    );
}

type FormProps = {
    workflow: Workflow & { outage: Outage };
    timezone: string;
    direct: boolean;
    controls: OutageControls;
    hold: string | null;
    onSubmit: Submit;
    attempt: Repeats | null | undefined;
};

function ResolveOutageForm({
    workflow,
    timezone,
    direct,
    controls,
    hold,
    onSubmit,
    attempt,
    onClose,
}: FormProps & { onClose: () => void }) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const [openedAt] = useState(Date.now);
    const container = useRef<HTMLDivElement>(null);
    const closeButton = useRef<HTMLButtonElement>(null);
    useHeldFocus(hold, container, closeButton);
    const { outage, stale } = workflow;
    const changeOf = (data: FormData): OutageChange => {
        const ends = plantInstant(data, "ends_at", timezone, t);
        if (!ends) throw new DisplayError(t("chooseEndTime"));
        if (Date.parse(ends) <= Date.parse(outage.starts_at))
            throw new DisplayError(t("resolutionAfterStart"));
        return {
            action: "resolve",
            outage,
            ends_at: ends,
            reason: formText(data, "reason"),
        };
    };
    return (
        <div ref={container}>
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
                pending={controls.pending}
                success={null}
                submit={direct ? t("saveResolution") : proposals("send")}
                attempt={attempt && { repeats: repeatsWith(attempt, changeOf) }}
                onSubmit={(data) => onSubmit(changeOf(data))}
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
            <div className="button-group">
                <button
                    ref={closeButton}
                    className="button secondary"
                    type="button"
                    disabled={!controls.close}
                    onClick={onClose}
                >
                    {t("closeForm")}
                </button>
            </div>
        </div>
    );
}

function CancelOutageForm({
    workflow,
    timezone,
    direct,
    controls,
    hold,
    onSubmit,
    attempt,
    onDiscard,
}: FormProps & { onDiscard: () => void }) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const { outage, stale } = workflow;
    const changeOf = (data: FormData): OutageChange => ({
        action: "cancel",
        outage,
        reason: formText(data, "reason"),
    });
    return (
        <>
            {stale && (
                <StaleChanges
                    stale={stale}
                    controls={controls}
                    timezone={timezone}
                    onDiscard={onDiscard}
                />
            )}
            {hold && <Notice>{hold}</Notice>}
            <ActionForm
                permitted={controls.editable}
                pending={controls.pending}
                success={null}
                submit={direct ? t("cancelOutage") : proposals("send")}
                attempt={attempt && { repeats: repeatsWith(attempt, changeOf) }}
                onSubmit={(data) => onSubmit(changeOf(data))}
            >
                {!direct && <ProposalReason />}
                <CheckboxField label={t("confirmationIncorrect")} required />
            </ActionForm>
        </>
    );
}
