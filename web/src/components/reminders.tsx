"use client";
import { ScrollRegion } from "./scroll-region";
import { useEffect, useId, useRef, useState } from "react";
import type { components } from "@/lib/schema";
import { request, type Region } from "@/lib/api";
import { ActionForm, Empty, Notice, Tag, useResource } from "./ui";
type Preference = components["schemas"]["ReminderOutput"];
const deliveryLabels: Record<string, string> = {
    pending: "Preparing",
    retry: "Retry scheduled",
    sending: "Sending",
    sent: "Accepted by mail service",
    failed: "Not sent",
    uncertain: "Acceptance unconfirmed",
    skipped: "Skipped",
    cancelled: "Cancelled",
    unavailable: "Forecast unavailable",
};
const forecastLabels: Record<string, string> = {
    current: "New forecast",
    saved_complete: "Saved forecast",
    saved_partial: "Partial forecast",
    unavailable: "No forecast available",
};
const deliveryDetails: Record<string, string> = {
    mail_not_sent:
        "The mail service was unavailable. Retry is bounded by the delivery window.",
    mail_rejected:
        "The mail service rejected the message. Check the recipient and mail settings.",
    mail_authentication_failed:
        "Mail service credentials need administrator attention. Delivery will retry within the sending window.",
    interrupted_connection:
        "The connection was interrupted before sending. A retry is scheduled.",
    authority_or_deadline_changed:
        "Sending stopped because access changed or the delivery window ended.",
    mail_outcome_uncertain:
        "Check the mail service before resending to avoid a duplicate.",
    interrupted_send:
        "Sending was interrupted. Check mail service acceptance before resending.",
    missed_schedule: "The delivery window has ended.",
    mail_deadline_exceeded:
        "The mail service did not accept the message within the delivery window.",
    preference_or_authority_changed: "Delivery settings or access changed.",
    missing_recipient: "No valid email address is available.",
    no_saved_forecast: "No saved forecast covers the requested day.",
};
function useRowFocus(rows: { id: number }[]) {
    const prefix = useId();
    const pending = useRef<number | null>(null);
    useEffect(() => {
        if (pending.current !== null) {
            document.getElementById(`${prefix}-${pending.current}`)?.focus();
            pending.current = null;
        }
    }, [prefix, rows]);
    return {
        prefix,
        focusRow: (id: number) => {
            pending.current = id;
        },
    };
}
function DeliveryTimes({
    data,
    timezone,
}: {
    data: Preference;
    timezone: string;
}) {
    const [rows, setRows] = useState(() =>
        (data.times.length ? data.times : [""]).map((value, id) => ({
            id,
            value,
        })),
    );
    const { prefix, focusRow } = useRowFocus(rows);
    const limit = data.quota?.daily_reminder_limit;
    return (
        <fieldset className="reminder-recipients">
            <legend>Delivery times ({timezone})</legend>
            {rows.map((row, index) => (
                <div className="recipient-row" key={row.id}>
                    <label className="field" htmlFor={`${prefix}-${row.id}`}>
                        Delivery time {index + 1}
                        <input
                            id={`${prefix}-${row.id}`}
                            type="time"
                            name="times"
                            value={row.value}
                            onChange={(event) =>
                                setRows(
                                    rows.map((item) =>
                                        item.id === row.id
                                            ? {
                                                  ...item,
                                                  value: event.target.value,
                                              }
                                            : item,
                                    ),
                                )
                            }
                        />
                    </label>
                    {rows.length > 1 && (
                        <button
                            type="button"
                            className="button secondary"
                            aria-label={`Remove delivery time ${index + 1}`}
                            onClick={() => {
                                focusRow(
                                    (rows[index + 1] ?? rows[index - 1]).id,
                                );
                                setRows(
                                    rows.filter((item) => item.id !== row.id),
                                );
                            }}
                        >
                            Remove
                        </button>
                    )}
                </div>
            ))}
            <button
                type="button"
                className="button secondary"
                disabled={limit != null && rows.length >= limit}
                onClick={() => {
                    const id = Math.max(-1, ...rows.map((row) => row.id)) + 1;
                    focusRow(id);
                    setRows([
                        ...rows,
                        {
                            id,
                            value: "",
                        },
                    ]);
                }}
            >
                Add time
            </button>
            {limit != null && (
                <span className="fine">
                    {" "}
                    {rows.filter((row) => row.value).length} / {limit}
                </span>
            )}
        </fieldset>
    );
}
function RecipientFields({ data }: { data: Preference }) {
    const [rows, setRows] = useState(() =>
        (data.effective_recipients.length
            ? data.effective_recipients
            : [""]
        ).map((value, id) => ({
            id,
            value,
        })),
    );
    const { prefix, focusRow } = useRowFocus(rows);
    const count = rows.filter((row) => row.value.trim()).length;
    return (
        <fieldset className="reminder-recipients">
            <legend>Email recipients</legend>
            {data.recipient_limit_exceeded && (
                <Notice>
                    Sending is paused. Reduce the plant's saved recipients to{" "}
                    {data.recipient_limit} to resume.
                </Notice>
            )}
            {rows.map((row, index) => (
                <div className="recipient-row" key={row.id}>
                    <label className="field" htmlFor={`${prefix}-${row.id}`}>
                        Email address {index + 1}
                        <input
                            id={`${prefix}-${row.id}`}
                            name="recipients"
                            type="email"
                            value={row.value}
                            onChange={(event) =>
                                setRows(
                                    rows.map((item) =>
                                        item.id === row.id
                                            ? {
                                                  ...item,
                                                  value: event.target.value,
                                              }
                                            : item,
                                    ),
                                )
                            }
                        />
                    </label>
                    {rows.length > 1 && (
                        <button
                            className="button secondary"
                            type="button"
                            aria-label={`Remove email address ${index + 1}`}
                            onClick={() => {
                                focusRow(
                                    (rows[index + 1] ?? rows[index - 1]).id,
                                );
                                setRows(
                                    rows.filter((item) => item.id !== row.id),
                                );
                            }}
                        >
                            Remove
                        </button>
                    )}
                </div>
            ))}
            <button
                className="button secondary"
                type="button"
                disabled={rows.length >= data.recipient_limit}
                onClick={() => {
                    const id = Math.max(-1, ...rows.map((row) => row.id)) + 1;
                    focusRow(id);
                    setRows([
                        ...rows,
                        {
                            id,
                            value: "",
                        },
                    ]);
                }}
            >
                Add address
            </button>
            <span className="fine">
                {" "}
                {count} / {data.recipient_limit}
            </span>
        </fieldset>
    );
}
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
                        <h2>Email reminders</h2>
                    </div>
                    <Tag>
                        {data.enabled && !data.recipient_limit_exceeded
                            ? "Enabled"
                            : "Paused"}
                    </Tag>
                </div>
                <ActionForm
                    key={JSON.stringify(data)}
                    submit="Save reminders"
                    onSubmit={async (form) => {
                        const times = form
                            .getAll("times")
                            .map((time) => String(time).trim())
                            .filter(Boolean);
                        if (
                            times.some(
                                (time) =>
                                    !/^([01]\d|2[0-3]):[0-5]\d$/.test(time),
                            )
                        )
                            throw new Error("Enter a valid delivery time.");
                        const enteredRecipients = form
                            .getAll("recipients")
                            .map((value) => String(value).trim())
                            .filter(Boolean);
                        const recipients =
                            !data.recipients.length &&
                            enteredRecipients.length === 1 &&
                            enteredRecipients[0] ===
                                data.effective_recipients[0]
                                ? []
                                : enteredRecipients;
                        const enabled = form.get("enabled") === "on";
                        if (enabled && !times.length)
                            throw new Error("Add at least one delivery time.");
                        if (
                            enabled &&
                            recipients.length > data.recipient_limit &&
                            !(
                                data.recipient_limit_exceeded &&
                                recipients.length < data.recipients.length
                            )
                        )
                            throw new Error(
                                `Use at most ${data.recipient_limit} email addresses.`,
                            );
                        if (
                            new Set(
                                recipients.map((email) => email.toLowerCase()),
                            ).size !== recipients.length
                        )
                            throw new Error(
                                "Enter each email address only once.",
                            );
                        await request(
                            `${base}reminders/`,
                            "PUT",
                            {
                                enabled,
                                recipients,
                                times: [...new Set(times)].sort(
                                    (a, b) =>
                                        Number(a.replace(":", "")) -
                                        Number(b.replace(":", "")),
                                ),
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
                        Enable forecast emails for this plant
                    </label>
                    {region.status !== "approved" && (
                        <Notice>
                            Reminders can be enabled once this plant is
                            approved.
                        </Notice>
                    )}
                    <DeliveryTimes data={data} timezone={region.timezone} />
                    <RecipientFields data={data} />
                    <details>
                        <summary>Reminder details</summary>
                        <p className="fine">
                            Emails use the latest saved forecast and show its
                            collection time and age; they do not request new
                            provider data. Daylight-saving gaps are skipped and
                            repeated local times run once. Daily limit:{" "}
                            {data.quota?.daily_reminder_limit ?? "?"}. Provider
                            queries per email:{" "}
                            {data.quota?.queries_per_email ?? "?"}.
                        </p>
                    </details>
                </ActionForm>
            </section>
            <details className="panel">
                <summary>Recent deliveries</summary>
                {data.deliveries.length ? (
                    <ScrollRegion label="Recent forecast email deliveries">
                        <table>
                            <caption className="sr-only">
                                Recent forecast email deliveries
                            </caption>
                            <thead>
                                <tr>
                                    <th>Scheduled ({region.timezone})</th>
                                    <th>Recipient</th>
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
                                        <td>{row.recipient}</td>
                                        <td>
                                            <Tag>
                                                {deliveryLabels[row.status] ??
                                                    row.status}
                                            </Tag>
                                            {row.forecast_status && (
                                                <div className="fine">
                                                    {forecastLabels[
                                                        row.forecast_status
                                                    ] ?? "Forecast unavailable"}
                                                </div>
                                            )}
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
                                            {row.last_error
                                                ? (deliveryDetails[
                                                      row.last_error
                                                  ] ??
                                                  "Delivery needs attention.")
                                                : "—"}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </ScrollRegion>
                ) : (
                    <Empty title="No deliveries yet">
                        Scheduled deliveries appear here.
                    </Empty>
                )}
            </details>
        </>
    );
}
