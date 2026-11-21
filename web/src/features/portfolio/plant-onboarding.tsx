"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { Site } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError, displayMessage } from "@/lib/api/errors";
import { useApiList, useRefresh } from "@/lib/api/query";
import { deviceZone } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";
import { Field, FieldErrors, Select, formText } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useLabel } from "@/ui/vocabulary";
import { onboardingSteps, stepOfField } from "./onboarding-steps";
import {
    PlantConfigurationFields,
    configurationInput,
} from "./plant-configuration-fields";
import {
    coordinatePair,
    sameLocation,
    supportedZones,
    zoneChoices,
} from "./site-location";

type Trashed = components["schemas"]["TrashedRegionOutput"];
type Submission =
    | { kind: "idle" }
    | { kind: "saving" }
    | {
          kind: "error";
          message: string;
          fields: Record<string, string>;
          holder: number | null;
      };
type Offer =
    | { kind: "none" }
    | { kind: "offered"; row: Trashed }
    | { kind: "restoring"; row: Trashed }
    | { kind: "failed"; row: Trashed; message: string };

const stepLabels = {
    location: "location",
    technical: "technicalDetails",
    review: "review",
} as const;

// The id of the workspace's plant that already holds the refused location.
function holderOf(error: unknown): number | null {
    if (
        !(error instanceof ApiError) ||
        error.code !== "location_already_exists" ||
        !error.body ||
        typeof error.body !== "object" ||
        !("region_id" in error.body)
    )
        return null;
    return typeof error.body.region_id === "number"
        ? error.body.region_id
        : null;
}

export function PlantOnboarding({
    tenant,
    existing,
    onCreated,
    onCancel,
    onOpen,
}: {
    tenant: number;
    existing?: Site;
    onCreated: (site: Site) => void;
    onCancel: () => void;
    onOpen: (site: number) => void;
}) {
    const t = useTranslations("PlantOnboarding");
    const label = useLabel();
    const blocked = existing?.edit_blocked_reason;
    if (existing && blocked)
        return (
            <section className="panel onboarding">
                <div className="section-heading">
                    <h2>{t("completePlantSetup")}</h2>
                    <button className="button secondary" onClick={onCancel}>
                        {t("cancel")}
                    </button>
                </div>
                <Notice>{label("PlantEditBlock", blocked)}</Notice>
            </section>
        );
    const props = { tenant, onCreated, onCancel, onOpen };
    return existing ? (
        <Wizard {...props} existing={existing} trashed={[]} />
    ) : (
        <NewPlant {...props} />
    );
}

type WizardProps = {
    tenant: number;
    onCreated: (site: Site) => void;
    onCancel: () => void;
    onOpen: (site: number) => void;
};

// Only a new plant can land on a location that waits in the recycle bin.
function NewPlant(props: WizardProps) {
    const trash = useApiList("/api/tenants/{tenant_id}/trash/regions/", {
        path: { tenant_id: props.tenant },
    });
    return <Wizard {...props} trashed={trash.data?.results ?? []} />;
}

function Wizard({
    tenant,
    existing,
    trashed,
    onCreated,
    onCancel,
    onOpen,
}: WizardProps & { existing?: Site; trashed: readonly Trashed[] }) {
    const t = useTranslations("PlantOnboarding");
    const format = useFormat();
    const refresh = useRefresh();
    const reviewNumber = (value: unknown) =>
        value == null || value === "" ? "" : format.number(Number(value), 6);
    const steps = onboardingSteps(!!existing);
    const last = steps.length - 1;
    const [step, setStep] = useState(0);
    const [review, setReview] = useState<Record<string, string>>({});
    const [status, setStatus] = useState<Submission>({ kind: "idle" });
    const [offer, setOffer] = useState<Offer>({ kind: "none" });
    const [zones] = useState(() => zoneChoices(supportedZones(), deviceZone()));
    const title = useRef<HTMLHeadingElement>(null);
    const form = useRef<HTMLFormElement>(null);
    const restoreButton = useRef<HTMLButtonElement>(null);
    const offered = offer.kind === "offered";
    useEffect(() => {
        if (offered) restoreButton.current?.focus();
    }, [offered]);
    const saving = status.kind === "saving";
    const fields = status.kind === "error" ? status.fields : {};
    const holder = status.kind === "error" ? status.holder : null;
    const at = (name: (typeof steps)[number]) => steps.indexOf(name);
    const move = (next: number, focus?: string) => {
        setStep(next);
        requestAnimationFrame(() => {
            const field = focus
                ? form.current?.querySelector<HTMLElement>(`[name="${focus}"]`)
                : null;
            (field ?? title.current)?.focus();
        });
    };
    const advance = () => {
        if (!form.current) return;
        const data = new FormData(form.current);
        setReview(
            Object.fromEntries(
                [...data.keys()].map((key) => [key, formText(data, key)]),
            ),
        );
        setOffer({ kind: "none" });
        move(step + 1);
    };
    const binned = (data: FormData) =>
        step === at("location")
            ? trashed.find(
                  (row) =>
                      row.restore_blocker === null &&
                      sameLocation(row, {
                          latitude: Number(data.get("latitude")),
                          longitude: Number(data.get("longitude")),
                      }),
              )
            : undefined;
    async function restore(row: Trashed) {
        setOffer({ kind: "restoring", row });
        try {
            await api.post(
                "/api/tenants/{tenant_id}/regions/{region_id}/restore/",
                { path: { tenant_id: tenant, region_id: row.id } },
            );
        } catch (error) {
            setOffer({
                kind: "failed",
                row,
                message: displayMessage(error, t("unableToRestore")),
            });
            return;
        }
        await refresh.workspace(tenant);
        onOpen(row.id);
    }
    async function submit(data: FormData) {
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
                : await api.post("/api/tenants/{tenant_id}/sites/", {
                      path: { tenant_id: tenant },
                      body: {
                          ...body,
                          latitude: Number(data.get("latitude")),
                          longitude: Number(data.get("longitude")),
                          timezone: formText(data, "timezone"),
                      },
                  });
            onCreated(site);
        } catch (error) {
            const refused = error instanceof ApiError ? error.fields : {};
            setStatus({
                kind: "error",
                message: displayMessage(error, t("unableToSaveThisPlant")),
                fields: refused,
                holder: holderOf(error),
            });
            const target = stepOfField(Object.keys(refused), !!existing);
            if (target !== null) move(target, Object.keys(refused)[0]);
        }
    }
    return (
        <section className="panel onboarding">
            <div className="section-heading">
                <div>
                    <h2 ref={title} tabIndex={-1}>
                        {existing
                            ? t("completePlantSetup")
                            : t("addASolarPlant")}
                    </h2>
                    <p>
                        {existing?.status === "rejected"
                            ? t("rejectedSetup")
                            : t("description")}
                    </p>
                    {existing && (
                        <p className="fine">
                            {t("keptLocation", {
                                latitude: format.number(existing.latitude, 6),
                                longitude: format.number(existing.longitude, 6),
                                timezone: existing.timezone,
                            })}
                        </p>
                    )}
                </div>
                <button
                    className="button secondary"
                    disabled={saving}
                    onClick={onCancel}
                >
                    {t("cancel")}
                </button>
            </div>
            <ol
                className="onboarding-steps"
                aria-label={t("plantSetupProgress")}
            >
                {steps.map((name, index) => (
                    <li
                        key={name}
                        aria-current={step === index ? "step" : undefined}
                    >
                        {t(stepLabels[name])}
                    </li>
                ))}
            </ol>
            <form
                ref={form}
                noValidate
                onSubmit={(event) => {
                    event.preventDefault();
                    const section = event.currentTarget.querySelector(
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
                    const data = new FormData(event.currentTarget);
                    const row = binned(data);
                    if (row) setOffer({ kind: "offered", row });
                    else if (step < last) advance();
                    else void submit(data);
                }}
            >
                <FieldErrors value={fields}>
                    <fieldset disabled={saving}>
                        {!existing && (
                            <div
                                data-step={at("location")}
                                hidden={step !== at("location")}
                                onPaste={(event) => {
                                    const target = event.target;
                                    if (
                                        !(target instanceof HTMLInputElement) ||
                                        target.name !== "latitude"
                                    )
                                        return;
                                    const pair = coordinatePair(
                                        event.clipboardData.getData("text"),
                                    );
                                    const longitude =
                                        target.form?.elements.namedItem(
                                            "longitude",
                                        );
                                    if (
                                        !pair ||
                                        !(longitude instanceof HTMLInputElement)
                                    )
                                        return;
                                    event.preventDefault();
                                    target.value = pair[0];
                                    longitude.value = pair[1];
                                }}
                            >
                                <div className="form-grid">
                                    <Field
                                        label={t("plantName")}
                                        name="name"
                                        placeholder={t("examplePlantName")}
                                        maxLength={200}
                                        required
                                    />
                                    <Select
                                        label={t("timezone")}
                                        name="timezone"
                                        defaultValue={deviceZone()}
                                        required
                                    >
                                        {zones.map((zone) => (
                                            <option key={zone} value={zone}>
                                                {zone}
                                            </option>
                                        ))}
                                    </Select>
                                    <Field
                                        label={t("latitude")}
                                        name="latitude"
                                        type="number"
                                        min={-90}
                                        max={90}
                                        step="any"
                                        hint={t("pasteHint")}
                                        required
                                    />
                                    <Field
                                        label={t("longitude")}
                                        name="longitude"
                                        type="number"
                                        min={-180}
                                        max={180}
                                        step="any"
                                        required
                                    />
                                </div>
                                <p className="fine">{t("locationHelp")}</p>
                                {offer.kind !== "none" && (
                                    <div
                                        className="notice"
                                        role="group"
                                        aria-labelledby="binned-plant"
                                    >
                                        <h3 id="binned-plant">
                                            {t("inBin", {
                                                name: offer.row.plant_name,
                                            })}
                                        </h3>
                                        <p>{t("inBinHelp")}</p>
                                        {offer.kind === "failed" && (
                                            <Notice error>
                                                {offer.message}
                                            </Notice>
                                        )}
                                        <div className="button-group">
                                            <button
                                                ref={restoreButton}
                                                className="button"
                                                type="button"
                                                disabled={
                                                    offer.kind === "restoring"
                                                }
                                                onClick={() =>
                                                    void restore(offer.row)
                                                }
                                            >
                                                {offer.kind === "restoring"
                                                    ? t("restoring")
                                                    : t("restore", {
                                                          name: offer.row
                                                              .plant_name,
                                                      })}
                                            </button>
                                            <button
                                                className="button secondary"
                                                type="button"
                                                disabled={
                                                    offer.kind === "restoring"
                                                }
                                                onClick={advance}
                                            >
                                                {t("addNewPlant")}
                                            </button>
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}
                        <div
                            data-step={at("technical")}
                            hidden={step !== at("technical")}
                        >
                            <PlantConfigurationFields
                                includeName={!!existing}
                                name={existing?.name}
                            />
                        </div>
                        <div
                            data-step={at("review")}
                            hidden={step !== at("review")}
                        >
                            <h3>{t("checkYourPlantDetails")}</h3>
                            <dl className="review-grid">
                                <div>
                                    <dt>{t("plant")}</dt>
                                    <dd>
                                        {review.name ?? existing?.name ?? ""}
                                    </dd>
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
                                            : review.mounting_type ===
                                                "tracking"
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
                            {step > 0 && (
                                <button
                                    className="button secondary"
                                    type="button"
                                    onClick={() => move(step - 1)}
                                >
                                    {t("back")}
                                </button>
                            )}
                            <button className="button" type="submit">
                                {saving
                                    ? t("submitting")
                                    : step === last
                                      ? t("submitPlantForApproval")
                                      : t("continue")}
                            </button>
                        </div>
                    </fieldset>
                </FieldErrors>
                {status.kind === "error" && (
                    <Notice error>
                        <p>{status.message}</p>
                        {holder !== null && (
                            <button
                                className="button secondary"
                                type="button"
                                onClick={() => onOpen(holder)}
                            >
                                {t("openExisting")}
                            </button>
                        )}
                    </Notice>
                )}
            </form>
        </section>
    );
}
