"use client";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { useTranslations } from "next-intl";

import { useState } from "react";
import type { Plant, PlantScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useApiList } from "@/lib/api/query";
import { DisplayError } from "@/lib/api/errors";
import { deviceZone } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import { useProposalKey } from "@/lib/idempotency";
import type { components } from "@/lib/schema";
import { Resource } from "@/ui/resource";
import {
    ActionForm,
    CheckboxField,
    Field,
    Select,
    TextAreaField,
    formText,
} from "@/ui/form";
import { Notice, Tag } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";
import { CsvDownload } from "@/ui/csv-download";

type Outage = components["schemas"]["IncidentOutput"];
const INCIDENT_KINDS = [
    "inverter",
    "panels",
    "grid",
    "other",
    "multiple",
] as const satisfies readonly components["schemas"]["IncidentKindEnum"][];
type OutageInput = components["schemas"]["IncidentInput"];
type Configuration = NonNullable<Plant["current_configuration"]>;
type IncidentRequest = Omit<
    components["schemas"]["IncidentRequestInput"],
    "idempotency_key"
>;
type Ceilings = Pick<
    Configuration,
    "dc_capacity_mw" | "inverter_capacity_mw" | "export_limit_mw"
>;

function localInput(value: string): string {
    const date = new Date(value);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
        .toISOString()
        .slice(0, 16);
}

function timestamp(
    data: FormData,
    key: string,
    t: ReturnType<typeof useTranslations<"Outages">>,
): string | null {
    const value = formText(data, key);
    if (!value) return null;
    const date = new Date(value);
    if (!Number.isFinite(date.getTime()) || date.getTime() > Date.now())
        throw new DisplayError(t("invalidTime"));
    if (localInput(date.toISOString()) !== value)
        throw new DisplayError(t("nonexistentTime"));
    return date.toISOString();
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
    const canManage = useCanManageWorkspace();
    const proposals = useTranslations("ChangeRequests");
    const proposalKey = useProposalKey();
    const [submitted, setSubmitted] = useState(false);
    async function propose(payload: IncidentRequest) {
        await api.post(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/change-requests/incidents/",
            {
                path: scope,
                body: {
                    ...payload,
                    idempotency_key: proposalKey.forPayload(payload),
                },
            },
        );
        proposalKey.reset();
        setSubmitted(true);
        await onChanged();
    }
    const format = useFormat();
    const label = useLabel();

    const [editor, setEditor] = useState<Outage | "new" | null>(null);
    const [resolving, setResolving] = useState<number | null>(null);
    const resource = useApiList(
        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/",
        { path: scope },
    );
    const statusTag = (status: string) =>
        status === "active"
            ? { tone: "warning" as const, label: proposals("active") }
            : {
                  tone: "neutral" as const,
                  label:
                      status === "resolved"
                          ? proposals("resolved")
                          : status === "cancelled"
                            ? proposals("cancelled")
                            : t("unknown"),
              };
    const ceilings = (outage?: Outage): Ceilings | undefined =>
        configuration &&
        (!outage ||
            !outage.ends_at ||
            outage.declared_configuration_id === configuration.id)
            ? configuration
            : undefined;
    const deviceTimezone = deviceZone();
    const time = (value: string) => format.time(value, deviceTimezone);
    const changed = onChanged;
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>{t("plantOutages")}</h2>
                </div>
                {configuration && (
                    <button
                        className="button"
                        onClick={() => {
                            proposalKey.reset();
                            setEditor("new");
                        }}
                    >
                        {canManage
                            ? t("reportOutage")
                            : proposals("proposeOutage")}
                    </button>
                )}
            </div>
            {!configuration && <Notice>{t("configurationRequired")}</Notice>}
            {submitted && <Notice>{proposals("submitted")}</Notice>}
            {!canManage && <p className="fine">{proposals("approvalHelp")}</p>}
            <p className="fine">
                {t("deviceTimezone", { timezone: deviceTimezone })}
            </p>
            <details>
                <summary>{t("howToReportAnOutage")}</summary>
                <p>{t("reportingHelp")}</p>
                <p className="fine">{t("editingHelp")}</p>
            </details>
            {editor && (
                <section
                    aria-label={
                        editor === "new" ? t("reportOutage") : t("editOutage")
                    }
                >
                    <h3>
                        {editor === "new" ? t("reportOutage") : t("editOutage")}
                    </h3>
                    <OutageForm
                        key={
                            editor === "new"
                                ? "new"
                                : `${editor.id}-${editor.revision}`
                        }
                        outage={editor === "new" ? undefined : editor}
                        ceilings={ceilings(
                            editor === "new" ? undefined : editor,
                        )}
                        proposal={!canManage}
                        onSave={async (data, reason) => {
                            const existing =
                                editor === "new" ? undefined : editor;
                            if (!canManage) {
                                await propose({
                                    action: existing ? "update" : "create",
                                    reason,
                                    ...(existing
                                        ? {
                                              incident_id: existing.id,
                                              expected_revision:
                                                  existing.revision,
                                          }
                                        : {}),
                                    values: data,
                                });
                            } else if (existing)
                                await api.put(
                                    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/{incident_id}/",
                                    {
                                        path: {
                                            ...scope,
                                            incident_id: existing.id,
                                        },
                                        body: {
                                            ...data,
                                            expected_revision:
                                                existing.revision,
                                        },
                                    },
                                );
                            else
                                await api.post(
                                    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/",
                                    {
                                        path: scope,
                                        body: data,
                                    },
                                );
                            setEditor(null);
                            void changed();
                        }}
                    />
                    <div className="button-group">
                        <button
                            className="button secondary"
                            onClick={() => {
                                setEditor(null);
                                setResolving(null);
                                void changed();
                            }}
                        >
                            {t("reloadOutages")}
                        </button>
                        <button
                            className="button secondary"
                            onClick={() => setEditor(null)}
                        >
                            {t("closeEditor")}
                        </button>
                    </div>
                </section>
            )}
            <Resource resource={resource} loading={t("loadingOutages")}>
                {(list) => (
                    <>
                        <div className="toolbar">
                            <CsvDownload
                                label={t("downloadOutagesCSV")}
                                filename="plant-outages"
                                records={list.results}
                                metadata={{
                                    timezone: deviceTimezone,
                                    plant_timezone: timezone,
                                }}
                            />
                        </div>
                        {!list.results.length && (
                            <p>{t("noOutagesRecorded")}</p>
                        )}
                        {list.results.map((outage) => (
                            <article key={outage.id} className="provenance">
                                <div className="section-heading">
                                    <h3>{outage.title}</h3>
                                    <Tag tone={statusTag(outage.status).tone}>
                                        <>{statusTag(outage.status).label}</>
                                    </Tag>
                                </div>
                                <p>
                                    {t("started", {
                                        kind: label(
                                            "IncidentKind",
                                            outage.kind,
                                        ),
                                        time: time(outage.starts_at),
                                    })}
                                    {outage.status === "cancelled"
                                        ? t("cancelledEntry")
                                        : outage.ends_at
                                          ? t("resolved", {
                                                time: time(outage.ends_at),
                                            })
                                          : t("ongoing")}
                                </p>
                                <dl className="stats">
                                    <div>
                                        <dt>{t("availablePanels")}</dt>
                                        <dd>
                                            {outage.available_dc_mw == null
                                                ? t("unknown")
                                                : t("power", {
                                                      value: format.number(
                                                          outage.available_dc_mw,
                                                          6,
                                                      ),
                                                  })}
                                        </dd>
                                    </div>
                                    <div>
                                        <dt>{t("availableInverter")}</dt>
                                        <dd>
                                            {outage.available_inverter_mw ==
                                            null
                                                ? t("unknown")
                                                : t("power", {
                                                      value: format.number(
                                                          outage.available_inverter_mw,
                                                          6,
                                                      ),
                                                  })}
                                        </dd>
                                    </div>
                                    <div>
                                        <dt>{t("availableExport")}</dt>
                                        <dd>
                                            {outage.available_export_mw == null
                                                ? t("unknown")
                                                : t("power", {
                                                      value: format.number(
                                                          outage.available_export_mw,
                                                          6,
                                                      ),
                                                  })}
                                        </dd>
                                    </div>
                                </dl>
                                {outage.notes && <p>{outage.notes}</p>}
                                {outage.status === "active" &&
                                    configuration &&
                                    outage.declared_configuration_id !==
                                        configuration.id && (
                                        <Notice>
                                            {t("configurationChanged")}
                                        </Notice>
                                    )}
                                {outage.status !== "cancelled" && (
                                    <div className="button-group">
                                        <button
                                            className="button secondary"
                                            onClick={() => {
                                                proposalKey.reset();
                                                setEditor(outage);
                                            }}
                                        >
                                            {canManage
                                                ? t("editReport")
                                                : proposals("proposeUpdate")}
                                        </button>
                                        {outage.status === "active" && (
                                            <button
                                                className="button secondary"
                                                onClick={() => {
                                                    proposalKey.reset();
                                                    setResolving(outage.id);
                                                }}
                                            >
                                                {canManage
                                                    ? t("resolveOutage")
                                                    : proposals(
                                                          "proposeResolution",
                                                      )}
                                            </button>
                                        )}
                                    </div>
                                )}
                                {resolving === outage.id && (
                                    <ActionForm
                                        permitted
                                        submit={
                                            canManage
                                                ? t("saveResolution")
                                                : proposals("send")
                                        }
                                        onSubmit={async (data) => {
                                            const ends = timestamp(
                                                data,
                                                "ends_at",
                                                t,
                                            );
                                            if (
                                                !ends ||
                                                new Date(ends) <=
                                                    new Date(outage.starts_at)
                                            )
                                                throw new DisplayError(
                                                    t("resolutionAfterStart"),
                                                );
                                            if (!canManage)
                                                await propose({
                                                    action: "resolve",
                                                    incident_id: outage.id,
                                                    expected_revision:
                                                        outage.revision,
                                                    ends_at: ends,
                                                    reason: formText(
                                                        data,
                                                        "reason",
                                                    ),
                                                });
                                            else
                                                await api.post(
                                                    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/{incident_id}/resolve/",
                                                    {
                                                        path: {
                                                            ...scope,
                                                            incident_id:
                                                                outage.id,
                                                        },
                                                        body: {
                                                            expected_revision:
                                                                outage.revision,
                                                            ends_at: ends,
                                                        },
                                                    },
                                                );
                                            setResolving(null);
                                            void changed();
                                        }}
                                    >
                                        {!canManage && <ProposalReason />}
                                        <Field
                                            label={t("resolvedAtDeviceTime")}
                                            name="ends_at"
                                            type="datetime-local"
                                            defaultValue={localInput(
                                                new Date().toISOString(),
                                            )}
                                            required
                                        />
                                    </ActionForm>
                                )}
                                {outage.status !== "cancelled" && (
                                    <details>
                                        <summary>
                                            {canManage
                                                ? t("cancelAnIncorrectEntry")
                                                : proposals(
                                                      "proposeCancellation",
                                                  )}
                                        </summary>
                                        <ActionForm
                                            permitted
                                            submit={
                                                canManage
                                                    ? t("cancelOutage")
                                                    : proposals("send")
                                            }
                                            onSubmit={async (data) => {
                                                if (!canManage)
                                                    await propose({
                                                        action: "cancel",
                                                        incident_id: outage.id,
                                                        expected_revision:
                                                            outage.revision,
                                                        reason: formText(
                                                            data,
                                                            "reason",
                                                        ),
                                                    });
                                                else
                                                    await api.post(
                                                        "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/outages/{incident_id}/cancel/",
                                                        {
                                                            path: {
                                                                ...scope,
                                                                incident_id:
                                                                    outage.id,
                                                            },
                                                            body: {
                                                                expected_revision:
                                                                    outage.revision,
                                                            },
                                                        },
                                                    );
                                                void changed();
                                            }}
                                        >
                                            {!canManage && <ProposalReason />}
                                            <CheckboxField
                                                label={t(
                                                    "confirmationIncorrect",
                                                )}
                                                required
                                            />
                                        </ActionForm>
                                    </details>
                                )}
                            </article>
                        ))}
                    </>
                )}
            </Resource>
        </section>
    );
}

function OutageForm({
    outage,
    ceilings,
    onSave,
    proposal = false,
}: {
    outage?: Outage;
    ceilings?: Ceilings;
    proposal?: boolean;
    onSave: (data: OutageInput, reason: string) => Promise<void>;
}) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");
    const format = useFormat();
    const label = useLabel();
    const ceiling = (value?: number | null) =>
        value == null
            ? undefined
            : t("capacityCeiling", { value: format.number(value, 6) });

    const capacities = [
        outage?.available_dc_mw,
        outage?.available_inverter_mw,
        outage?.available_export_mw,
    ];
    const [impact, setImpact] = useState(
        outage
            ? capacities.every((value) => value === null)
                ? "unknown"
                : capacities.every((value) => value === 0)
                  ? "total"
                  : "reduced"
            : "total",
    );
    return (
        <ActionForm
            permitted
            submit={
                proposal
                    ? proposals("send")
                    : outage
                      ? t("saveOutageChanges")
                      : t("saveOutage")
            }
            onSubmit={async (data) => {
                const starts = timestamp(data, "starts_at", t);
                const ends = timestamp(data, "ends_at", t);
                if (!starts) throw new DisplayError(t("enterTheOutageStart"));
                if (ends && new Date(ends) <= new Date(starts))
                    throw new DisplayError(t("resolutionAfterStart"));
                const capacity = (key: string) =>
                    impact === "total"
                        ? 0
                        : impact === "unknown" ||
                            formText(data, key).trim() === ""
                          ? null
                          : Number(data.get(key));
                const kind = INCIDENT_KINDS.find(
                    (value) => value === data.get("kind"),
                );
                if (!kind) throw new DisplayError(t("chooseAValidOutageType"));
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
                await onSave(
                    {
                        kind,
                        title: formText(data, "title"),
                        notes: formText(data, "notes"),
                        starts_at: starts,
                        ends_at: ends,
                        ...capacities,
                    },
                    formText(data, "reason"),
                );
            }}
        >
            {proposal && <ProposalReason />}
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
                    label={t("startedAtDeviceTime")}
                    name="starts_at"
                    type="datetime-local"
                    defaultValue={localInput(
                        outage?.starts_at ?? new Date().toISOString(),
                    )}
                    required
                />
                <Field
                    label={t("resolvedAtDeviceTimeOptional")}
                    name="ends_at"
                    type="datetime-local"
                    defaultValue={
                        outage?.ends_at ? localInput(outage.ends_at) : ""
                    }
                />
            </div>
            <Select
                label={t("capacityImpact")}
                value={impact}
                onChange={(event) => setImpact(event.target.value)}
            >
                <option value="total">{t("totalOutage")}</option>
                <option value="reduced">{t("reducedCapacity")}</option>
                <option value="unknown">{t("unknownImpact")}</option>
            </Select>
            {impact === "reduced" && (
                <>
                    <p className="fine">{t("remainingCapacityHelp")}</p>
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
                            max={ceilings?.inverter_capacity_mw ?? undefined}
                            hint={ceiling(ceilings?.inverter_capacity_mw)}
                            step="any"
                            defaultValue={outage?.available_inverter_mw ?? ""}
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
    );
}

function ProposalReason() {
    const t = useTranslations("ChangeRequests");
    return (
        <TextAreaField
            label={t("optionalReason")}
            name="reason"
            maxLength={500}
        />
    );
}
