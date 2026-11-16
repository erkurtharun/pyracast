"use client";
import { useCanManageWorkspace } from "./workspace-access";
import { useTranslations, useLocale, useFormatter } from "next-intl";

import { useState } from "react";
import {
    useChangeRequestUpdates,
    useProposalKey,
} from "./change-request-updates";
import { request } from "@/lib/api";
import type { components } from "@/lib/schema";
import { ActionForm, Field, Notice, Select, Tag, useCollection } from "./ui";
import { CsvDownload } from "./csv-download";

type Outage = components["schemas"]["IncidentOutput"];
type OutageInput = components["schemas"]["IncidentInput"];

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
    const value = String(data.get(key) ?? "");
    if (!value) return null;
    const date = new Date(value);
    if (!Number.isFinite(date.getTime()) || date.getTime() > Date.now())
        throw new Error(t("invalidTime"));
    if (localInput(date.toISOString()) !== value)
        throw new Error(t("nonexistentTime"));
    return date.toISOString();
}

export function Outages({
    base,
    token,
    timezone,
    onChanged,
    currentConfigurationId,
}: {
    base: string;
    token: string;
    timezone: string;
    onChanged: () => void;
    currentConfigurationId?: number;
}) {
    const t = useTranslations("Outages");
    const canManage = useCanManageWorkspace();
    const proposals = useTranslations("ChangeRequests");
    const proposalKey = useProposalKey();
    const requestsChanged = useChangeRequestUpdates();
    const [submitted, setSubmitted] = useState(false);
    async function propose(payload: unknown) {
        await request(
            `${base}change-requests/incidents/`,
            "POST",
            {
                ...(payload as object),
                idempotency_key: proposalKey.forPayload(payload),
            },
            token,
        );
        proposalKey.reset();
        setSubmitted(true);
        requestsChanged();
    }
    const locale = useLocale();
    const formatNumber = useFormatter();
    const kinds: Record<string, string> = {
        inverter: t("inverter"),
        panels: t("panels"),
        grid: t("grid"),
        other: t("other"),
        multiple: t("multipleFaults"),
    };

    const [revision, setRevision] = useState(0);
    const [editor, setEditor] = useState<Outage | "new" | null>(null);
    const [resolving, setResolving] = useState<number | null>(null);
    const resource = useCollection<Outage>(`${base}outages/`, revision);
    const deviceTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const format = (value: string) =>
        new Date(value).toLocaleString(locale, { timeZone: deviceTimezone });
    const changed = () => {
        setRevision((value) => value + 1);
        onChanged();
    };
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>{t("plantOutages")}</h2>
                </div>
                {(canManage || currentConfigurationId) && (
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
            {submitted && <Notice>{proposals("submitted")}</Notice>}
            {!canManage && <p className="fine">{proposals("approvalHelp")}</p>}
            <p className="fine">
                {t("deviceTimezone", { timezone: deviceTimezone })}
                {deviceTimezone !== timezone
                    ? t("plantTimezone", { timezone })
                    : ""}
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
                            } else
                                await request(
                                    `${base}outages/${existing ? `${existing.id}/` : ""}`,
                                    existing ? "PUT" : "POST",
                                    {
                                        ...data,
                                        ...(existing
                                            ? {
                                                  expected_revision:
                                                      existing.revision,
                                              }
                                            : {}),
                                    } satisfies
                                        | OutageInput
                                        | components["schemas"]["IncidentUpdate"],
                                    token,
                                );
                            setEditor(null);
                            changed();
                        }}
                    />
                    <div className="button-group">
                        <button
                            className="button secondary"
                            onClick={() => {
                                setEditor(null);
                                setResolving(null);
                                changed();
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
            {resource.loading ? (
                <p role="status">{t("loadingOutages")}</p>
            ) : resource.error ? (
                <Notice error>{resource.error}</Notice>
            ) : (
                <>
                    <div className="toolbar">
                        <CsvDownload
                            label={t("downloadOutagesCSV")}
                            filename="plant-outages"
                            records={resource.data?.results ?? []}
                            metadata={{
                                timezone: deviceTimezone,
                                plant_timezone: timezone,
                            }}
                        />
                    </div>
                    {!resource.data?.results.length && (
                        <p>{t("noOutagesRecorded")}</p>
                    )}
                    {resource.data?.results.map((outage) => (
                        <article key={outage.id} className="provenance">
                            <div className="section-heading">
                                <h3>{outage.title}</h3>
                                <Tag>{outage.status}</Tag>
                            </div>
                            <p>
                                {t("started", {
                                    kind: kinds[outage.kind] ?? t("other"),
                                    time: format(outage.starts_at),
                                })}
                                {outage.status === "cancelled"
                                    ? t("cancelledEntry")
                                    : outage.ends_at
                                      ? t("resolved", {
                                            time: format(outage.ends_at),
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
                                                  value: formatNumber.number(
                                                      outage.available_dc_mw,
                                                      {
                                                          maximumFractionDigits: 6,
                                                      },
                                                  ),
                                              })}
                                    </dd>
                                </div>
                                <div>
                                    <dt>{t("availableInverter")}</dt>
                                    <dd>
                                        {outage.available_inverter_mw == null
                                            ? t("unknown")
                                            : t("power", {
                                                  value: formatNumber.number(
                                                      outage.available_inverter_mw,
                                                      {
                                                          maximumFractionDigits: 6,
                                                      },
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
                                                  value: formatNumber.number(
                                                      outage.available_export_mw,
                                                      {
                                                          maximumFractionDigits: 6,
                                                      },
                                                  ),
                                              })}
                                    </dd>
                                </div>
                            </dl>
                            {outage.notes && <p>{outage.notes}</p>}
                            {outage.status === "active" &&
                                currentConfigurationId &&
                                outage.declared_configuration_id !==
                                    currentConfigurationId && (
                                    <Notice>{t("configurationChanged")}</Notice>
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
                                            throw new Error(
                                                t("resolutionAfterStart"),
                                            );
                                        if (!canManage)
                                            await propose({
                                                action: "resolve",
                                                incident_id: outage.id,
                                                expected_revision:
                                                    outage.revision,
                                                ends_at: ends,
                                                reason: String(
                                                    data.get("reason") ?? "",
                                                ),
                                            });
                                        else
                                            await request(
                                                `${base}outages/${outage.id}/resolve/`,
                                                "POST",
                                                {
                                                    expected_revision:
                                                        outage.revision,
                                                    ends_at: ends,
                                                } satisfies components["schemas"]["ResolveInput"],
                                                token,
                                            );
                                        setResolving(null);
                                        changed();
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
                                    />
                                </ActionForm>
                            )}
                            {outage.status !== "cancelled" && (
                                <details>
                                    <summary>
                                        {canManage
                                            ? t("cancelAnIncorrectEntry")
                                            : proposals("proposeCancellation")}
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
                                                    reason: String(
                                                        data.get("reason") ??
                                                            "",
                                                    ),
                                                });
                                            else
                                                await request(
                                                    `${base}outages/${outage.id}/cancel/`,
                                                    "POST",
                                                    {
                                                        expected_revision:
                                                            outage.revision,
                                                    } satisfies components["schemas"]["RevisionInput"],
                                                    token,
                                                );
                                            changed();
                                        }}
                                    >
                                        {!canManage && <ProposalReason />}
                                        <label className="field">
                                            <span>
                                                <input
                                                    type="checkbox"
                                                    required
                                                />{" "}
                                                {t("confirmationIncorrect")}
                                            </span>
                                        </label>
                                    </ActionForm>
                                </details>
                            )}
                        </article>
                    ))}
                </>
            )}
        </section>
    );
}

function OutageForm({
    outage,
    onSave,
    proposal = false,
}: {
    outage?: Outage;
    proposal?: boolean;
    onSave: (data: OutageInput, reason: string) => Promise<void>;
}) {
    const t = useTranslations("Outages");
    const proposals = useTranslations("ChangeRequests");

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
                if (!starts) throw new Error(t("enterTheOutageStart"));
                if (ends && new Date(ends) <= new Date(starts))
                    throw new Error(t("resolutionAfterStart"));
                const capacity = (key: string) =>
                    impact === "total"
                        ? 0
                        : impact === "unknown" ||
                            String(data.get(key) ?? "").trim() === ""
                          ? null
                          : Number(data.get(key));
                const kind = (
                    ["inverter", "panels", "grid", "other", "multiple"] as const
                ).find((value) => value === data.get("kind"));
                if (!kind) throw new Error(t("chooseAValidOutageType"));
                await onSave(
                    {
                        kind,
                        title: String(data.get("title") ?? ""),
                        notes: String(data.get("notes") ?? ""),
                        starts_at: starts,
                        ends_at: ends,
                        available_dc_mw: capacity("available_dc_mw"),
                        available_inverter_mw: capacity(
                            "available_inverter_mw",
                        ),
                        available_export_mw: capacity("available_export_mw"),
                    },
                    String(data.get("reason") ?? ""),
                );
            }}
        >
            {proposal && <ProposalReason />}
            <div className="form-grid">
                <Select
                    label={t("outageType")}
                    name="kind"
                    defaultValue={outage?.kind ?? "inverter"}
                >
                    <option value="inverter">{t("inverter")}</option>
                    <option value="panels">{t("panels")}</option>
                    <option value="grid">{t("grid")}</option>
                    <option value="other">{t("other")}</option>
                    <option value="multiple">{t("multipleFaults")}</option>
                </Select>
                <Field
                    label={t("incidentTitle")}
                    name="title"
                    defaultValue={outage?.title}
                />
                <Field
                    label={t("startedAtDeviceTime")}
                    name="starts_at"
                    type="datetime-local"
                    defaultValue={localInput(
                        outage?.starts_at ?? new Date().toISOString(),
                    )}
                />
                <Field
                    label={t("resolvedAtDeviceTimeOptional")}
                    name="ends_at"
                    type="datetime-local"
                    required={false}
                    defaultValue={
                        outage?.ends_at ? localInput(outage.ends_at) : ""
                    }
                />
            </div>
            <label className="field">
                {t("capacityImpact")}
                <select
                    value={impact}
                    onChange={(event) => setImpact(event.target.value)}
                >
                    <option value="total">{t("totalOutage")}</option>
                    <option value="reduced">{t("reducedCapacity")}</option>
                    <option value="unknown">{t("unknownImpact")}</option>
                </select>
            </label>
            {impact === "reduced" && (
                <>
                    <p className="fine">{t("remainingCapacityHelp")}</p>
                    <div className="form-grid">
                        <Field
                            label={t("availablePanelDCCapacityMW")}
                            name="available_dc_mw"
                            type="number"
                            min={0}
                            step="any"
                            required={false}
                            defaultValue={outage?.available_dc_mw ?? ""}
                        />
                        <Field
                            label={t("availableInverterACCapacityMW")}
                            name="available_inverter_mw"
                            type="number"
                            min={0}
                            step="any"
                            required={false}
                            defaultValue={outage?.available_inverter_mw ?? ""}
                        />
                        <Field
                            label={t("availableGridExportCapacityMW")}
                            name="available_export_mw"
                            type="number"
                            min={0}
                            step="any"
                            required={false}
                            defaultValue={outage?.available_export_mw ?? ""}
                        />
                    </div>
                    <details>
                        <summary>{t("capacityEntryHelp")}</summary>
                        <p className="fine">{t("capacityHelp")}</p>
                    </details>
                </>
            )}
            <label className="field">
                {t("notesOptional")}
                <textarea name="notes" defaultValue={outage?.notes} />
            </label>
        </ActionForm>
    );
}

function ProposalReason() {
    const t = useTranslations("ChangeRequests");
    return (
        <label className="field">
            {t("optionalReason")}
            <textarea name="reason" maxLength={500} />
        </label>
    );
}
