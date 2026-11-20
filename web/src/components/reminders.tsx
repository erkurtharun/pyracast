"use client";
import { useTranslations, useLocale, useFormatter } from "next-intl";
import { ScrollRegion } from "./scroll-region";
import { useEffect, useId, useRef, useState } from "react";
import type { components } from "@/lib/schema";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { isLanguage } from "@/i18n/language";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import { ActionForm, Empty, Notice, RefreshFailed, Tag } from "./ui";
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
    const limit = data.daily_reminder_limit;
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
                                const neighbour =
                                    rows[index + 1] ?? rows[index - 1];
                                if (neighbour) focusRow(neighbour.id);
                                setRows(
                                    rows.filter((item) => item.id !== row.id),
                                );
                            }}
                        >
                            <span aria-hidden="true">&times;</span>
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
    if (!data.addresses)
        return (
            <p className="recipient-count">
                {t("recipientCountOnly", {
                    count: data.recipient_count,
                    limit: data.recipient_limit,
                })}
            </p>
        );
    return <RecipientRows data={data} addresses={data.addresses} />;
}
function RecipientRows({
    data,
    addresses,
}: {
    data: ReminderSchedule;
    addresses: NonNullable<ReminderSchedule["addresses"]>;
}) {
    const t = useTranslations("Reminders");
    const [rows, setRows] = useState(() =>
        (addresses.recipients.length ? addresses.recipients : [""]).map(
            (value, id) => ({
                id,
                value,
            }),
        ),
    );
    const { prefix, focusRow } = useRowFocus(rows);
    const [includeMembers, setIncludeMembers] = useState(
        data.include_workspace_members,
    );
    const count = new Set(
        [
            ...rows.map((row) => row.value.trim()).filter(Boolean),
            ...(includeMembers ? addresses.workspace_recipients : []),
        ].map((email) => email.toLowerCase()),
    ).size;
    return (
        <fieldset className="reminder-recipients">
            <legend>{t("emailRecipients")}</legend>
            <span className="recipient-count fine" aria-live="polite">
                {t("quotaCount", { count, limit: data.recipient_limit })}
            </span>
            <p className="fine">{t("groupPrivacy")}</p>
            {count > data.recipient_limit && (
                <Notice>
                    {t(
                        data.recipient_limit_exceeded
                            ? "recipientLimitExceeded"
                            : "recipientLimit",
                        {
                            limit: data.recipient_limit,
                        },
                    )}
                </Notice>
            )}
            <label className="check-field">
                <input
                    type="checkbox"
                    name="include_workspace_members"
                    checked={includeMembers}
                    onChange={(event) =>
                        setIncludeMembers(event.target.checked)
                    }
                />
                {t("includeWorkspaceMembers")}
            </label>
            <p className="fine">{t("membershipSync")}</p>
            <details className="reminder-members">
                <summary>
                    {t("workspaceMembers", {
                        count: addresses.workspace_recipients.length,
                    })}
                </summary>
                {addresses.workspace_recipients.length ? (
                    <ul>
                        {addresses.workspace_recipients.map((email) => (
                            <li key={email}>{email}</li>
                        ))}
                    </ul>
                ) : (
                    <p className="fine">{t("noWorkspaceMembers")}</p>
                )}
            </details>
            <p className="recipient-label">{t("otherAddresses")}</p>
            {rows.map((row, index) => (
                <div className="recipient-row" key={row.id}>
                    <label className="field" htmlFor={`${prefix}-${row.id}`}>
                        {t("emailAddress", { index: index + 1 })}
                        <input
                            id={`${prefix}-${row.id}`}
                            name="recipients"
                            type="email"
                            autoCapitalize="none"
                            autoCorrect="off"
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
                                const neighbour =
                                    rows[index + 1] ?? rows[index - 1];
                                if (neighbour) focusRow(neighbour.id);
                                setRows(
                                    rows.filter((item) => item.id !== row.id),
                                );
                            }}
                        >
                            <span aria-hidden="true">&times;</span>
                        </button>
                    )}
                </div>
            ))}
            <button
                className="button secondary"
                type="button"
                disabled={
                    count >= data.recipient_limit ||
                    rows.length >= data.recipient_limit
                }
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
        </fieldset>
    );
}
type ReminderProps = {
    scope: RegionScope;
    region: Region;
};

export function Reminders(props: ReminderProps) {
    return <ReminderSettings key={props.scope.region_id} {...props} />;
}

function ReminderSettings({ scope, region }: ReminderProps) {
    const t = useTranslations("Reminders");
    const refresh = useRefresh();
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
    };
    const stateLabels: Record<ReminderSchedule["state"], string> = {
        sending: t("enabled"),
        paused_by_user: t("paused"),
        waiting_for_approval: t("stateWaitingForApproval"),
        provider_attention: t("stateProviderAttention"),
        no_recipients: t("noRecipientsPaused"),
        invalid_recipient: t("invalidRecipientPaused"),
        over_recipient_limit: t("recipientLimitPaused"),
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
        mail_credentials_rejected: t("mailCredentialsRejected"),
        mail_authorization_revoked: t("mailAuthorizationRevoked"),
        mail_sender_forbidden: t("mailSenderForbidden"),
        mail_configuration_invalid: t("mailConfigurationInvalid"),
        mail_rate_limited: t("mailRateLimited"),
        mail_preparation_failed: t("mailPreparationFailed"),
        interrupted_connection: t("interruptedConnection"),
        authority_or_deadline_changed: t("authorityChanged"),
        mail_outcome_uncertain: t("outcomeUncertain"),
        interrupted_send: t("interruptedSend"),
        missed_schedule: t("missedSchedule"),
        mail_deadline_exceeded: t("deadlineExceeded"),
        preference_or_authority_changed: t("preferenceChanged"),
        schedule_or_authority_changed: t("preferenceChanged"),
        cancelled_by_settings_change: t("cancelledBySettingsChange"),
        recipient_limit_exceeded: t("recipientLimitPaused"),
        no_recipients: t("noRecipientsPaused"),
        invalid_recipient: t("invalidRecipientPaused"),
        no_saved_forecast: t("noSavedForecast"),
    };

    const resource = useApiQuery(
        "/api/tenants/{tenant_id}/regions/{region_id}/reminders/",
        { path: scope },
    );
    // A save resets the editor only once a later read has succeeded; a failed read keeps what is on screen.
    const [editor, setEditor] = useState<{
        edition: number;
        savedAt?: number;
    }>({ edition: 0 });
    if (editor.savedAt !== undefined && resource.updatedAt > editor.savedAt)
        setEditor({ edition: editor.edition + 1 });
    const data = resource.data;
    if (data === undefined)
        return resource.error ? (
            <Notice error>{resource.error}</Notice>
        ) : (
            <p role="status">{t("loadingEmailReminders")}</p>
        );
    return (
        <>
            <RefreshFailed resource={resource} />
            <section className="panel">
                <div className="section-heading">
                    <div>
                        <h2>{t("emailReminders")}</h2>
                    </div>
                    <Tag
                        tone={data.state === "sending" ? "success" : "neutral"}
                    >
                        {stateLabels[data.state]}
                    </Tag>
                </div>
                <ActionForm
                    key={`${editor.edition}:${JSON.stringify(data)}`}
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
                        const includeWorkspaceMembers =
                            form.get("include_workspace_members") === "on";
                        const recipientCount = new Set(
                            [
                                ...recipients,
                                ...(includeWorkspaceMembers
                                    ? (data.addresses?.workspace_recipients ??
                                      [])
                                    : []),
                            ].map((email) => email.toLowerCase()),
                        ).size;
                        if (enabled && !times.length)
                            throw new Error(t("deliveryTimeRequired"));
                        if (
                            enabled &&
                            recipientCount > data.recipient_limit &&
                            !(
                                data.recipient_limit_exceeded &&
                                recipientCount < data.recipient_count
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
                        const language = String(form.get("language"));
                        await api.put(
                            "/api/tenants/{tenant_id}/regions/{region_id}/reminders/",
                            {
                                path: scope,
                                body: {
                                    enabled,
                                    recipients,
                                    include_workspace_members:
                                        includeWorkspaceMembers,
                                    language: isLanguage(language)
                                        ? language
                                        : "en",
                                    times: [...new Set(times)].sort(
                                        (a, b) =>
                                            Number(a.replace(":", "")) -
                                            Number(b.replace(":", "")),
                                    ),
                                },
                            },
                        );
                        setEditor((current) => ({
                            ...current,
                            savedAt: resource.updatedAt,
                        }));
                        await refresh.workspace(scope.tenant_id);
                    }}
                >
                    <label className="check-field">
                        <input
                            type="checkbox"
                            name="enabled"
                            defaultChecked={data.enabled}
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
                                    data.daily_reminder_limit == null
                                        ? "?"
                                        : format.number(
                                              data.daily_reminder_limit,
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
                                    <th>{t("emailRecipients")}</th>
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
                                        <td className="delivery-recipients">
                                            {row.recipients
                                                ? row.recipients.map(
                                                      (email) => (
                                                          <div key={email}>
                                                              {email}
                                                          </div>
                                                      ),
                                                  )
                                                : t("deliveryRecipientCount", {
                                                      count: row.recipient_count,
                                                  })}
                                        </td>
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
