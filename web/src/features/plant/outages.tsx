"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import type { Plant, PlantScope } from "@/lib/api";
import { DisplayError } from "@/lib/api/errors";
import { useApiList } from "@/lib/api/query";
import { outagesExport } from "@/lib/exports";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import { Resource } from "@/ui/resource";
import { Notice } from "@/ui/ui";
import { useCodeLabel } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import {
    INITIAL_OUTAGES,
    outageControls,
    proposalBody,
    reduceOutages,
    staleVersion,
    submitOutageChange,
    type Live,
    type Offer,
    type Outage,
    type OutageChange,
    type OutageEvent,
    type OutageRow,
} from "./outage/model";
import { OutageCard } from "./outage/outage-card";
import { OutageEditor, type Ceilings } from "./outage/outage-editor";

type Configuration = NonNullable<Plant["current_configuration"]>;

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
    const proposals = useTranslations("ChangeRequests");
    const direct = useCanManageWorkspace();
    const plant = useExportPlant();
    const [screen, setScreen] = useState(INITIAL_OUTAGES);
    const [reconciled, setReconciled] = useState<Offer>();
    const [reloads, setReloads] = useState(0);
    const resource = useApiList(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/",
        { path: scope },
    );
    const offer = resource.data;
    const live: Live = { offer, direct, configured: !!configuration };
    const apply = (event: OutageEvent) =>
        setScreen((current) => reduceOutages(current, event));
    if (offer && offer !== reconciled) {
        setReconciled(offer);
        apply({ type: "rowsLoaded", live });
    }
    const derived = outageControls(screen, live);
    const { workflow } = screen;

    async function reload() {
        setReloads((count) => count + 1);
        try {
            await onChanged();
        } finally {
            setReloads((count) => count - 1);
        }
    }
    async function submit(change: OutageChange) {
        const next = reduceOutages(screen, {
            type: "send",
            payload: JSON.stringify(direct ? null : proposalBody(change)),
            key: crypto.randomUUID(),
        });
        const attempt = next.workflow?.attempt;
        if (next === screen || !attempt) return;
        setScreen(next);
        try {
            await submitOutageChange(scope, change, {
                key: attempt.key,
                direct,
            });
        } catch (error) {
            const current =
                change.action === "create"
                    ? null
                    : staleVersion(error, change.outage.id);
            apply({ type: "sendFailed", current, direct });
            if (!current) throw error;
            // The list shows the version the change lost to while the form keeps what was typed.
            void reload();
            throw new DisplayError(t("staleEdit"));
        }
        try {
            await reload();
        } finally {
            apply({ type: "sent", proposed: !direct });
        }
    }
    const ceilings = (outage?: Outage): Ceilings | undefined =>
        configuration &&
        (!outage ||
            !outage.ends_at ||
            outage.declared_configuration_id === configuration.id)
            ? configuration
            : undefined;
    const hold =
        derived.hold === "held"
            ? t("editorHeld")
            : derived.hold && reason(derived.hold, t("proposalUnavailable"));
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
            workflow={
                (workflow?.kind === "resolve" || workflow?.kind === "cancel") &&
                workflow.outage.id === outage.id
                    ? workflow
                    : null
            }
            onOpen={(kind) =>
                apply({ type: "open", target: { kind, outage }, live })
            }
            onClose={() => apply({ type: "close" })}
            onDiscard={() => apply({ type: "discard" })}
            onSubmit={submit}
        />
    );
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>{t("plantOutages")}</h2>
                </div>
                {derived.create && (
                    <button
                        className="button"
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
            {unavailable && <Notice>{unavailable}</Notice>}
            {screen.submitted && <Notice>{proposals("submitted")}</Notice>}
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
                    onClose={() => apply({ type: "close" })}
                />
            )}
            <Resource resource={resource} loading={t("loadingOutages")}>
                {(list) => {
                    const cancelled = list.results.filter(
                        (outage) => outage.status === "cancelled",
                    );
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
                            {list.results
                                .filter(
                                    (outage) => outage.status !== "cancelled",
                                )
                                .map(card)}
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
