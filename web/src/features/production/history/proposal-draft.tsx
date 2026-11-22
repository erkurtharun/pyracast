"use client";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef } from "react";
import type { PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { useFormat } from "@/lib/format/use-format";
import { useProposalKey } from "@/lib/idempotency";
import { ControlField, useErrorMessage } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";
import type { Interval } from "./model";
import {
    fieldRefusals,
    proposalChanges,
    refusals,
    type StagedChange,
} from "./proposal";
import type { Controls, Proposal, Screen, ScreenEvent } from "./screen";

export function ProposalDraft({
    scope,
    configuration,
    draft,
    controls,
    rows,
    intervalLabel,
    request,
    onEvent,
    onEdit,
    onRemove,
    onReturn,
    onDiscard,
    onReload,
    onSent,
}: {
    scope: PlantScope;
    configuration: number;
    draft: Proposal;
    controls: Controls;
    // The rows of the shown day.
    rows: Interval[] | undefined;
    intervalLabel: (item: StagedChange) => string;
    // Applies an event to the screen and returns the next state, or null when the event is refused.
    request: (event: ScreenEvent) => Screen | null;
    onEvent: (event: ScreenEvent) => void;
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
    const heldId = useId();
    const heading = useRef<HTMLHeadingElement>(null);
    const reasonRef = useRef<HTMLTextAreaElement>(null);
    const running = useRef(false);
    const { items, send } = draft;
    const count = items.length;
    const sending = send.status === "sending";
    const held = controls.held.size > 0;
    const refused = (item: StagedChange) =>
        send.status === "failed"
            ? send.refusals.find((refusal) => refusal.key === item.key)
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
        if (running.current) return;
        const sent = request({ type: "send", rows })?.draft;
        if (sent?.send.status !== "sending") return;
        running.current = true;
        const body = {
            configuration_id: configuration,
            unit: "MWh" as const,
            changes: proposalChanges(sent.items),
            reason: sent.reason.trim(),
        };
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
            onEvent({
                type: "sendFailed",
                error: errorMessage(error),
                refusals: refusals(error, sent.items),
                fields: fieldRefusals(error),
            });
            // A refusal of what was sent shows the current rows, so the stale items are named.
            if (
                error instanceof ApiError &&
                (error.status === 400 || error.status === 409)
            )
                void onReload().catch(() => undefined);
        } finally {
            running.current = false;
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
                {t("intervals", { count })} · {draft.day}. {t("draftDayLocked")}
            </p>
            {controls.away && (
                <Notice>{t("draftOtherDay", { day: draft.day })}</Notice>
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
                            {controls.stale.has(item.key) && (
                                <span className="field-error">
                                    {controls.held.has(item.key)
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
                                    disabled={!controls.editable.has(item.key)}
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
                                    disabled={!controls.remove}
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
                            value={draft.reason}
                            disabled={!controls.reason}
                            onChange={(event) =>
                                onEvent({
                                    type: "reason",
                                    text: event.target.value,
                                })
                            }
                        />
                    )}
                </ControlField>
                {send.status === "failed" && (
                    <Notice error>
                        {[send.error, ...send.fields.general].join(" ")}
                    </Notice>
                )}
                {held && (
                    <p className="field-error" id={heldId} role="status">
                        {t("draftHeld")}
                    </p>
                )}
                <div className="button-group">
                    <button
                        className="button"
                        type="submit"
                        disabled={!controls.send}
                        aria-describedby={held ? heldId : undefined}
                    >
                        {sending ? t("sending") : t("send")}
                    </button>
                    {controls.away && (
                        <button
                            className="button secondary"
                            type="button"
                            onClick={onReturn}
                        >
                            {t("returnToDraftDay", { day: draft.day })}
                        </button>
                    )}
                    <button
                        className="button secondary"
                        type="button"
                        disabled={!controls.discard}
                        onClick={onDiscard}
                    >
                        {t("discardDraft")}
                    </button>
                </div>
            </form>
        </section>
    );
}
