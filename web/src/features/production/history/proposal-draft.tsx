"use client";
import { useTranslations } from "next-intl";
import { useId, useRef, useState } from "react";
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
    changedSinceStaged,
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
    rows,
    intervalLabel,
    editing,
    onEdit,
    onRemove,
    onDiscard,
    onReload,
    onSent,
}: {
    scope: PlantScope;
    configuration: number;
    proposal: Proposal;
    // The rows of the draft's day, when that day is shown.
    rows: Interval[] | undefined;
    intervalLabel: (item: StagedChange) => string;
    editing: boolean;
    onEdit: (key: string) => void;
    onRemove: (key: string) => void;
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
    const [reason, setReason] = useState("");
    const [send, setSend] = useState<ProposalSend>({ status: "idle" });
    const { items } = proposal;
    const count = items.length;
    const sending = send.status === "sending";
    const stale = rows ? changedSinceStaged(items, rows) : new Set<string>();
    const proposable = new Set(
        (rows ?? [])
            .filter((row) => row.can_propose)
            .map((row) => intervalKey(row)),
    );
    const refused = (item: StagedChange) =>
        send.status === "failed"
            ? send.refusals.find((refusal) => refusal.item === item)
            : undefined;

    async function submit() {
        if (sending) return;
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
                                    {t("stagedChanged")}
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
                                        sending ||
                                        editing ||
                                        !proposable.has(item.key)
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
                <ControlField label={t("reason")} hint={t("approvalHelp")}>
                    {(describedBy) => (
                        <textarea
                            required
                            maxLength={500}
                            aria-describedby={describedBy}
                            value={reason}
                            disabled={sending}
                            onChange={(event) => setReason(event.target.value)}
                        />
                    )}
                </ControlField>
                {send.status === "failed" && (
                    <Notice error>{send.error}</Notice>
                )}
                <div className="button-group">
                    <button
                        className="button"
                        type="submit"
                        disabled={sending || editing}
                    >
                        {sending ? t("sending") : t("send")}
                    </button>
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
