"use client";
import { useTranslations } from "next-intl";

import { useRef, useState } from "react";
import type { Region } from "@/lib/api";
import { api } from "@/lib/api/client";
import { displayMessage } from "@/lib/api/errors";
import { deviceZone } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";
import { Field, formText } from "@/ui/form";
import { Notice } from "@/ui/ui";
import {
    PlantConfigurationFields,
    configurationInput,
} from "./plant-configuration-fields";

export type PlantSite = components["schemas"]["SiteOutput"];

export function PlantOnboarding({
    tenant,
    existing,
    onCreated,
    onCancel,
}: {
    tenant: number;
    existing?: Region;
    onCreated: (site: PlantSite) => void;
    onCancel: () => void;
}) {
    const t = useTranslations("PlantOnboarding");
    const format = useFormat();
    const reviewNumber = (value: unknown) =>
        value == null || value === "" ? "" : format.number(Number(value), 6);

    const [step, setStep] = useState(existing ? 1 : 0);
    const [review, setReview] = useState<Record<string, string>>({});
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
                            ? t("completePlantSetup")
                            : t("addASolarPlant")}
                    </h2>
                    <p>{t("description")}</p>
                </div>
                <button className="button secondary" onClick={onCancel}>
                    {t("cancel")}
                </button>
            </div>
            <ol
                className="onboarding-steps"
                aria-label={t("plantSetupProgress")}
            >
                {[t("location"), t("technicalDetails"), t("review")].map(
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
                onSubmit={(event) =>
                    void (async () => {
                        event.preventDefault();
                        const form = event.currentTarget;
                        const section = form.querySelector(
                            `[data-step="${step}"]`,
                        );
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
                            setReview(
                                Object.fromEntries(
                                    [...data.keys()].map((key) => [
                                        key,
                                        formText(data, key),
                                    ]),
                                ),
                            );
                            move(step + 1);
                            return;
                        }
                        setStatus({ kind: "saving" });
                        try {
                            const body = configurationInput(data);
                            const site = existing
                                ? {
                                      ...existing,
                                      reminder: null,
                                      plant: await api.post(
                                          "/api/tenants/{tenant_id}/regions/{region_id}/plants/",
                                          {
                                              path: {
                                                  tenant_id: tenant,
                                                  region_id: existing.id,
                                              },
                                              body,
                                          },
                                      ),
                                  }
                                : await api.post(
                                      "/api/tenants/{tenant_id}/sites/",
                                      {
                                          path: { tenant_id: tenant },
                                          body: {
                                              ...body,
                                              latitude: Number(
                                                  data.get("latitude"),
                                              ),
                                              longitude: Number(
                                                  data.get("longitude"),
                                              ),
                                              timezone: formText(
                                                  data,
                                                  "timezone",
                                              ),
                                          },
                                      },
                                  );
                            onCreated(site);
                        } catch (error) {
                            setStatus({
                                kind: "error",
                                message: displayMessage(
                                    error,
                                    t("unableToSaveThisPlant"),
                                ),
                            });
                        }
                    })()
                }
            >
                <fieldset disabled={status.kind === "saving"}>
                    <div data-step="0" hidden={step !== 0}>
                        <div className="form-grid">
                            {!existing && (
                                <Field
                                    label={t("plantName")}
                                    name="name"
                                    placeholder={t("examplePlantName")}
                                    required
                                />
                            )}
                            <Field
                                label={t("ianaTimezone")}
                                name="timezone"
                                defaultValue={
                                    existing?.timezone ?? deviceZone()
                                }
                                placeholder="Europe/Istanbul"
                                required
                            />
                            <Field
                                label={t("latitude")}
                                name="latitude"
                                type="number"
                                min={-90}
                                max={90}
                                step="any"
                                defaultValue={existing?.latitude}
                                required
                            />
                            <Field
                                label={t("longitude")}
                                name="longitude"
                                type="number"
                                min={-180}
                                max={180}
                                step="any"
                                defaultValue={existing?.longitude}
                                required
                            />
                        </div>
                        <p className="fine">{t("locationHelp")}</p>
                    </div>
                    <div data-step="1" hidden={step !== 1}>
                        <PlantConfigurationFields
                            includeName={!!existing}
                            name={existing?.name}
                        />
                    </div>
                    <div data-step="2" hidden={step !== 2}>
                        <h3>{t("checkYourPlantDetails")}</h3>
                        <dl className="review-grid">
                            <div>
                                <dt>{t("plant")}</dt>
                                <dd>{review.name ?? existing?.name ?? ""}</dd>
                            </div>
                            <div>
                                <dt>{t("location")}</dt>
                                <dd>
                                    {t("coordinates", {
                                        latitude: reviewNumber(
                                            review.latitude ??
                                                existing?.latitude,
                                        ),
                                        longitude: reviewNumber(
                                            review.longitude ??
                                                existing?.longitude,
                                        ),
                                    })}
                                </dd>
                            </div>
                            <div>
                                <dt>{t("timezone")}</dt>
                                <dd>
                                    {review.timezone ??
                                        existing?.timezone ??
                                        ""}
                                </dd>
                            </div>
                            <div>
                                <dt>{t("dcNameplate")}</dt>
                                <dd>
                                    {t("power", {
                                        value: reviewNumber(
                                            review.dc_capacity_mw,
                                        ),
                                    })}
                                </dd>
                            </div>
                            <div>
                                <dt>{t("inverterACCapacity")}</dt>
                                <dd>
                                    {review.inverter_capacity_mw
                                        ? t("power", {
                                              value: reviewNumber(
                                                  review.inverter_capacity_mw,
                                              ),
                                          })
                                        : t("unknown")}
                                </dd>
                            </div>
                            <div>
                                <dt>{t("gridExportLimit")}</dt>
                                <dd>
                                    {review.export_limit_mw
                                        ? t("power", {
                                              value: reviewNumber(
                                                  review.export_limit_mw,
                                              ),
                                          })
                                        : t("unknown")}
                                </dd>
                            </div>
                            <div>
                                <dt>{t("measurementPoint")}</dt>
                                <dd>
                                    {review.measurement_basis ===
                                    "ac_generation"
                                        ? t("acGeneration")
                                        : t("netExport")}
                                </dd>
                            </div>
                            <div>
                                <dt>{t("mounting")}</dt>
                                <dd>
                                    {review.mounting_type === "fixed"
                                        ? t("mountingFixed")
                                        : review.mounting_type === "tracking"
                                          ? t("mountingTracking")
                                          : t("mountingUnknown")}
                                    {review.mounting_type === "fixed"
                                        ? t("geometry", {
                                              tilt:
                                                  reviewNumber(
                                                      review.surface_tilt_degrees,
                                                  ) || t("unknownGeometry"),
                                              azimuth:
                                                  reviewNumber(
                                                      review.surface_azimuth_degrees,
                                                  ) || t("unknownGeometry"),
                                          })
                                        : ""}
                                </dd>
                            </div>
                        </dl>
                        <Notice>{t("approvalNotice")}</Notice>
                    </div>
                    <div className="form-footer onboarding-actions">
                        {step > (existing ? 1 : 0) && (
                            <button
                                className="button secondary"
                                type="button"
                                onClick={() => move(step - 1)}
                            >
                                {t("back")}
                            </button>
                        )}
                        <button className="button" type="submit">
                            {status.kind === "saving"
                                ? t("submitting")
                                : step === 2
                                  ? t("submitPlantForApproval")
                                  : t("continue")}
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
