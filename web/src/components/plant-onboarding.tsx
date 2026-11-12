"use client";

import { useRef, useState } from "react";
import { request, type Region, type Plant } from "@/lib/api";
import type { components } from "@/lib/schema";
import { Field, Notice } from "./ui";
import {
    PlantConfigurationFields,
    configurationInput,
} from "./plant-configuration-fields";

export type PlantSite = components["schemas"]["SiteOutput"];

export function PlantOnboarding({
    tenant,
    token,
    existing,
    onCreated,
    onCancel,
}: {
    tenant: number;
    token: string;
    existing?: Region;
    onCreated: (site: PlantSite) => void;
    onCancel: () => void;
}) {
    const [step, setStep] = useState(existing ? 1 : 0);
    const [review, setReview] = useState<Record<string, FormDataEntryValue>>(
        {},
    );
    const [status, setStatus] = useState<
        | { kind: "idle" }
        | { kind: "saving" }
        | { kind: "error"; message: string }
    >({ kind: "idle" });
    const title = useRef<HTMLHeadingElement>(null);
    const move = (next: number) => {
        setStep(next);
        requestAnimationFrame(() => title.current?.focus());
    };
    return (
        <section className="panel onboarding">
            <div className="section-heading">
                <div>
                    <h2 ref={title} tabIndex={-1}>
                        {existing
                            ? "Complete plant setup"
                            : "Add a solar plant"}
                    </h2>
                    <p>
                        Enter your site and equipment details. An administrator
                        connects the data source and approves collection.
                    </p>
                </div>
                <button className="button secondary" onClick={onCancel}>
                    Cancel
                </button>
            </div>
            <ol className="onboarding-steps" aria-label="Plant setup progress">
                {["Location", "Technical details", "Review"].map(
                    (label, index) => (
                        <li
                            key={label}
                            aria-current={step === index ? "step" : undefined}
                        >
                            {label}
                        </li>
                    ),
                )}
            </ol>
            <form
                noValidate
                onSubmit={async (event) => {
                    event.preventDefault();
                    const form = event.currentTarget;
                    const section = form.querySelector(`[data-step="${step}"]`);
                    const invalid = Array.from(
                        section?.querySelectorAll<
                            HTMLInputElement | HTMLSelectElement
                        >("input, select") ?? [],
                    ).find((field) => !field.checkValidity());
                    if (invalid) {
                        invalid.reportValidity();
                        return;
                    }
                    const data = new FormData(form);
                    if (step < 2) {
                        setReview(Object.fromEntries(data));
                        move(step + 1);
                        return;
                    }
                    setStatus({ kind: "saving" });
                    try {
                        const body = configurationInput(data);
                        const site = existing
                            ? {
                                  ...existing,
                                  plant: await request<Plant>(
                                      `/api/tenants/${tenant}/regions/${existing.id}/plants/`,
                                      "POST",
                                      body,
                                      token,
                                  ),
                              }
                            : await request<PlantSite>(
                                  `/api/tenants/${tenant}/sites/`,
                                  "POST",
                                  {
                                      ...body,
                                      latitude: Number(data.get("latitude")),
                                      longitude: Number(data.get("longitude")),
                                      timezone: data.get("timezone"),
                                  },
                                  token,
                              );
                        onCreated(site);
                    } catch (error) {
                        setStatus({
                            kind: "error",
                            message:
                                error instanceof Error
                                    ? error.message
                                    : "Unable to save this plant.",
                        });
                    }
                }}
            >
                <fieldset disabled={status.kind === "saving"}>
                    <div data-step="0" hidden={step !== 0}>
                        <div className="form-grid">
                            {!existing && (
                                <Field
                                    label="Plant name"
                                    name="name"
                                    placeholder="e.g. Antalya solar park"
                                />
                            )}
                            <Field
                                label="IANA timezone"
                                name="timezone"
                                defaultValue={
                                    existing?.timezone ??
                                    Intl.DateTimeFormat().resolvedOptions()
                                        .timeZone
                                }
                                placeholder="Europe/Istanbul"
                            />
                            <Field
                                label="Latitude (°)"
                                name="latitude"
                                type="number"
                                min={-90}
                                max={90}
                                step="any"
                                defaultValue={existing?.latitude}
                            />
                            <Field
                                label="Longitude (°)"
                                name="longitude"
                                type="number"
                                min={-180}
                                max={180}
                                step="any"
                                defaultValue={existing?.longitude}
                            />
                        </div>
                        <p className="fine">
                            Use the plant's coordinates and local timezone for
                            daily forecasts.
                        </p>
                    </div>
                    <div data-step="1" hidden={step !== 1}>
                        <PlantConfigurationFields
                            includeName={!!existing}
                            name={existing?.name}
                        />
                    </div>
                    <div data-step="2" hidden={step !== 2}>
                        <h3>Check your plant details</h3>
                        <dl className="review-grid">
                            <div>
                                <dt>Plant</dt>
                                <dd>
                                    {String(
                                        review.name ?? existing?.name ?? "",
                                    )}
                                </dd>
                            </div>
                            <div>
                                <dt>Location</dt>
                                <dd>
                                    {String(
                                        review.latitude ??
                                            existing?.latitude ??
                                            "",
                                    )}
                                    °,{" "}
                                    {String(
                                        review.longitude ??
                                            existing?.longitude ??
                                            "",
                                    )}
                                    °
                                </dd>
                            </div>
                            <div>
                                <dt>Timezone</dt>
                                <dd>
                                    {String(
                                        review.timezone ??
                                            existing?.timezone ??
                                            "",
                                    )}
                                </dd>
                            </div>
                            <div>
                                <dt>DC nameplate</dt>
                                <dd>
                                    {String(review.dc_capacity_mw ?? "")} MW
                                </dd>
                            </div>
                            <div>
                                <dt>Inverter AC capacity</dt>
                                <dd>
                                    {review.inverter_capacity_mw
                                        ? `${review.inverter_capacity_mw} MW`
                                        : "Unknown"}
                                </dd>
                            </div>
                            <div>
                                <dt>Grid export limit</dt>
                                <dd>
                                    {review.export_limit_mw
                                        ? `${review.export_limit_mw} MW`
                                        : "Unknown"}
                                </dd>
                            </div>
                            <div>
                                <dt>Measurement point</dt>
                                <dd>
                                    {review.measurement_basis ===
                                    "ac_generation"
                                        ? "AC generation"
                                        : "Net export"}
                                </dd>
                            </div>
                            <div>
                                <dt>Mounting</dt>
                                <dd>
                                    {String(review.mounting_type ?? "Unknown")}
                                    {review.mounting_type === "fixed"
                                        ? ` · tilt ${review.surface_tilt || "unknown"}° · azimuth ${review.surface_azimuth || "unknown"}°`
                                        : ""}
                                </dd>
                            </div>
                        </dl>
                        <Notice>
                            Your plant will wait for administrator approval.
                            Approval and forecast availability are shown
                            separately.
                        </Notice>
                    </div>
                    <div className="form-footer onboarding-actions">
                        {step > (existing ? 1 : 0) && (
                            <button
                                className="button secondary"
                                type="button"
                                onClick={() => move(step - 1)}
                            >
                                Back
                            </button>
                        )}
                        <button className="button" type="submit">
                            {status.kind === "saving"
                                ? "Submitting…"
                                : step === 2
                                  ? "Submit plant for approval"
                                  : "Continue"}
                        </button>
                    </div>
                </fieldset>
                {status.kind === "error" && (
                    <Notice error>{status.message}</Notice>
                )}
            </form>
        </section>
    );
}
