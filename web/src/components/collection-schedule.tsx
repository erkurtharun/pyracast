"use client";
import { useEffect, useState } from "react";
import type { components } from "@/lib/schema";
import { request, type Region } from "@/lib/api";
import { ActionForm, Notice, Stat } from "./ui";

type Schedule = components["schemas"]["CollectionScheduleOutput"];

const endpointLabels: Record<string, string> = {
    forecasts: "Forecast",
    estimated_actuals: "Historical estimates",
};
const statusLabels: Record<string, string> = {
    pending: "Waiting",
    running: "In progress",
    succeeded: "Succeeded",
    retry: "Retry scheduled",
    failed: "Failed",
    stale: "Cancelled after settings changed",
};
const errorLabels: Record<string, string> = {
    provider_failure: "Provider request failed",
    quota_exhausted: "Daily account budget exhausted",
    invalid_source_kind: "Unexpected provider data",
    calculation_failure: "Forecast calculation failed",
    history_not_supported: "Historical estimates unavailable",
};

export function CollectionSchedule({
    base,
    region,
    token,
}: {
    base: string;
    region: Region;
    token: string;
}) {
    const [version, setVersion] = useState(0);
    const [data, setData] = useState<Schedule>();
    const [error, setError] = useState("");
    useEffect(() => {
        let active = true;
        let sequence = 0;
        const load = () => {
            const requestSequence = ++sequence;
            request<Schedule>(`${base}collection-schedule/`)
                .then((result) => {
                    if (active && requestSequence === sequence) {
                        setData(result);
                        setError("");
                    }
                })
                .catch((failure: Error) => {
                    if (active && requestSequence === sequence)
                        setError(failure.message);
                });
        };
        load();
        const timer = setInterval(load, 30_000);
        return () => {
            active = false;
            clearInterval(timer);
        };
    }, [base, version]);
    if (!data)
        return error ? (
            <Notice error>{error}</Notice>
        ) : (
            <p role="status">Loading collection schedule...</p>
        );
    return (
        <>
            {error && <Notice error>{error}</Notice>}
            <ScheduleEditor
                key={JSON.stringify([data.enabled, data.times])}
                data={data}
                base={base}
                region={region}
                token={token}
                onSaved={() => setVersion((current) => current + 1)}
            />
        </>
    );
}

function ScheduleEditor({
    data,
    base,
    region,
    token,
    onSaved,
}: {
    data: Schedule;
    base: string;
    region: Region;
    token: string;
    onSaved: () => void;
}) {
    const [times, setTimes] = useState(data.times);
    const nextReset = new Date(
        Date.parse(`${data.quota_utc_day}T00:00:00Z`) + 86_400_000,
    ).toLocaleString("en", {
        timeZone: data.timezone,
        dateStyle: "medium",
        timeStyle: "short",
    });
    return (
        <>
            <section className="panel">
                <h2>Data collection schedule</h2>
                <p>
                    Choose local times for collecting forecasts for this region.
                </p>
                <ActionForm
                    submit="Save collection schedule"
                    onSubmit={async (form) => {
                        const enabled = form.get("enabled") === "on";
                        if (
                            times.some(
                                (time) =>
                                    !/^([01]\d|2[0-3]):[0-5]\d$/.test(time),
                            )
                        )
                            throw new Error("Enter a valid time in every row.");
                        if (new Set(times).size !== times.length)
                            throw new Error(
                                "Each collection time must be unique.",
                            );
                        if (enabled && times.length === 0)
                            throw new Error(
                                "Add at least one collection time.",
                            );
                        const sorted = [...times].sort();
                        if (
                            enabled === data.enabled &&
                            JSON.stringify(sorted) ===
                                JSON.stringify([...data.times].sort())
                        )
                            return;
                        await request(
                            `${base}collection-schedule/`,
                            "PUT",
                            { enabled, times: sorted },
                            token,
                        );
                        onSaved();
                    }}
                >
                    <label className="check-field">
                        <input
                            type="checkbox"
                            name="enabled"
                            defaultChecked={data.enabled}
                            disabled={region.status !== "approved"}
                        />{" "}
                        Enable data collection
                    </label>
                    {region.status !== "approved" && (
                        <Notice>
                            Data collection can be enabled once this region is
                            approved and connected.
                        </Notice>
                    )}
                    <p>Collection times ({data.timezone})</p>
                    {times.map((time, index) => (
                        <div className="button-group" key={index}>
                            <label className="field">
                                Time {index + 1}
                                <input
                                    type="time"
                                    aria-label={`Collection time ${index + 1} (${data.timezone})`}
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
                                aria-label={`Remove collection time ${index + 1}`}
                            >
                                Remove
                            </button>
                        </div>
                    ))}
                    <button
                        className="button secondary"
                        type="button"
                        disabled={times.length >= data.max_forecast_times}
                        onClick={() => setTimes([...times, ""])}
                    >
                        + Add time
                    </button>
                    <p className="fine">
                        Each scheduled time requests a forecast. The first
                        eligible time each day also requests estimated actuals
                        (one additional request). Retries and connection
                        validation can use more requests, and all regions using
                        this provider account share its UTC daily quota. Email
                        reminders do not request provider data.
                    </p>
                </ActionForm>
            </section>
            <div className="stats">
                <Stat
                    label="Account daily request limit (UTC)"
                    value={data.daily_request_limit}
                />
                <Stat
                    label="Account requests used today (UTC)"
                    value={data.used_today}
                />
                <Stat
                    label="Tracked account budget remaining (UTC)"
                    value={data.remaining_today}
                />
                <Stat
                    label="Maximum forecast times per region"
                    value={data.max_forecast_times}
                />
            </div>
            <p className="fine">
                UTC quota day: {data.quota_utc_day}. This is Pyracast's tracked
                account usage, including validation and retries. Requests made
                outside Pyracast are unknown, so the tracked remaining budget is
                not a guarantee of provider quota. The UTC day resets at 00:00
                UTC ({nextReset} in {data.timezone}).
            </p>
            {data.requests_by_outcome && (
                <p className="fine">
                    Tracked attempts: {data.requests_by_outcome.succeeded}{" "}
                    succeeded, {data.requests_by_outcome.failed} failed,{" "}
                    {data.requests_by_outcome.uncertain} uncertain.
                </p>
            )}
            {data.recent_jobs?.length > 0 && (
                <section className="panel">
                    <h3>Recent collection jobs</h3>
                    <ul>
                        {data.recent_jobs.map((job) => (
                            <li key={job.id}>
                                {endpointLabels[job.endpoint] ?? "Collection"} -{" "}
                                {statusLabels[job.status] ??
                                    "Status unavailable"}{" "}
                                -{" "}
                                {new Date(
                                    job.scheduled_slot ?? job.available_at,
                                ).toLocaleString("en", {
                                    timeZone: data.timezone,
                                })}{" "}
                                ({data.timezone}) - {job.attempts} attempts
                                {job.last_error && job.status !== "succeeded"
                                    ? ` - ${errorLabels[job.last_error] ?? "Collection could not be completed"}`
                                    : ""}
                            </li>
                        ))}
                    </ul>
                </section>
            )}
        </>
    );
}
