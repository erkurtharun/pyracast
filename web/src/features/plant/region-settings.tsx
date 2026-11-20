"use client";
import { useState } from "react";
import { useTranslations, useFormatter } from "next-intl";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import type { components } from "@/lib/schema";
import { Resource } from "@/ui/resource";
import { ActionForm, Field, Notice, Tag } from "@/ui/ui";
import { useCanManageWorkspace } from "@/ui/workspace-access";

type LocationRequest = components["schemas"]["LocationChangeOutput"];

export function RegionSettings({
    region,
    scope,
}: {
    region: Region;
    scope: RegionScope;
}) {
    const t = useTranslations("RegionSettings");
    return (
        <section className="panel">
            <h2>{t("plantLocation")}</h2>
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
    const format = useFormatter();
    const refresh = useRefresh();
    const requests = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/location-change-requests/",
        { path: scope },
    );
    const latest = requests.data?.results[0];
    const place = (row: { latitude: number; longitude: number }) => ({
        latitude: format.number(row.latitude, { maximumFractionDigits: 6 }),
        longitude: format.number(row.longitude, { maximumFractionDigits: 6 }),
    });
    return (
        <>
            <p>
                {t("currentLocation", {
                    ...place(region),
                    timezone: region.timezone,
                })}
            </p>
            <p>{t("approvalHelp")}</p>
            <Resource resource={requests} loading={t("loadingRequests")}>
                {() => (
                    <>
                        {latest && latest.status !== "pending" && (
                            <Outcome request={latest} />
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
                                                timezone: String(
                                                    data.get("timezone") ?? "",
                                                ),
                                                reason: String(
                                                    data.get("reason") ?? "",
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
                                        maxLength={100}
                                        defaultValue={region.timezone}
                                    />
                                    <Field
                                        label={t("reasonOptional")}
                                        name="reason"
                                        maxLength={1000}
                                        required={false}
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

function Outcome({ request }: { request: LocationRequest }) {
    const t = useTranslations("RegionSettings");
    const format = useFormatter();
    const date = request.reviewed_at
        ? format.dateTime(new Date(request.reviewed_at), {
              year: "numeric",
              month: "short",
              day: "numeric",
          })
        : "";
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
    const canManage = useCanManageWorkspace();
    const refresh = useRefresh();
    const [withdrawal, setWithdrawal] = useState<
        { status: "idle" | "working" } | { status: "failed"; message: string }
    >({ status: "idle" });
    async function withdraw() {
        setWithdrawal({ status: "working" });
        try {
            await api.post(
                "/api/tenants/{tenant_id}/regions/{region_id}/location-change-requests/{request_id}/withdraw/",
                { path: { ...scope, request_id: request.id } },
            );
            setWithdrawal({ status: "idle" });
            await refresh.workspace(scope.tenant_id);
        } catch (failure) {
            setWithdrawal({
                status: "failed",
                message: (failure as Error).message,
            });
        }
    }
    return (
        <div>
            <Tag>{request.status}</Tag> <span>{location}</span>
            {withdrawal.status === "failed" && (
                <Notice error>{withdrawal.message}</Notice>
            )}
            {canManage && (
                <button
                    className="button secondary"
                    disabled={withdrawal.status === "working"}
                    onClick={() => void withdraw()}
                >
                    {t("withdraw")}
                </button>
            )}
        </div>
    );
}
