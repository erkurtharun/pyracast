"use client";
import { useTranslations, useFormatter } from "next-intl";
import { useState } from "react";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useApiList, useRefresh } from "@/lib/api/query";
import { ActionForm, Field, Notice, Tag } from "./ui";

export function RegionSettings({
    region,
    scope,
}: {
    region: Region;
    scope: RegionScope;
}) {
    const t = useTranslations("RegionSettings");
    const refresh = useRefresh();
    const [version, setVersion] = useState(0);
    return (
        <section className="panel">
            <h2>{t("plantLocation")}</h2>
            <details>
                <summary>{t("requestLocationChange")}</summary>
                <LocationRequests
                    key={version}
                    region={region}
                    scope={scope}
                    onSubmitted={async () => {
                        await refresh.workspace(scope.tenant_id);
                        setVersion((value) => value + 1);
                    }}
                />
            </details>
        </section>
    );
}

function LocationRequests({
    region,
    scope,
    onSubmitted,
}: {
    region: Region;
    scope: RegionScope;
    onSubmitted: () => void;
}) {
    const t = useTranslations("RegionSettings");
    const format = useFormatter();

    const requests = useApiList(
        "/api/tenants/{tenant_id}/regions/{region_id}/location-change-requests/",
        {
            path: scope,
        },
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
                        await api.post(
                            "/api/tenants/{tenant_id}/regions/{region_id}/location-change-requests/",
                            {
                                path: scope,
                                body: {
                                    latitude: Number(data.get("latitude")),
                                    longitude: Number(data.get("longitude")),
                                    timezone: String(
                                        data.get("timezone") ?? "",
                                    ),
                                    reason: String(data.get("reason") ?? ""),
                                    expected_revision: region.revision,
                                },
                            },
                        );
                        onSubmitted();
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
