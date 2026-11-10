"use client";
import { useState } from "react";
import type { components } from "@/lib/schema";
import { request, type Region } from "@/lib/api";
import { ActionForm, Empty, Field, Notice, Stat, Tag, useResource } from "./ui";
type Preference = components["schemas"]["ReminderOutput"];
export function Reminders({
    base,
    region,
    token,
}: {
    base: string;
    region: Region;
    token: string;
}) {
    const [version, setVersion] = useState(0);
    const resource = useResource<Preference>(`${base}reminders/`, version);
    if (resource.loading) return <p role="status">Loading email reminders…</p>;
    if (resource.error) return <Notice error>{resource.error}</Notice>;
    const data = resource.data!;
    return (
        <>
            <section className="panel">
                <div className="section-heading">
                    <div>
                        <h2>The right forecast. The right time.</h2>
                        <p>
                            Choose when the latest saved forecast is emailed to
                            you.
                        </p>
                    </div>
                    <Tag>{data.enabled ? "Enabled" : "Paused"}</Tag>
                </div>
                <ActionForm
                    key={JSON.stringify(data)}
                    submit="Save reminders"
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
                            `${base}reminders/`,
                            "PUT",
                            {
                                enabled: form.get("enabled") === "on",
                                email: form.get("email"),
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
                        Enable forecast emails for this region
                    </label>
                    {region.status !== "approved" && (
                        <Notice>
                            Reminders can be enabled once this region is
                            approved.
                        </Notice>
                    )}
                    <div className="form-grid">
                        <Field
                            label={`Delivery times (${region.timezone})`}
                            name="times"
                            defaultValue={data.times.join(", ")}
                            placeholder="08:00, 14:30"
                            required={false}
                        />
                        <Field
                            label="Email address (optional override)"
                            name="email"
                            type="email"
                            defaultValue={data.email}
                            required={false}
                        />
                    </div>
                    <p className="fine">
                        Separate times with commas. Leave email empty to use
                        your account email
                        {data.effective_email
                            ? ` (${data.effective_email})`
                            : ""}
                        . Remove all times and pause to clear your schedule.
                    </p>
                    <p className="fine">
                        Emails use the latest saved forecast and show its
                        collection time and age. Sending an email does not
                        request new provider data. Daylight-saving gaps are
                        skipped; repeated local times run once.
                    </p>
                </ActionForm>
            </section>
            <div className="stats">
                <Stat
                    label="Daily reminder limit"
                    value={data.quota?.daily_reminder_limit}
                    note="Per region, for your reminder schedule"
                />
                <Stat label="Scheduled times" value={data.times.length} />
                <Stat
                    label="Provider queries per email"
                    value={data.quota?.queries_per_email}
                />
            </div>
            <section className="panel">
                <h2>Recent deliveries</h2>
                {data.deliveries.length ? (
                    <div className="table-scroll">
                        <table>
                            <caption className="sr-only">
                                Recent forecast email deliveries
                            </caption>
                            <thead>
                                <tr>
                                    <th>Scheduled ({region.timezone})</th>
                                    <th>Status</th>
                                    <th>Collected</th>
                                    <th>Data age</th>
                                    <th>Sent</th>
                                    <th>Details</th>
                                </tr>
                            </thead>
                            <tbody>
                                {data.deliveries.map((row) => (
                                    <tr key={row.id}>
                                        <td>
                                            {new Date(
                                                row.scheduled_for,
                                            ).toLocaleString("en", {
                                                timeZone: region.timezone,
                                            })}
                                        </td>
                                        <td>
                                            <Tag>{row.status}</Tag>
                                        </td>
                                        <td>
                                            {row.collected_at
                                                ? new Date(
                                                      row.collected_at,
                                                  ).toLocaleString("en", {
                                                      timeZone: region.timezone,
                                                  })
                                                : "—"}
                                        </td>
                                        <td>
                                            {row.source_age_seconds == null
                                                ? "—"
                                                : `${Math.floor(row.source_age_seconds / 3600)}h ${Math.floor((row.source_age_seconds % 3600) / 60)}m`}
                                            {row.stale && <Tag>Stale</Tag>}
                                        </td>
                                        <td>
                                            {row.sent_at
                                                ? new Date(
                                                      row.sent_at,
                                                  ).toLocaleString("en", {
                                                      timeZone: region.timezone,
                                                  })
                                                : "—"}
                                        </td>
                                        <td>
                                            {row.late ? "Late delivery. " : ""}
                                            {row.last_error || "—"}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                ) : (
                    <Empty title="Your inbox has a brighter future">
                        Scheduled deliveries will appear here with their
                        collection and delivery status.
                    </Empty>
                )}
            </section>
        </>
    );
}
