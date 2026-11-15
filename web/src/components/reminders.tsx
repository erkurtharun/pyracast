"use client";
import { useTranslations, useLocale, useFormatter } from "next-intl";
import { ScrollRegion } from "./scroll-region";
import { useEffect, useId, useRef, useState } from "react";
import type { components } from "@/lib/schema";
import { request, type Region } from "@/lib/api";
import { ActionForm, Empty, Notice, Tag, useResource } from "./ui";
type ReminderSchedule = components["schemas"]["ReminderOutput"];
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
    data: ReminderSchedule;
    timezone: string;
}) {
    const t = useTranslations("Reminders");

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
            <legend>{t("deliveryTimes", { timezone })}</legend>
            {rows.map((row, index) => (
                <div className="recipient-row" key={row.id}>
                    <label className="field" htmlFor={`${prefix}-${row.id}`}>
                        {t("deliveryTime", { index: index + 1 })}
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
                            aria-label={t("removeDeliveryTime", {
                                index: index + 1,
                            })}
                            onClick={() => {
                                focusRow(
                                    (rows[index + 1] ?? rows[index - 1]).id,
                                );
                                setRows(
                                    rows.filter((item) => item.id !== row.id),
                                );
                            }}
                        >
                            {t("remove")}
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
                {t("addTime")}
            </button>
            {limit != null && (
                <span className="fine">
                    {" "}
                    {t("quotaCount", {
                        count: rows.filter((row) => row.value).length,
                        limit,
                    })}
                </span>
            )}
        </fieldset>
    );
}
function RecipientFields({ data }: { data: ReminderSchedule }) {
    const t = useTranslations("Reminders");

    const [rows, setRows] = useState(() =>
        (data.recipients.length ? data.recipients : [""]).map((value, id) => ({
            id,
            value,
        })),
    );
    const { prefix, focusRow } = useRowFocus(rows);
    const count = rows.filter((row) => row.value.trim()).length;
    return (
        <fieldset className="reminder-recipients">
            <legend>{t("emailRecipients")}</legend>
            {data.recipient_limit_exceeded && (
                <Notice>
                    {t("recipientLimitExceeded", {
                        limit: data.recipient_limit,
                    })}
                </Notice>
            )}
            {rows.map((row, index) => (
                <div className="recipient-row" key={row.id}>
                    <label className="field" htmlFor={`${prefix}-${row.id}`}>
                        {t("emailAddress", { index: index + 1 })}
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
                            aria-label={t("removeEmailAddress", {
                                index: index + 1,
                            })}
                            onClick={() => {
                                focusRow(
                                    (rows[index + 1] ?? rows[index - 1]).id,
                                );
                                setRows(
                                    rows.filter((item) => item.id !== row.id),
                                );
                            }}
                        >
                            {t("remove")}
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
                {t("addAddress")}
            </button>
            <span className="fine">
                {" "}
                {t("quotaCount", { count, limit: data.recipient_limit })}
            </span>
        </fieldset>
    );
}
type ReminderProps = {
    base: string;
    region: Region;
    token: string;
};

export function Reminders(props: ReminderProps) {
    const [version, setVersion] = useState(0);
    return (
        <ReminderSettings
            key={`${props.base}-${version}`}
            {...props}
            onSaved={() => setVersion((current) => current + 1)}
        />
    );
}

function ReminderSettings({
    base,
    region,
    token,
    onSaved,
}: ReminderProps & { onSaved: () => void }) {
    const t = useTranslations("Reminders");
    const locale = useLocale();
    const format = useFormatter();
    const languageId = useId();
    const deliveryLabels: Record<string, string> = {
        pending: t("preparing"),
        retry: t("retryScheduled"),
        sending: t("sending"),
        sent: t("acceptedByMailService"),
        failed: t("notSent"),
        uncertain: t("acceptanceUnconfirmed"),
        skipped: t("skipped"),
        cancelled: t("cancelled"),
        unavailable: t("forecastUnavailable"),
    };
    const forecastLabels: Record<string, string> = {
        current: t("newForecast"),
        saved_complete: t("savedForecast"),
        saved_partial: t("partialForecast"),
        unavailable: t("noForecastAvailable"),
    };
    const deliveryDetails: Record<string, string> = {
        mail_not_sent: t("mailNotSent"),
        mail_rejected: t("mailRejected"),
        mail_authentication_failed: t("mailAuthenticationFailed"),
        interrupted_connection: t("interruptedConnection"),
        authority_or_deadline_changed: t("authorityChanged"),
        mail_outcome_uncertain: t("outcomeUncertain"),
        interrupted_send: t("interruptedSend"),
        missed_schedule: t("missedSchedule"),
        mail_deadline_exceeded: t("deadlineExceeded"),
        preference_or_authority_changed: t("preferenceChanged"),
        schedule_or_authority_changed: t("preferenceChanged"),
        missing_recipient: t("missingRecipient"),
        no_saved_forecast: t("noSavedForecast"),
    };

    const resource = useResource<ReminderSchedule>(`${base}reminders/`);
    if (resource.loading)
        return <p role="status">{t("loadingEmailReminders")}</p>;
    if (resource.error) return <Notice error>{resource.error}</Notice>;
    const data = resource.data!;
    return (
        <>
            <section className="panel">
                <div className="section-heading">
                    <div>
                        <h2>{t("emailReminders")}</h2>
                    </div>
                    <Tag tone="neutral">
                        {data.enabled && !data.recipient_limit_exceeded
                            ? t("enabled")
                            : t("paused")}
                    </Tag>
                </div>
                <ActionForm
                    key={JSON.stringify(data)}
                    submit={t("saveReminders")}
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
                            throw new Error(t("invalidDeliveryTime"));
                        const recipients = form
                            .getAll("recipients")
                            .map((value) => String(value).trim())
                            .filter(Boolean);
                        const enabled = form.get("enabled") === "on";
                        if (enabled && !times.length)
                            throw new Error(t("deliveryTimeRequired"));
                        if (
                            enabled &&
                            recipients.length > data.recipient_limit &&
                            !(
                                data.recipient_limit_exceeded &&
                                recipients.length < data.recipients.length
                            )
                        )
                            throw new Error(
                                t("recipientLimit", {
                                    limit: data.recipient_limit,
                                }),
                            );
                        if (
                            new Set(
                                recipients.map((email) => email.toLowerCase()),
                            ).size !== recipients.length
                        )
                            throw new Error(t("duplicateEmail"));
                        await request(
                            `${base}reminders/`,
                            "PUT",
                            {
                                enabled,
                                recipients,
                                language: form.get("language"),
                                times: [...new Set(times)].sort(
                                    (a, b) =>
                                        Number(a.replace(":", "")) -
                                        Number(b.replace(":", "")),
                                ),
                            },
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
                        {t("enableForecastEmailsForThisPlant")}
                    </label>
                    {region.status !== "approved" && (
                        <Notice>{t("approvalRequired")}</Notice>
                    )}
                    <DeliveryTimes data={data} timezone={region.timezone} />
                    <RecipientFields data={data} />
                    <div className="field">
                        <label htmlFor={languageId}>{t("emailLanguage")}</label>
                        <select
                            id={languageId}
                            name="language"
                            defaultValue={data.language}
                        >
                            <option value="en">English</option>
                            <option value="tr">Türkçe</option>
                        </select>
                    </div>
                    <details>
                        <summary>{t("reminderDetails")}</summary>
                        <p className="fine">
                            {t("reminderPolicy", {
                                limit:
                                    data.quota?.daily_reminder_limit == null
                                        ? "?"
                                        : format.number(
                                              data.quota.daily_reminder_limit,
                                          ),
                                queries:
                                    data.quota?.queries_per_email == null
                                        ? "?"
                                        : format.number(
                                              data.quota.queries_per_email,
                                          ),
                            })}
                        </p>
                    </details>
                </ActionForm>
            </section>
            <details className="panel">
                <summary>{t("recentDeliveries")}</summary>
                {data.deliveries.length ? (
                    <ScrollRegion label={t("recentForecastEmailDeliveries")}>
                        <table>
                            <caption className="sr-only">
                                {t("recentForecastEmailDeliveries")}
                            </caption>
                            <thead>
                                <tr>
                                    <th>
                                        {t("scheduled", {
                                            timezone: region.timezone,
                                        })}
                                    </th>
                                    <th>{t("recipient")}</th>
                                    <th>{t("status")}</th>
                                    <th>{t("collected")}</th>
                                    <th>{t("dataAge")}</th>
                                    <th>{t("sent")}</th>
                                    <th>{t("details")}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {data.deliveries.map((row) => (
                                    <tr key={row.id}>
                                        <td>
                                            {new Date(
                                                row.scheduled_for,
                                            ).toLocaleString(locale, {
                                                timeZone: region.timezone,
                                            })}
                                        </td>
                                        <td>{row.recipient}</td>
                                        <td>
                                            <Tag
                                                tone={
                                                    row.status === "failed"
                                                        ? "danger"
                                                        : row.status === "sent"
                                                          ? "success"
                                                          : [
                                                                  "pending",
                                                                  "retry",
                                                                  "uncertain",
                                                              ].includes(
                                                                  row.status,
                                                              )
                                                            ? "warning"
                                                            : "neutral"
                                                }
                                            >
                                                {deliveryLabels[row.status] ??
                                                    t("statusUnavailable")}
                                            </Tag>
                                            {row.forecast_status && (
                                                <div className="fine">
                                                    {forecastLabels[
                                                        row.forecast_status
                                                    ] ??
                                                        t(
                                                            "forecastUnavailable",
                                                        )}
                                                </div>
                                            )}
                                        </td>
                                        <td>
                                            {row.collected_at
                                                ? new Date(
                                                      row.collected_at,
                                                  ).toLocaleString(locale, {
                                                      timeZone: region.timezone,
                                                  })
                                                : "—"}
                                        </td>
                                        <td>
                                            {row.source_age_seconds == null
                                                ? "—"
                                                : t("sourceAge", {
                                                      hours: Math.floor(
                                                          row.source_age_seconds /
                                                              3600,
                                                      ),
                                                      minutes: Math.floor(
                                                          (row.source_age_seconds %
                                                              3600) /
                                                              60,
                                                      ),
                                                  })}
                                            {row.stale && <Tag>stale</Tag>}
                                        </td>
                                        <td>
                                            {row.sent_at
                                                ? new Date(
                                                      row.sent_at,
                                                  ).toLocaleString(locale, {
                                                      timeZone: region.timezone,
                                                  })
                                                : "—"}
                                        </td>
                                        <td>
                                            {row.late ? t("lateDelivery") : ""}
                                            {row.last_error
                                                ? (deliveryDetails[
                                                      row.last_error
                                                  ] ??
                                                  t("deliveryNeedsAttention"))
                                                : "—"}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </ScrollRegion>
                ) : (
                    <Empty title={t("noDeliveriesYet")}>
                        {t("scheduledDeliveriesAppearHere")}
                    </Empty>
                )}
            </details>
        </>
    );
}
