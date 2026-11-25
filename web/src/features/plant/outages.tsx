"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { Plant, PlantScope } from "@/lib/api";
import { DisplayError, UnconfirmedError } from "@/lib/api/errors";
import { useApiList } from "@/lib/api/query";
import { outagesExport } from "@/lib/exports";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import { UnconfirmedNotice } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { Notice } from "@/ui/ui";
import { useCodeLabel, useLabel, useReceipt } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import {
    attemptPayload,
    INITIAL_OUTAGES,
    isEnding,
    outageControls,
    reduceOutages,
    staleVersion,
    submitOutageChange,
    type Hold,
    type Live,
    type Outage,
    type OutageChange,
    type OutageEvent,
    type OutageRow,
    type OutageScreen,
    type Workflow,
} from "./outage/model";
import { OutageCard } from "./outage/outage-card";
import { OutageEditor, type Ceilings } from "./outage/outage-editor";

type Configuration = NonNullable<Plant["current_configuration"]>;

// After a workflow closed or was sent, the focus it lost goes to the control that opened it, else to its outage's
// heading, else to the screen's status messages, else to the screen's heading; a hidden candidate takes no focus.
function returnFocus(screen: HTMLElement, closed: Workflow) {
    const outage = closed.kind === "create" ? "" : String(closed.outage.id);
    const candidates = [
        screen.querySelector(
            `[data-opens="${closed.kind}"][data-outage="${outage}"]`,
        ),
        outage && screen.querySelector(`h3[data-outage="${outage}"]`),
        screen.querySelector("[data-screen-status]:not(:empty)"),
        screen.querySelector("h2"),
    ];
    for (const candidate of candidates) {
        if (!(candidate instanceof HTMLElement)) continue;
        candidate.focus();
        if (document.activeElement === candidate) return;
    }
}

export function Outages({
    scope,
    timezone,
    onChanged,
    configuration,
}: {
    scope: PlantScope;
    timezone: string;
    onChanged: () => Promise<unknown>;
    configuration?: Configuration | null;
}) {
    const t = useTranslations("Outages");
    const reason = useCodeLabel("Outages.proposalReasons");
    const label = useLabel();
    const proposals = useTranslations("ChangeRequests");
    const receipt = useReceipt();
    const direct = useCanManageWorkspace();
    const plant = useExportPlant();
    const [screen, setScreen] = useState(INITIAL_OUTAGES);
    const [reconciled, setReconciled] = useState<Live>();
    const [reloads, setReloads] = useState(0);
    const resource = useApiList(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/",
        { path: scope },
    );
    const offer = resource.data;
    const live: Live = { offer, direct, configured: !!configuration };
    const apply = (event: OutageEvent) =>
        setScreen((current) => reduceOutages(current, event));
    // The open workflow meets every list it is shown beside, and the viewer's access when that changes alone; a
    // workflow that is sending meets what changed meanwhile once its answer arrived.
    if (
        offer &&
        screen.workflow?.phase.status !== "sending" &&
        (offer !== reconciled?.offer || direct !== reconciled.direct)
    ) {
        setReconciled(live);
        apply({ type: "rowsLoaded", live });
    }
    const derived = outageControls(screen, live);
    const { workflow } = screen;
    const section = useRef<HTMLElement>(null);
    const shown = useRef(workflow);
    useEffect(() => {
        const closed = shown.current;
        shown.current = workflow;
        const active = document.activeElement;
        if (!closed || workflow || !section.current) return;
        if (active && active !== document.body) return;
        returnFocus(section.current, closed);
    }, [workflow]);

    async function reload() {
        setReloads((count) => count + 1);
        try {
            await onChanged();
        } finally {
            setReloads((count) => count - 1);
        }
    }
    const configured = configuration?.id ?? null;
    async function submit(entered: OutageChange) {
        const next = reduceOutages(screen, {
            type: "send",
            payload: attemptPayload(entered, direct, configured),
            key: crypto.randomUUID(),
            change: entered,
        });
        const phase = next.workflow?.phase;
        if (next === screen || phase?.status !== "sending") return;
        setScreen(next);
        const { change, key } = phase.sent;
        let proposal: Awaited<ReturnType<typeof submitOutageChange>>;
        try {
            proposal = await submitOutageChange(scope, change, {
                key,
                direct,
            });
        } catch (error) {
            const current =
                change.action === "create"
                    ? null
                    : staleVersion(error, change.outage.id);
            const failed = reduceOutages(next, {
                type: "sendFailed",
                change,
                current,
                error,
                direct,
            });
            setScreen(failed);
            if (failed.workflow?.phase.status === "saved")
                return finishSent(null);
            if (!current) {
                if (error instanceof UnconfirmedError) void reload();
                throw error;
            }
            // The list shows the version the change lost to while the form keeps what was typed.
            void reload();
            throw new DisplayError(t("staleEdit"));
        }
        return finishSent(proposal?.status ?? null);
    }
    async function finishSent(receipt: OutageScreen["receipt"]) {
        try {
            await reload();
        } finally {
            apply({ type: "sent", receipt });
        }
    }
    const lost = workflow?.attempt;
    const attempt =
        lost &&
        ((change: OutageChange) =>
            lost.payload === attemptPayload(change, direct, configured));
    const ceilings = (outage?: Outage): Ceilings | undefined =>
        configuration &&
        (!outage ||
            !outage.ends_at ||
            outage.declared_configuration_id === configuration.id)
            ? configuration
            : undefined;
    const holdText = (hold: Hold) =>
        hold === "held"
            ? t("editorHeld")
            : hold === "removed"
              ? t("targetRemoved")
              : isEnding(hold)
                ? t("staleClosed", { status: label("IncidentStatus", hold) })
                : reason(hold, t("proposalUnavailable"));
    const hold = derived.hold && holdText(derived.hold);
    const unavailable = direct
        ? !configuration && t("configurationRequired")
        : offer &&
          !offer.can_propose &&
          reason(
              offer.proposal_unavailable_reason ?? "",
              t("proposalUnavailable"),
          );
    const editing =
        workflow?.kind === "create" || workflow?.kind === "update"
            ? workflow
            : null;
    const carded =
        workflow?.kind === "resolve" || workflow?.kind === "cancel"
            ? workflow
            : null;
    const card = (outage: OutageRow) => (
        <OutageCard
            key={outage.id}
            outage={outage}
            tenantId={scope.tenant_id}
            pendingRequestId={outage.pending_request_id}
            timezone={timezone}
            direct={direct}
            offered={derived.offered.has(outage.id)}
            controls={derived}
            hold={hold}
            configurationChanged={
                outage.status === "active" &&
                !!configuration &&
                outage.declared_configuration_id !== configuration.id
            }
            workflow={carded?.outage.id === outage.id ? carded : null}
            onOpen={(kind) =>
                apply({ type: "open", target: { kind, outage }, live })
            }
            onClose={() => apply({ type: "close" })}
            onDiscard={() => apply({ type: "discard" })}
            onSubmit={submit}
            attempt={attempt}
        />
    );
    return (
        <section className="panel" ref={section}>
            <div className="section-heading">
                <div>
                    <h2 tabIndex={-1}>{t("plantOutages")}</h2>
                </div>
                {derived.create && (
                    <button
                        className="button"
                        data-opens="create"
                        data-outage=""
                        disabled={!derived.acts}
                        onClick={() =>
                            apply({
                                type: "open",
                                target: { kind: "create" },
                                live,
                            })
                        }
                    >
                        {direct
                            ? t("reportOutage")
                            : proposals("proposeOutage")}
                    </button>
                )}
            </div>
            <div data-screen-status tabIndex={-1}>
                {screen.receipt && <Notice>{receipt(screen.receipt)}</Notice>}
                {/* The open workflow says the same itself, and the page says it once. */}
                {unavailable && unavailable !== hold && (
                    <Notice>{unavailable}</Notice>
                )}
            </div>
            {!direct && <p className="fine">{proposals("approvalHelp")}</p>}
            <details>
                <summary>{t("howToReportAnOutage")}</summary>
                <p>{t("reportingHelp")}</p>
                <p className="fine">{t("editingHelp")}</p>
            </details>
            {editing && (
                <OutageEditor
                    key={editing.kind === "create" ? "new" : editing.outage.id}
                    outage={
                        editing.kind === "create" ? undefined : editing.outage
                    }
                    stale={editing.stale}
                    timezone={timezone}
                    ceilings={ceilings(
                        editing.kind === "create" ? undefined : editing.outage,
                    )}
                    direct={direct}
                    controls={derived}
                    hold={hold}
                    onSubmit={submit}
                    attempt={attempt}
                    onClose={() => apply({ type: "close" })}
                />
            )}
            {carded?.attempt && !resource.data && (
                <UnconfirmedNotice repeatable={false} />
            )}
            <Resource resource={resource} loading={t("loadingOutages")}>
                {(list) => {
                    // The card of the open resolution or cancellation stays among the listed outages while the
                    // workflow is open: also once its outage was cancelled, and from the workflow once it left the list.
                    const open = carded?.outage.id;
                    const cancelled = list.results.filter(
                        (outage) =>
                            outage.status === "cancelled" && outage.id !== open,
                    );
                    const listed = list.results.filter(
                        (outage) =>
                            outage.status !== "cancelled" || outage.id === open,
                    );
                    const gone =
                        carded &&
                        !list.results.some((outage) => outage.id === open)
                            ? [{ ...carded.outage, pending_request_id: null }]
                            : [];
                    return (
                        <>
                            <div className="toolbar">
                                <CsvDownload
                                    label={t("downloadOutagesCSV")}
                                    pending={reloads > 0}
                                    build={() =>
                                        outagesExport(plant, list.results)
                                    }
                                />
                            </div>
                            {!list.results.length && (
                                <p>{t("noOutagesRecorded")}</p>
                            )}
                            {[...listed, ...gone].map(card)}
                            {cancelled.length > 0 && (
                                <details>
                                    <summary>
                                        {t("cancelledOutages", {
                                            count: cancelled.length,
                                        })}
                                    </summary>
                                    {cancelled.map(card)}
                                </details>
                            )}
                        </>
                    );
                }}
            </Resource>
        </section>
    );
}
