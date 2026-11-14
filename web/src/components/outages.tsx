"use client";

import { useState } from "react";
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

function timestamp(data: FormData, key: string): string | null {
    const value = String(data.get(key) ?? "");
    if (!value) return null;
    const date = new Date(value);
    if (!Number.isFinite(date.getTime()) || date.getTime() > Date.now())
        throw new Error("Choose a valid time that is not in the future.");
    if (localInput(date.toISOString()) !== value)
        throw new Error(
            "This local time does not exist in your device timezone. Choose another time.",
        );
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
    const [revision, setRevision] = useState(0);
    const [editor, setEditor] = useState<Outage | "new" | null>(null);
    const [resolving, setResolving] = useState<number | null>(null);
    const resource = useCollection<Outage>(`${base}outages/`, revision);
    const deviceTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const format = (value: string) =>
        new Date(value).toLocaleString("en", { timeZone: deviceTimezone });
    const changed = () => {
        setRevision((value) => value + 1);
        onChanged();
    };
    return (
        <section className="panel">
            <div className="section-heading">
                <div>
                    <h2>Plant outages</h2>
                </div>
                <button className="button" onClick={() => setEditor("new")}>
                    Report outage
                </button>
            </div>
            <p className="fine">
                Entry and display times use your device timezone:{" "}
                {deviceTimezone}.
                {deviceTimezone !== timezone
                    ? ` Plant timezone: ${timezone}.`
                    : ""}
            </p>
            <details>
                <summary>How to report an outage</summary>
                <p>
                    Keep simultaneous faults in one incident and enter the total
                    remaining plant capacity. Unknown impact stays unknown; it
                    is never treated as zero.
                </p>
                <p className="fine">
                    Edit corrects an existing report. If available capacity
                    changes during an outage, close this period and report the
                    new operating state.
                </p>
            </details>
            {editor && (
                <section
                    aria-label={
                        editor === "new" ? "Report outage" : "Edit outage"
                    }
                >
                    <h3>
                        {editor === "new" ? "Report outage" : "Edit outage"}
                    </h3>
                    <OutageForm
                        key={
                            editor === "new"
                                ? "new"
                                : `${editor.id}-${editor.revision}`
                        }
                        outage={editor === "new" ? undefined : editor}
                        onSave={async (data) => {
                            const existing =
                                editor === "new" ? undefined : editor;
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
                            Reload outages
                        </button>
                        <button
                            className="button secondary"
                            onClick={() => setEditor(null)}
                        >
                            Close editor
                        </button>
                    </div>
                </section>
            )}
            {resource.loading ? (
                <p role="status">Loading outages…</p>
            ) : resource.error ? (
                <Notice error>{resource.error}</Notice>
            ) : (
                <>
                    <div className="toolbar">
                        <CsvDownload
                            label="Download outages CSV"
                            filename="plant-outages"
                            records={resource.data?.results ?? []}
                            metadata={{
                                timezone: deviceTimezone,
                                plant_timezone: timezone,
                            }}
                        />
                    </div>
                    {!resource.data?.results.length && (
                        <p>No outages recorded.</p>
                    )}
                    {resource.data?.results.map((outage) => (
                        <article key={outage.id} className="provenance">
                            <div className="section-heading">
                                <h3>{outage.title}</h3>
                                <Tag>{outage.status}</Tag>
                            </div>
                            <p>
                                {outage.kind.replaceAll("_", " ")} · Started{" "}
                                {format(outage.starts_at)}
                                {outage.status === "cancelled"
                                    ? " · Cancelled entry"
                                    : outage.ends_at
                                      ? ` · Resolved ${format(outage.ends_at)}`
                                      : " · Ongoing"}
                            </p>
                            <dl className="stats">
                                <div>
                                    <dt>Available panels</dt>
                                    <dd>
                                        {outage.available_dc_mw == null
                                            ? "Unknown"
                                            : `${outage.available_dc_mw} MW`}
                                    </dd>
                                </div>
                                <div>
                                    <dt>Available inverter</dt>
                                    <dd>
                                        {outage.available_inverter_mw == null
                                            ? "Unknown"
                                            : `${outage.available_inverter_mw} MW`}
                                    </dd>
                                </div>
                                <div>
                                    <dt>Available export</dt>
                                    <dd>
                                        {outage.available_export_mw == null
                                            ? "Unknown"
                                            : `${outage.available_export_mw} MW`}
                                    </dd>
                                </div>
                            </dl>
                            {outage.notes && <p>{outage.notes}</p>}
                            {outage.status === "active" &&
                                currentConfigurationId &&
                                outage.declared_configuration_id !==
                                    currentConfigurationId && (
                                    <Notice>
                                        Configuration changed. Edit this report
                                        to confirm remaining capacities. Outage
                                        impact is unknown until confirmed.
                                    </Notice>
                                )}
                            {outage.status !== "cancelled" && (
                                <div className="button-group">
                                    <button
                                        className="button secondary"
                                        onClick={() => setEditor(outage)}
                                    >
                                        Edit report
                                    </button>
                                    {outage.status === "active" && (
                                        <button
                                            className="button secondary"
                                            onClick={() =>
                                                setResolving(outage.id)
                                            }
                                        >
                                            Resolve outage
                                        </button>
                                    )}
                                </div>
                            )}
                            {resolving === outage.id && (
                                <ActionForm
                                    submit="Save resolution"
                                    onSubmit={async (data) => {
                                        const ends = timestamp(data, "ends_at");
                                        if (
                                            !ends ||
                                            new Date(ends) <=
                                                new Date(outage.starts_at)
                                        )
                                            throw new Error(
                                                "Resolution must be after the outage start.",
                                            );
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
                                    <Field
                                        label="Resolved at (device time)"
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
                                    <summary>Cancel an incorrect entry</summary>
                                    <ActionForm
                                        submit="Cancel outage"
                                        onSubmit={async () => {
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
                                        <label className="field">
                                            <span>
                                                <input
                                                    type="checkbox"
                                                    required
                                                />{" "}
                                                I confirm this incident was
                                                recorded incorrectly.
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
}: {
    outage?: Outage;
    onSave: (data: OutageInput) => Promise<void>;
}) {
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
            submit={outage ? "Save outage changes" : "Save outage"}
            onSubmit={async (data) => {
                const starts = timestamp(data, "starts_at");
                const ends = timestamp(data, "ends_at");
                if (!starts) throw new Error("Enter the outage start.");
                if (ends && new Date(ends) <= new Date(starts))
                    throw new Error(
                        "Resolution must be after the outage start.",
                    );
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
                if (!kind) throw new Error("Choose a valid outage type.");
                await onSave({
                    kind,
                    title: String(data.get("title") ?? ""),
                    notes: String(data.get("notes") ?? ""),
                    starts_at: starts,
                    ends_at: ends,
                    available_dc_mw: capacity("available_dc_mw"),
                    available_inverter_mw: capacity("available_inverter_mw"),
                    available_export_mw: capacity("available_export_mw"),
                });
            }}
        >
            <div className="form-grid">
                <Select
                    label="Outage type"
                    name="kind"
                    defaultValue={outage?.kind ?? "inverter"}
                >
                    <option value="inverter">Inverter</option>
                    <option value="panels">Panels</option>
                    <option value="grid">Grid</option>
                    <option value="other">Other</option>
                    <option value="multiple">Multiple faults</option>
                </Select>
                <Field
                    label="Incident title"
                    name="title"
                    defaultValue={outage?.title}
                />
                <Field
                    label="Started at (device time)"
                    name="starts_at"
                    type="datetime-local"
                    defaultValue={localInput(
                        outage?.starts_at ?? new Date().toISOString(),
                    )}
                />
                <Field
                    label="Resolved at (device time, optional)"
                    name="ends_at"
                    type="datetime-local"
                    required={false}
                    defaultValue={
                        outage?.ends_at ? localInput(outage.ends_at) : ""
                    }
                />
            </div>
            <label className="field">
                Capacity impact
                <select
                    value={impact}
                    onChange={(event) => setImpact(event.target.value)}
                >
                    <option value="total">Total outage</option>
                    <option value="reduced">Reduced capacity</option>
                    <option value="unknown">Unknown impact</option>
                </select>
            </label>
            {impact === "reduced" && (
                <>
                    <p className="fine">
                        Enter remaining MW. Blank means unknown.
                    </p>
                    <div className="form-grid">
                        <Field
                            label="Available panel DC capacity (MW)"
                            name="available_dc_mw"
                            type="number"
                            min={0}
                            step="any"
                            required={false}
                            defaultValue={outage?.available_dc_mw ?? ""}
                        />
                        <Field
                            label="Available inverter AC capacity (MW)"
                            name="available_inverter_mw"
                            type="number"
                            min={0}
                            step="any"
                            required={false}
                            defaultValue={outage?.available_inverter_mw ?? ""}
                        />
                        <Field
                            label="Available grid export capacity (MW)"
                            name="available_export_mw"
                            type="number"
                            min={0}
                            step="any"
                            required={false}
                            defaultValue={outage?.available_export_mw ?? ""}
                        />
                    </div>
                    <details>
                        <summary>Capacity entry help</summary>
                        <p className="fine">
                            Enter available capacity, not lost capacity.
                            Disconnected panels reduce DC capacity. Do not infer
                            a loss percentage from an inverter fault.
                        </p>
                    </details>
                </>
            )}
            <label className="field">
                Notes (optional)
                <textarea name="notes" defaultValue={outage?.notes} />
            </label>
        </ActionForm>
    );
}
