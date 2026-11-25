"use client";
import { useTranslations } from "next-intl";
import { useState } from "react";
import type { components } from "@/lib/schema";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { cadence, useApiQuery, useRefresh } from "@/lib/api/query";
import { DisplayError } from "@/lib/api/errors";
import { useFormat } from "@/lib/format/use-format";
import { ActionForm, CheckboxField } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { useLabel } from "@/ui/vocabulary";
import { CollectionStatus } from "./collection-status";

type Schedule = components["schemas"]["CollectionScheduleOutput"];
type Job = Schedule["recent_jobs"][number];
type JobError = components["schemas"]["CollectionJobErrorEnum"];

const retried = new Set<JobError>([
    "provider_allowance_exhausted",
    "provider_throttled",
    "provider_server_error",
    "provider_unreachable",
    "history_budget_reserved",
    "quota_exhausted",
    "lock_contention",
    "publication_failure",
]);
const attempting = new Set<Job["status"]>(["pending", "running", "retry"]);

function shownError({ status, last_error }: Job): JobError | null {
    if (!last_error || status === "succeeded") return null;
    if (!retried.has(last_error) || attempting.has(status)) return last_error;
    return status === "failed" ? "attempts_exhausted" : null;
}

function sentTimes(times: readonly string[]) {
    return [...times].sort(
        (a, b) => Number(a.replace(":", "")) - Number(b.replace(":", "")),
    );
}

function sameTimes(a: readonly string[], b: readonly string[]) {
    const sorted = sentTimes(b);
    return (
        a.length === b.length &&
        sentTimes(a).every((time, index) => time === sorted[index])
    );
}

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
                    onSaved={() => void refresh.workspace(tenant_id)}
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
    const format = useFormat();
    const label = useLabel();

    const [times, setTimes] = useState(data.times);
    const [saved, setSaved] =
        useState<Pick<Schedule, "enabled" | "times">>(data);
    return (
        <>
            <section className="panel">
                <h2>{t("dataCollection")}</h2>
                <ActionForm
                    submit={t("saveCollectionSchedule")}
                    unsaved={!sameTimes(times, saved.times)}
                    onSubmit={async (form) => {
                        const enabled = form.get("enabled") === "on";
                        if (
                            times.some(
                                (time) =>
                                    !/^([01]\d|2[0-3]):[0-5]\d$/.test(time),
                            )
                        )
                            throw new DisplayError(
                                t("enterAValidTimeInEveryRow"),
                            );
                        if (new Set(times).size !== times.length)
                            throw new DisplayError(
                                t("eachCollectionTimeMustBeUnique"),
                            );
                        if (enabled && times.length === 0)
                            throw new DisplayError(
                                t("addAtLeastOneCollectionTime"),
                            );
                        if (
                            enabled === saved.enabled &&
                            sameTimes(times, saved.times)
                        )
                            return;
                        setSaved(
                            await api.put(
                                "/api/tenants/{tenant_id}/regions/{region_id}/collection-schedule/",
                                {
                                    path: scope,
                                    body: { enabled, times: sentTimes(times) },
                                },
                            ),
                        );
                        onSaved();
                    }}
                >
                    <CheckboxField
                        label={t("enableDataCollection")}
                        name="enabled"
                        defaultChecked={data.enabled}
                        disabled={region.status !== "approved"}
                    />
                    <p>{t("collectionTimes")}</p>
                    {times.map((time, index) => (
                        <div className="button-group" key={index}>
                            <label className="field">
                                <span className="sr-only">
                                    {t("collectionTime", { index: index + 1 })}
                                </span>
                                <input
                                    type="time"
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
                        {t("dailyHistory", { time: data.history_local_time })}
                    </p>
                    <details>
                        <summary>{t("collectionDetails")}</summary>
                        <p className="fine">{t("historyHelp")}</p>
                    </details>
                </ActionForm>
            </section>
            <CollectionStatus data={data} region={region} />
            {data.recent_jobs?.length > 0 && (
                <details className="panel">
                    <summary>{t("recentCollectionJobs")}</summary>
                    <ul>
                        {data.recent_jobs.map((job) => {
                            const error = shownError(job);
                            return (
                                <li key={job.id}>
                                    {t("jobSummary", {
                                        endpoint: label(
                                            "CollectionEndpoint",
                                            job.endpoint,
                                        ),
                                        status:
                                            job.last_error ===
                                            "history_incomplete"
                                                ? t("partiallyCollected")
                                                : label(
                                                      "CollectionJobStatus",
                                                      job.status,
                                                  ),
                                        time: format.time(
                                            job.scheduled_for,
                                            data.timezone,
                                        ),
                                        attempts: job.attempts,
                                    })}
                                    {error
                                        ? ` · ${label("CollectionJobError", error)}`
                                        : ""}
                                </li>
                            );
                        })}
                    </ul>
                </details>
            )}
        </>
    );
}
