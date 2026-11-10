"use client";
import { useState } from "react";
import type { components } from "@/lib/schema";
import { request, type Region } from "@/lib/api";
import { ActionForm, Field, Notice, Stat, useResource } from "./ui";

type Schedule = components["schemas"]["CollectionScheduleOutput"];

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
    const resource = useResource<Schedule>(
        `${base}collection-schedule/`,
        version,
    );
    if (resource.loading)
        return <p role="status">Loading collection schedule…</p>;
    if (resource.error) return <Notice error>{resource.error}</Notice>;
    const data = resource.data!;
    return (
        <>
            <section className="panel">
                <h2>Data collection schedule</h2>
                <p>
                    Choose local times for collecting forecasts for this region.
                </p>
                <ActionForm
                    key={JSON.stringify(data)}
                    submit="Save collection schedule"
                    onSubmit={async (form) => {
                        const times = String(form.get("times"))
                            .split(",")
                            .map((time) => time.trim())
                            .filter(Boolean);
                        if (
                            times.some(
                                (time) =>
                                    !/^([01]\d|2[0-3]):[0-5]\d$/.test(time),
                            )
                        )
                            throw new Error(
                                "Enter times as HH:MM, separated by commas.",
                            );
                        await request(
                            `${base}collection-schedule/`,
                            "PUT",
                            {
                                enabled: form.get("enabled") === "on",
                                times: [...new Set(times)].sort(),
                            },
                            token,
                        );
                        setVersion(version + 1);
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
                    <Field
                        label={`Collection times (${data.timezone})`}
                        name="times"
                        defaultValue={data.times.join(", ")}
                        placeholder="08:00, 16:00"
                        required={false}
                    />
                    <p className="fine">
                        Separate times with commas. At the first scheduled time
                        each day, one additional request updates past estimated
                        actuals. Email reminders have a separate schedule and do
                        not request provider data.
                    </p>
                </ActionForm>
            </section>
            <div className="stats">
                <Stat
                    label="Daily request limit"
                    value={data.daily_request_limit}
                />
                <Stat label="Requests used today" value={data.used_today} />
                <Stat
                    label="Requests remaining today"
                    value={data.remaining_today}
                />
                <Stat
                    label="Maximum forecast times"
                    value={data.max_forecast_times}
                />
            </div>
        </>
    );
}
