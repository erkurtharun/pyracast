"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { Site } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError, displayMessage } from "@/lib/api/errors";
import { useApiList, useRefresh } from "@/lib/api/query";
import { MISSING } from "@/lib/format/number";
import { deviceZone } from "@/lib/format/time";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";
import {
    Field,
    FieldErrors,
    formText,
    UnconfirmedTitle,
    useWriteErrorMessage,
} from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useForgetDeletion } from "@/features/trash/recent-deletion";
import {
    onboardingSteps,
    refusalTarget,
    unplacedFields,
} from "./onboarding-steps";
import {
    PlantConfigurationFields,
    configurationInput,
} from "./plant-configuration-fields";
import { CoordinateField } from "./coordinate-field";
import {
    binnedAt,
    coordinateValue,
    type CoordinateAxis,
} from "./site-location";
import { TimezoneField } from "./timezone-field";

type Trashed = components["schemas"]["TrashedRegionOutput"];
type LocationRefusal = components["schemas"]["LocationRefusal"];
type Submission =
    | { kind: "idle" }
    | { kind: "saving" }
    | {
          kind: "error";
          error: unknown;
          fields: Record<string, string>;
          holder: number | null;
      };
type Found = { row: Trashed; others: number };
type Offer =
    | { kind: "none" }
    | ({ kind: "offered" | "restoring" } & Found)
    | ({ kind: "failed"; message: string } & Found);

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
        typeof error.body !== "object"
    )
        return null;
    const { region_id } = error.body as Partial<LocationRefusal>;
    return typeof region_id === "number" ? region_id : null;
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
    const decision = useTranslations("PlantWorkspace");
    const common = useTranslations("Common");
    const writeErrorMessage = useWriteErrorMessage();
    const format = useFormat();
    const refresh = useRefresh();
    const forget = useForgetDeletion();
    const reviewNumber = (value: unknown) =>
        value == null || value === "" ? "" : format.number(Number(value), 6);
    const reviewAngle = (value: unknown) => {
        const shown = reviewNumber(value);
        return shown ? `${shown}°` : MISSING;
    };
    const refused = existing?.status === "rejected";
    const steps = onboardingSteps(!!existing);
    const last = steps.length - 1;
    const [step, setStep] = useState(0);
    const [review, setReview] = useState<Record<string, string>>({});
    const reviewCoordinate = (axis: CoordinateAxis) => {
        const typed = review[axis];
        return reviewNumber(
            typed ? coordinateValue(axis, typed) : existing?.[axis],
        );
    };
    const [status, setStatus] = useState<Submission>({ kind: "idle" });
    const [offer, setOffer] = useState<Offer>({ kind: "none" });
    // React disables the form only once it commits; these refs act before that and after an unmount.
    const running = useRef(false);
    const open = useRef(true);
    useEffect(() => {
        open.current = true;
        return () => {
            open.current = false;
        };
    }, []);
    const title = useRef<HTMLHeadingElement>(null);
    const form = useRef<HTMLFormElement>(null);
    const restoreButton = useRef<HTMLButtonElement>(null);
    const errorAlert = useRef<HTMLDivElement>(null);
    // A disabled control loses focus: the restore button while it restores, the submit button
    // while it saves.
    useEffect(() => {
        if (offer.kind === "offered" || offer.kind === "failed")
            restoreButton.current?.focus();
    }, [offer.kind]);
    useEffect(() => {
        if (
            status.kind === "error" &&
            !refusalTarget(Object.keys(status.fields), !!existing)
        )
            errorAlert.current?.focus();
    }, [status, existing]);
    const saving = status.kind === "saving";
    const busy = saving || offer.kind === "restoring";
    const fields = status.kind === "error" ? status.fields : {};
    const holder = status.kind === "error" ? status.holder : null;
    const failure =
        status.kind === "error"
            ? writeErrorMessage(status.error, {
                  fallback: t("unableToSaveThisPlant"),
              })
            : "";
    const at = (name: (typeof steps)[number]) => steps.indexOf(name);
    // Focus only after the commit that shows the step and re-enables its fields.
    const [focusRequest, setFocusRequest] = useState<{ field?: string }>();
    useEffect(() => {
        if (!focusRequest) return;
        const field = focusRequest.field
            ? form.current?.elements.namedItem(focusRequest.field)
            : null;
        const control = field instanceof RadioNodeList ? field[0] : field;
        (control instanceof HTMLElement ? control : title.current)?.focus();
    }, [focusRequest]);
    const move = (next: number, focus?: string) => {
        setStep(next);
        setFocusRequest({ field: focus });
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
            ? binnedAt(trashed, {
                  latitude: coordinateValue(
                      "latitude",
                      formText(data, "latitude"),
                  ),
                  longitude: coordinateValue(
                      "longitude",
                      formText(data, "longitude"),
                  ),
              })
            : null;
    async function restore(found: Found) {
        if (running.current) return;
        running.current = true;
        setOffer({ ...found, kind: "restoring" });
        try {
            await api.post(
                "/api/tenants/{tenant_id}/regions/{region_id}/restore/",
                { path: { tenant_id: tenant, region_id: found.row.id } },
            );
        } catch (error) {
            running.current = false;
            setOffer({
                ...found,
                kind: "failed",
                message: displayMessage(error, t("unableToRestore")),
            });
            return;
        }
        forget(found.row.id);
        await refresh.workspace(tenant);
        running.current = false;
        if (open.current) onOpen(found.row.id);
    }
    async function submit(data: FormData) {
        if (running.current) return;
        running.current = true;
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
                          latitude: coordinateValue(
                              "latitude",
                              formText(data, "latitude"),
                          ),
                          longitude: coordinateValue(
                              "longitude",
                              formText(data, "longitude"),
                          ),
                          timezone: formText(data, "timezone"),
                      },
                  });
            running.current = false;
            if (open.current) onCreated(site);
        } catch (error) {
            running.current = false;
            const refused = error instanceof ApiError ? error.fields : {};
            setStatus({
                kind: "error",
                error,
                fields: refused,
                holder: holderOf(error),
            });
            const target = refusalTarget(Object.keys(refused), !!existing);
            if (target) move(target.step, target.field);
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
                    {existing && refused ? (
                        <>
                            <p>{decision("rejectedNotice")}</p>
                            {existing.review_reason && (
                                <p>{existing.review_reason}</p>
                            )}
                            <p>{decision("resubmitHelp")}</p>
                        </>
                    ) : (
                        <p>{t("description")}</p>
                    )}
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
                    disabled={busy}
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
                    if (running.current) return;
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
                    const found = binned(data);
                    if (found) setOffer({ kind: "offered", ...found });
                    else if (step < last) advance();
                    else void submit(data);
                }}
            >
                <FieldErrors value={fields}>
                    <fieldset disabled={busy}>
                        {!existing && (
                            <div
                                data-step={at("location")}
                                hidden={step !== at("location")}
                            >
                                <div className="form-grid">
                                    <Field
                                        label={t("plantName")}
                                        name="name"
                                        placeholder={t("examplePlantName")}
                                        maxLength={200}
                                        required
                                    />
                                    <TimezoneField
                                        label={t("timezone")}
                                        defaultValue={deviceZone()}
                                    />
                                    <CoordinateField
                                        axis="latitude"
                                        label={t("latitude")}
                                    />
                                    <CoordinateField
                                        axis="longitude"
                                        label={t("longitude")}
                                    />
                                </div>
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
                                        {offer.others > 0 && (
                                            <p>
                                                {t("inBinMore", {
                                                    count: offer.others,
                                                })}
                                            </p>
                                        )}
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
                                                    void restore(offer)
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
                                            latitude:
                                                reviewCoordinate("latitude"),
                                            longitude:
                                                reviewCoordinate("longitude"),
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
                                        {reviewNumber(review.dc_capacity_mw)}
                                    </dd>
                                </div>
                                <div>
                                    <dt>{t("inverterACCapacity")}</dt>
                                    <dd>
                                        {review.inverter_capacity_mw
                                            ? reviewNumber(
                                                  review.inverter_capacity_mw,
                                              )
                                            : t("unknown")}
                                    </dd>
                                </div>
                                <div>
                                    <dt>{t("gridExportLimit")}</dt>
                                    <dd>
                                        {review.export_limit_mw
                                            ? reviewNumber(
                                                  review.export_limit_mw,
                                              )
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
                                                  tilt: reviewAngle(
                                                      review.surface_tilt_degrees,
                                                  ),
                                                  azimuth: reviewAngle(
                                                      review.surface_azimuth_degrees,
                                                  ),
                                              })
                                            : ""}
                                    </dd>
                                </div>
                            </dl>
                            <Notice>
                                {refused
                                    ? t("resubmitAfterSave")
                                    : t("approvalNotice")}
                            </Notice>
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
                                {step < last
                                    ? t("continue")
                                    : refused
                                      ? saving
                                          ? common("saving")
                                          : decision("saveConfiguration")
                                      : saving
                                        ? t("submitting")
                                        : t("submitPlantForApproval")}
                            </button>
                        </div>
                    </fieldset>
                </FieldErrors>
                {status.kind === "error" && (
                    <div
                        className="notice error"
                        role="alert"
                        tabIndex={-1}
                        ref={errorAlert}
                    >
                        <UnconfirmedTitle error={status.error} />
                        <p>{failure}</p>
                        {unplacedFields(fields, !!existing, failure).map(
                            ([field, message]) => (
                                <p key={field}>{message}</p>
                            ),
                        )}
                        {holder !== null && (
                            <button
                                className="button secondary"
                                type="button"
                                onClick={() => onOpen(holder)}
                            >
                                {t("openExisting")}
                            </button>
                        )}
                    </div>
                )}
            </form>
        </section>
    );
}
