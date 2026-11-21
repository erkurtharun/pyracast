"use client";
import { useTranslations } from "next-intl";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";
import { ActionForm, AsyncButton, Field, formText } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { StatusTag } from "@/ui/vocabulary";
import { TimezoneField } from "@/features/portfolio/timezone-field";

type LocationRequest = components["schemas"]["LocationChangeOutput"];

export function RegionSettings({
    region,
    scope,
}: {
    region: Region;
    scope: RegionScope;
}) {
    const t = useTranslations("RegionSettings");
    const format = useFormat();
    return (
        <section className="panel">
            <h2>{t("plantLocation")}</h2>
            <p>
                {t("currentLocation", {
                    latitude: format.number(region.latitude, 6),
                    longitude: format.number(region.longitude, 6),
                    timezone: region.timezone,
                })}
            </p>
            <details>
                <summary>{t("requestLocationChange")}</summary>
                <LocationRequests region={region} scope={scope} />
            </details>
        </section>
    );
}

function LocationRequests({
    region,
    scope,
}: {
    region: Region;
    scope: RegionScope;
}) {
    const t = useTranslations("RegionSettings");
    const format = useFormat();
    const refresh = useRefresh();
    const requests = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/location-change-requests/",
        { path: scope },
    );
    const latest = requests.data?.results[0];
    const place = (row: { latitude: number; longitude: number }) => ({
        latitude: format.number(row.latitude, 6),
        longitude: format.number(row.longitude, 6),
    });
    return (
        <>
            <p>{t("approvalHelp")}</p>
            <Resource resource={requests} loading={t("loadingRequests")}>
                {() => (
                    <>
                        {latest && latest.status !== "pending" && (
                            <Outcome
                                request={latest}
                                timezone={region.timezone}
                            />
                        )}
                        {latest?.status === "pending" ? (
                            <PendingRequest
                                request={latest}
                                scope={scope}
                                location={t("location", {
                                    ...place(latest),
                                    timezone: latest.timezone,
                                })}
                            />
                        ) : (
                            <ActionForm
                                key={region.revision}
                                submit={t("submitLocationRequest")}
                                onSubmit={async (data) => {
                                    await api.post(
                                        "/api/tenants/{tenant_id}/regions/{region_id}/location-change-requests/",
                                        {
                                            path: scope,
                                            body: {
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
                                                reason: formText(
                                                    data,
                                                    "reason",
                                                ),
                                                expected_revision:
                                                    region.revision,
                                            },
                                        },
                                    );
                                    await refresh.workspace(scope.tenant_id);
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
                                        required
                                    />
                                    <Field
                                        label={t("longitude")}
                                        name="longitude"
                                        type="number"
                                        min={-180}
                                        max={180}
                                        step="any"
                                        defaultValue={region.longitude}
                                        required
                                    />
                                    <TimezoneField
                                        label={t("timezone")}
                                        defaultValue={region.timezone}
                                    />
                                    <Field
                                        label={t("reasonOptional")}
                                        name="reason"
                                        maxLength={1000}
                                    />
                                </div>
                            </ActionForm>
                        )}
                    </>
                )}
            </Resource>
        </>
    );
}

function Outcome({
    request,
    timezone,
}: {
    request: LocationRequest;
    timezone: string;
}) {
    const t = useTranslations("RegionSettings");
    const format = useFormat();
    const date = format.time(request.reviewed_at, timezone, {
        year: "numeric",
        month: "short",
        day: "numeric",
    });
    return (
        <div role="status">
            <p>
                {request.status === "approved"
                    ? t("latestApproved", { date })
                    : request.status === "rejected"
                      ? t("latestRejected", { date })
                      : t("latestWithdrawn", { date })}
            </p>
            {request.decision_reason && (
                <p>{t("decisionNote", { reason: request.decision_reason })}</p>
            )}
        </div>
    );
}

function PendingRequest({
    request,
    scope,
    location,
}: {
    request: LocationRequest;
    scope: RegionScope;
    location: string;
}) {
    const t = useTranslations("RegionSettings");
    const refresh = useRefresh();
    return (
        <div>
            <StatusTag group="LocationChangeStatus" value={request.status} />{" "}
            <span>{location}</span>
            <AsyncButton
                variant="secondary"
                onClick={async () => {
                    await api.post(
                        "/api/tenants/{tenant_id}/regions/{region_id}/location-change-requests/{request_id}/withdraw/",
                        { path: { ...scope, request_id: request.id } },
                    );
                    await refresh.workspace(scope.tenant_id);
                }}
            >
                {t("withdraw")}
            </AsyncButton>
        </div>
    );
}
