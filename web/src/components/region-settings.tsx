"use client";
import { useTranslations, useFormatter } from "next-intl";
import { useState } from "react";
import { request, type LocationChangeRequest, type Region } from "@/lib/api";
import { ActionForm, Field, Notice, Tag, useCollection } from "./ui";

export function RegionSettings({
    region,
    base,
    token,
}: {
    region: Region;
    base: string;
    token: string;
}) {
    const t = useTranslations("RegionSettings");

    const [version, setVersion] = useState(0);
    return (
        <section className="panel">
            <h2>{t("plantLocation")}</h2>
            <details>
                <summary>{t("requestLocationChange")}</summary>
                <LocationRequests
                    key={version}
                    region={region}
                    base={base}
                    token={token}
                    onSubmitted={() => setVersion((value) => value + 1)}
                />
            </details>
        </section>
    );
}

function LocationRequests({
    region,
    base,
    token,
    onSubmitted,
}: {
    region: Region;
    base: string;
    token: string;
    onSubmitted: () => void;
}) {
    const t = useTranslations("RegionSettings");
    const format = useFormatter();

    const requests = useCollection<LocationChangeRequest>(
        `${base}location-change-requests/`,
    );
    const pending = requests.data?.results.find(
        (row) => row.status === "pending",
    );
    return (
        <>
            <p>
                {t("currentLocation", {
                    latitude: format.number(region.latitude, {
                        maximumFractionDigits: 6,
                    }),
                    longitude: format.number(region.longitude, {
                        maximumFractionDigits: 6,
                    }),
                    timezone: region.timezone,
                })}
            </p>
            <p>{t("approvalHelp")}</p>
            {requests.loading ? (
                <p role="status">{t("loadingRequests")}</p>
            ) : requests.error ? (
                <Notice error>{requests.error}</Notice>
            ) : pending ? (
                <div>
                    <Tag>{pending.status}</Tag>{" "}
                    <span>
                        {t("location", {
                            latitude: format.number(pending.latitude, {
                                maximumFractionDigits: 6,
                            }),
                            longitude: format.number(pending.longitude, {
                                maximumFractionDigits: 6,
                            }),
                            timezone: pending.timezone,
                        })}
                    </span>
                </div>
            ) : (
                <ActionForm
                    submit={t("submitLocationRequest")}
                    onSubmit={async (data) => {
                        try {
                            await request<LocationChangeRequest>(
                                `${base}location-change-requests/`,
                                "POST",
                                {
                                    latitude: Number(data.get("latitude")),
                                    longitude: Number(data.get("longitude")),
                                    timezone: data.get("timezone"),
                                    reason: data.get("reason"),
                                    expected_revision: region.revision,
                                },
                                token,
                            );
                            onSubmitted();
                        } catch (error) {
                            const message =
                                error instanceof Error
                                    ? error.message
                                    : t("requestFailed");
                            if (message.includes("region_revision_changed"))
                                throw new Error(t("locationChanged"), {
                                    cause: error,
                                });
                            if (
                                message.includes(
                                    "location_change_already_pending",
                                )
                            )
                                throw new Error(t("requestPending"), {
                                    cause: error,
                                });
                            if (message.includes("location_unchanged"))
                                throw new Error(t("locationUnchanged"), {
                                    cause: error,
                                });
                            throw error;
                        }
                    }}
                >
                    <div className="form-grid">
                        <Field
                            label={t("latitude")}
                            name="latitude"
                            type="number"
                            min={-90}
                            max={90}
                            step="any"
                            defaultValue={region.latitude}
                        />
                        <Field
                            label={t("longitude")}
                            name="longitude"
                            type="number"
                            min={-180}
                            max={180}
                            step="any"
                            defaultValue={region.longitude}
                        />
                        <Field
                            label={t("ianaTimezone")}
                            name="timezone"
                            defaultValue={region.timezone}
                        />
                        <Field
                            label={t("reasonOptional")}
                            name="reason"
                            required={false}
                        />
                    </div>
                </ActionForm>
            )}
        </>
    );
}
