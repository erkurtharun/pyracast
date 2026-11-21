"use client";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { useFormat } from "@/lib/format/use-format";
import { useProposalKey } from "@/lib/idempotency";
import { ControlField, useErrorMessage } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";
import { intervalKey, type Interval } from "./model";
import {
    canStage,
    changedSinceStaged,
    fieldRefusals,
    proposalChanges,
    refusals,
    type ProposalSend,
    type StagedChange,
} from "./proposal";

export type Proposal = { day: string; items: StagedChange[] };

export function ProposalDraft({
    scope,
    configuration,
    proposal,
    away,
    rows,
    intervalLabel,
    editing,
    onEdit,
    onRemove,
    onReturn,
    onDiscard,
    onReload,
    onSent,
}: {
    scope: PlantScope;
    configuration: number;
    proposal: Proposal;
    // Another day is shown; nothing is staged or sent until the draft's day is shown again.
    away: boolean;
    // The rows of the draft's day, when that day is shown.
    rows: Interval[] | undefined;
    intervalLabel: (item: StagedChange) => string;
    editing: boolean;
    onEdit: (key: string) => void;
    onRemove: (key: string) => void;
    onReturn: () => void;
    onDiscard: () => void;
    onReload: () => Promise<unknown>;
    onSent: () => void;
}) {
    const t = useTranslations("ChangeRequests");
    const format = useFormat();
    const label = useLabel();
    const errorMessage = useErrorMessage();
    const proposalKey = useProposalKey();
    const headingId = useId();
    const heading = useRef<HTMLHeadingElement>(null);
    const reasonRef = useRef<HTMLTextAreaElement>(null);
    const [reason, setReason] = useState("");
    const [send, setSend] = useState<ProposalSend>({ status: "idle" });
    const { items } = proposal;
    const count = items.length;
    const sending = send.status === "sending";
    const current = new Map((rows ?? []).map((row) => [intervalKey(row), row]));
    const stale = rows ? changedSinceStaged(items, rows) : new Set<string>();
    const editable = (item: StagedChange) => {
        const row = current.get(item.key);
        return row !== undefined && canStage(row);
    };
    const refused = (item: StagedChange) =>
        send.status === "failed"
            ? send.refusals.find((refusal) => refusal.item === item)
            : undefined;
    const reasonError =
        send.status === "reason_missing"
            ? t("reasonRequired")
            : send.status === "failed"
              ? send.fields.reason
              : undefined;
    useEffect(() => {
        if (send.status === "reason_missing") reasonRef.current?.focus();
    }, [send]);

    async function submit() {
        if (sending || away) return;
        if (!reason.trim()) {
            setSend({ status: "reason_missing" });
            return;
        }
        const body = {
            configuration_id: configuration,
            unit: "MWh" as const,
            changes: proposalChanges(items),
            reason: reason.trim(),
        };
        setSend({ status: "sending" });
        try {
            await api.post(
                "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/change-requests/production/",
                {
                    path: scope,
                    body: {
                        ...body,
                        idempotency_key: proposalKey.forPayload(body),
                    },
                },
            );
            proposalKey.reset();
            onSent();
        } catch (error) {
            setSend({
                status: "failed",
                error: errorMessage(error),
                refusals: refusals(error, items),
                fields: fieldRefusals(error),
            });
            // A refusal of what was sent shows the current rows, so the stale items are named.
            if (
                error instanceof ApiError &&
                (error.status === 400 || error.status === 409)
            )
                void onReload().catch(() => undefined);
        }
    }

    return (
        <section
            className="production-proposal"
            aria-labelledby={headingId}
            aria-busy={sending}
        >
            <h3 id={headingId} ref={heading} tabIndex={-1}>
                {t("draftTitle")}
            </h3>
            <p className="fine">
                {t("intervals", { count })} · {proposal.day}.{" "}
                {t("draftDayLocked")}
            </p>
            {away && (
                <Notice>{t("draftOtherDay", { day: proposal.day })}</Notice>
            )}
            <ul className="production-proposal-items">
                {items.map((item) => {
                    const interval = intervalLabel(item);
                    const refusal = refused(item);
                    return (
                        <li key={item.key}>
                            <span>
                                <strong>{interval}</strong>{" "}
                                {item.action === "revert"
                                    ? t("stagedProviderReading")
                                    : `${format.quantity(item.energy, "MWh", 6)} · ${label("ObservationQuality", item.quality)}`}
                            </span>
                            {stale.has(item.key) && (
                                <span className="field-error">
                                    {current.get(item.key)
                                        ?.pending_request_id != null
                                        ? t("stagedPending")
                                        : t("stagedChanged")}
                                </span>
                            )}
                            {refusal && (
                                <span className="field-error">
                                    {refusal.message}
                                </span>
                            )}
                            <span className="button-group">
                                <button
                                    className="button secondary"
                                    type="button"
                                    aria-label={t("editStagedInterval", {
                                        interval,
                                    })}
                                    disabled={
                                        sending || editing || !editable(item)
                                    }
                                    onClick={() => onEdit(item.key)}
                                >
                                    {t("editStaged")}
                                </button>
                                <button
                                    className="button secondary"
                                    type="button"
                                    aria-label={t("removeStagedInterval", {
                                        interval,
                                    })}
                                    disabled={sending}
                                    onClick={() => {
                                        onRemove(item.key);
                                        if (count > 1) heading.current?.focus();
                                    }}
                                >
                                    {t("removeStaged")}
                                </button>
                            </span>
                        </li>
                    );
                })}
            </ul>
            <form
                onSubmit={(event) => {
                    event.preventDefault();
                    void submit();
                }}
            >
                <ControlField
                    label={t("reason")}
                    hint={t("approvalHelp")}
                    error={reasonError}
                >
                    {(describedBy) => (
                        <textarea
                            ref={reasonRef}
                            required
                            maxLength={500}
                            aria-describedby={describedBy}
                            aria-invalid={reasonError ? true : undefined}
                            value={reason}
                            disabled={sending}
                            onChange={(event) => {
                                setReason(event.target.value);
                                if (send.status === "reason_missing")
                                    setSend({ status: "idle" });
                            }}
                        />
                    )}
                </ControlField>
                {send.status === "failed" && (
                    <Notice error>
                        {[send.error, ...send.fields.general].join(" ")}
                    </Notice>
                )}
                <div className="button-group">
                    <button
                        className="button"
                        type="submit"
                        disabled={sending || editing || away}
                    >
                        {sending ? t("sending") : t("send")}
                    </button>
                    {away && (
                        <button
                            className="button secondary"
                            type="button"
                            onClick={onReturn}
                        >
                            {t("returnToDraftDay", { day: proposal.day })}
                        </button>
                    )}
                    <button
                        className="button secondary"
                        type="button"
                        disabled={sending}
                        onClick={onDiscard}
                    >
                        {t("discardDraft")}
                    </button>
                </div>
            </form>
        </section>
    );
}
