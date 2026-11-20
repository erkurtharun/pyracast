"use client";
import { useTranslations, useLocale } from "next-intl";
import { useState } from "react";
import type { components } from "@/lib/schema";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiQuery, useRefresh } from "@/lib/api/query";
import { collectionOutcomeMessages } from "@/lib/collection-outcomes";
import { Resource } from "@/ui/resource";
import { ActionForm, Notice, Stat } from "@/ui/ui";

type Schedule = components["schemas"]["CollectionScheduleOutput"];

export function CollectionSchedule({
    scope,
    region,
}: {
    scope: RegionScope;
    region: Region;
}) {
    const t = useTranslations("CollectionSchedule");
    const { tenant_id, region_id } = scope;
    const refresh = useRefresh();
    const schedule = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/collection-schedule/",
        { path: { tenant_id, region_id } },
        { poll: cadence.live },
    );
    return (
        <Resource resource={schedule} loading={t("loadingCollectionSchedule")}>
            {(data) => (
                <ScheduleEditor
                    key={JSON.stringify([data.enabled, data.times])}
                    data={data}
                    scope={scope}
                    region={region}
                    onSaved={() => refresh.workspace(tenant_id)}
                />
            )}
        </Resource>
    );
}

function ScheduleEditor({
    data,
    scope,
    region,
    onSaved,
}: {
    data: Schedule;
    scope: RegionScope;
    region: Region;
    onSaved: () => void;
}) {
    const t = useTranslations("CollectionSchedule");
    const locale = useLocale();
    const endpointLabels: Record<string, string> = {
        forecasts: t("forecast"),
        estimated_actuals: t("historicalEstimates"),
    };
    const statusLabels: Record<string, string> = {
        pending: t("waiting"),
        running: t("inProgress"),
        succeeded: t("succeeded"),
        retry: t("retryScheduled"),
        failed: t("failed"),
        stale: t("supersededOrNoLongerEligible"),
    };
    const [times, setTimes] = useState(data.times);
    const moment = (value: string | null, missing: string) =>
        value
            ? new Date(value).toLocaleString(locale, {
                  timeZone: data.timezone,
                  dateStyle: "medium",
                  timeStyle: "short",
              })
            : missing;
    return (
        <>
            <section className="panel">
                <h2>{t("dataCollection")}</h2>
                <p>{t("chooseTimes", { timezone: data.timezone })}</p>
                <ActionForm
                    submit={t("saveCollectionSchedule")}
                    onSubmit={async (form) => {
                        const enabled = form.get("enabled") === "on";
                        if (
                            times.some(
                                (time) =>
                                    !/^([01]\d|2[0-3]):[0-5]\d$/.test(time),
                            )
                        )
                            throw new Error(t("enterAValidTimeInEveryRow"));
                        if (new Set(times).size !== times.length)
                            throw new Error(
                                t("eachCollectionTimeMustBeUnique"),
                            );
                        if (enabled && times.length === 0)
                            throw new Error(t("addAtLeastOneCollectionTime"));
                        const compareTimes = (a: string, b: string) =>
                            Number(a.replace(":", "")) -
                            Number(b.replace(":", ""));
                        const sorted = [...times].sort(compareTimes);
                        if (
                            enabled === data.enabled &&
                            JSON.stringify(sorted) ===
                                JSON.stringify(
                                    [...data.times].sort(compareTimes),
                                )
                        )
                            return;
                        await api.put(
                            "/api/tenants/{tenant_id}/regions/{region_id}/collection-schedule/",
                            {
                                path: scope,
                                body: { enabled, times: sorted },
                            },
                        );
                        onSaved();
                    }}
                >
                    <label className="check-field">
                        <input
                            type="checkbox"
                            name="enabled"
                            defaultChecked={data.enabled}
                        />{" "}
                        {t("enableDataCollection")}
                    </label>
                    {region.status !== "approved" && (
                        <Notice>{t("approvalRequired")}</Notice>
                    )}
                    <p>{t("collectionTimes", { timezone: data.timezone })}</p>
                    {times.map((time, index) => (
                        <div className="button-group" key={index}>
                            <label className="field">
                                <span className="sr-only">
                                    {t("time", { index: index + 1 })}
                                </span>
                                <input
                                    type="time"
                                    aria-label={t("collectionTime", {
                                        index: index + 1,
                                        timezone: data.timezone,
                                    })}
                                    value={time}
                                    onChange={(event) =>
                                        setTimes(
                                            times.map((item, i) =>
                                                i === index
                                                    ? event.target.value
                                                    : item,
                                            ),
                                        )
                                    }
                                />
                            </label>
                            <button
                                className="button secondary"
                                type="button"
                                onClick={() =>
                                    setTimes(
                                        times.filter((_, i) => i !== index),
                                    )
                                }
                                aria-label={t("removeTime", {
                                    index: index + 1,
                                })}
                            >
                                {t("remove")}
                            </button>
                        </div>
                    ))}
                    <button
                        className="button secondary"
                        type="button"
                        disabled={times.length >= data.max_forecast_times}
                        onClick={() => setTimes([...times, ""])}
                    >
                        {t("addTime")}
                    </button>
                    <p className="fine">
                        {t("dailyHistory", {
                            time: data.history_local_time,
                            timezone: data.timezone,
                        })}
                    </p>
                    <details>
                        <summary>{t("collectionDetails")}</summary>
                        <p className="fine">{t("historyHelp")}</p>
                    </details>
                </ActionForm>
            </section>
            <section className="panel">
                <h3>{t("collectionStatus")}</h3>
                <div className="stats">
                    <Stat
                        label={t("lastSuccess")}
                        value={moment(data.last_success_at, t("notYet"))}
                    />
                    <Stat
                        label={t("nextCollection")}
                        value={moment(
                            data.next_collection_at,
                            t("notScheduled"),
                        )}
                    />
                </div>
            </section>
            {data.recent_jobs?.length > 0 && (
                <details className="panel">
                    <summary>{t("recentCollectionJobs")}</summary>
                    <ul>
                        {data.recent_jobs.map((job) => (
                            <li key={job.id}>
                                {t("jobSummary", {
                                    endpoint:
                                        endpointLabels[job.endpoint] ??
                                        t("collection"),
                                    status:
                                        job.last_error === "history_incomplete"
                                            ? t("partiallyCollected")
                                            : (statusLabels[job.status] ??
                                              t("statusUnavailable")),
                                    time: new Date(
                                        job.scheduled_for ?? job.available_at,
                                    ).toLocaleString(locale, {
                                        timeZone: data.timezone,
                                    }),
                                    timezone: data.timezone,
                                    attempts: job.attempts,
                                })}
                                {job.last_error && job.status !== "succeeded"
                                    ? ` - ${t(collectionOutcomeMessages[job.last_error])}`
                                    : ""}
                            </li>
                        ))}
                    </ul>
                </details>
            )}
        </>
    );
}
