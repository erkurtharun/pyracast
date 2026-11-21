"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import type { Plant, PlantScope } from "@/lib/api";
import { useApiList } from "@/lib/api/query";
import { outagesExport } from "@/lib/exports";
import { useProposalKey } from "@/lib/idempotency";
import { CsvDownload } from "@/ui/csv-download";
import { useExportPlant } from "@/ui/export-plant";
import { Resource } from "@/ui/resource";
import { Notice } from "@/ui/ui";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import {
    staleVersion,
    submitOutageChange,
    type Outage,
    type OutageChange,
} from "./outage/model";
import { OutageCard } from "./outage/outage-card";
import { OutageEditor, type Ceilings } from "./outage/outage-editor";

type Configuration = NonNullable<Plant["current_configuration"]>;
type Editor = { status: "closed" } | { status: "open"; outage?: Outage };

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
    const proposals = useTranslations("ChangeRequests");
    const direct = useCanManageWorkspace();
    const plant = useExportPlant();
    const proposalKey = useProposalKey();
    const [submitted, setSubmitted] = useState(false);
    const [editor, setEditor] = useState<Editor>({ status: "closed" });
    const [resolving, setResolving] = useState<number | null>(null);
    const [reloads, setReloads] = useState(0);
    const resource = useApiList(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/",
        { path: scope },
    );
    async function reload() {
        setReloads((count) => count + 1);
        try {
            await onChanged();
        } finally {
            setReloads((count) => count - 1);
        }
    }
    async function submit(change: OutageChange) {
        try {
            await submitOutageChange(scope, change, { proposalKey, direct });
        } catch (error) {
            // The list shows the version the edit lost to while the form keeps what was typed.
            if (
                change.action !== "create" &&
                staleVersion(error, change.outage.id)
            )
                void reload();
            throw error;
        }
        if (!direct) setSubmitted(true);
        await reload();
    }
    const ceilings = (outage?: Outage): Ceilings | undefined =>
        configuration &&
        (!outage ||
            !outage.ends_at ||
            outage.declared_configuration_id === configuration.id)
            ? configuration
            : undefined;
    const open = (outage?: Outage) => {
        proposalKey.reset();
        setEditor({ status: "open", outage });
    };
    const card = (outage: Outage) => (
        <OutageCard
            key={outage.id}
            outage={outage}
            timezone={timezone}
            direct={direct}
            configurationChanged={
                outage.status === "active" &&
                !!configuration &&
                outage.declared_configuration_id !== configuration.id
            }
            resolving={resolving === outage.id}
            onEdit={() => open(outage)}
            onResolve={(next) => {
                proposalKey.reset();
                setResolving(next ? outage.id : null);
            }}
            onSubmit={submit}
        />
    );
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>{t("plantOutages")}</h2>
                </div>
                {configuration && (
                    <button className="button" onClick={() => open()}>
                        {direct
                            ? t("reportOutage")
                            : proposals("proposeOutage")}
                    </button>
                )}
            </div>
            {!configuration && <Notice>{t("configurationRequired")}</Notice>}
            {submitted && <Notice>{proposals("submitted")}</Notice>}
            {!direct && <p className="fine">{proposals("approvalHelp")}</p>}
            <details>
                <summary>{t("howToReportAnOutage")}</summary>
                <p>{t("reportingHelp")}</p>
                <p className="fine">{t("editingHelp")}</p>
            </details>
            {editor.status === "open" && (
                <OutageEditor
                    key={editor.outage?.id ?? "new"}
                    outage={editor.outage}
                    timezone={timezone}
                    ceilings={ceilings(editor.outage)}
                    direct={direct}
                    onSubmit={submit}
                    onClose={() => setEditor({ status: "closed" })}
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
