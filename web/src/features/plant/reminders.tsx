"use client";
import { useTranslations } from "next-intl";
import { ScrollRegion } from "@/ui/scroll-region";
import { useEffect, useId, useRef, useState } from "react";
import type { components } from "@/lib/schema";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { DisplayError } from "@/lib/api/errors";
import { MISSING } from "@/lib/format/number";
import { useFormat } from "@/lib/format/use-format";
import { isLanguage } from "@/i18n/language";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import { ActionForm, CheckboxField, Select } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { Empty, Notice } from "@/ui/ui";
import { StatusTag, useLabel } from "@/ui/vocabulary";
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
function DeliveryTimes({ data }: { data: ReminderSchedule }) {
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
            <legend>{t("deliveryTimes")}</legend>
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
            <CheckboxField
                label={t("includeWorkspaceMembers")}
                name="include_workspace_members"
                checked={includeMembers}
                onChange={setIncludeMembers}
            />
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
    const format = useFormat();
    const label = useLabel();
    const time = (value: string | null | undefined) =>
        format.time(value, region.timezone);

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
    return (
        <Resource resource={resource} loading={t("loadingEmailReminders")}>
            {(data) => (
                <>
                    <section className="panel">
                        <div className="section-heading">
                            <div>
                                <h2>{t("emailReminders")}</h2>
                            </div>
                            <StatusTag
                                group="ReminderState"
                                value={data.state}
                            />
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
                                            !/^([01]\d|2[0-3]):[0-5]\d$/.test(
                                                time,
                                            ),
                                    )
                                )
                                    throw new DisplayError(
                                        t("invalidDeliveryTime"),
                                    );
                                const recipients = form
                                    .getAll("recipients")
                                    .map((value) => String(value).trim())
                                    .filter(Boolean);
                                const enabled = form.get("enabled") === "on";
                                const includeWorkspaceMembers =
                                    form.get("include_workspace_members") ===
                                    "on";
                                const recipientCount = new Set(
                                    [
                                        ...recipients,
                                        ...(includeWorkspaceMembers
                                            ? (data.addresses
                                                  ?.workspace_recipients ?? [])
                                            : []),
                                    ].map((email) => email.toLowerCase()),
                                ).size;
                                if (enabled && !times.length)
                                    throw new DisplayError(
                                        t("deliveryTimeRequired"),
                                    );
                                if (
                                    enabled &&
                                    recipientCount > data.recipient_limit &&
                                    !(
                                        data.recipient_limit_exceeded &&
                                        recipientCount < data.recipient_count
                                    )
                                )
                                    throw new DisplayError(
                                        t("recipientLimit", {
                                            limit: data.recipient_limit,
                                        }),
                                    );
                                if (
                                    new Set(
                                        recipients.map((email) =>
                                            email.toLowerCase(),
                                        ),
                                    ).size !== recipients.length
                                )
                                    throw new DisplayError(t("duplicateEmail"));
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
                            <CheckboxField
                                label={t("enableForecastEmailsForThisPlant")}
                                name="enabled"
                                defaultChecked={data.enabled}
                            />
                            {region.status !== "approved" && (
                                <Notice>{t("approvalRequired")}</Notice>
                            )}
                            <DeliveryTimes data={data} />
                            <RecipientFields data={data} />
                            <Select
                                label={t("emailLanguage")}
                                name="language"
                                defaultValue={data.language}
                                required
                            >
                                <option value="en">English</option>
                                <option value="tr">Türkçe</option>
                            </Select>
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
                            <ScrollRegion
                                label={t("recentForecastEmailDeliveries")}
                            >
                                <table>
                                    <caption className="sr-only">
                                        {t("recentForecastEmailDeliveries")}
                                    </caption>
                                    <thead>
                                        <tr>
                                            <th>{t("scheduled")}</th>
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
                                                    {time(row.scheduled_for)}
                                                </td>
                                                <td className="delivery-recipients">
                                                    {row.recipients
                                                        ? row.recipients.map(
                                                              (email) => (
                                                                  <div
                                                                      key={
                                                                          email
                                                                      }
                                                                  >
                                                                      {email}
                                                                  </div>
                                                              ),
                                                          )
                                                        : t(
                                                              "deliveryRecipientCount",
                                                              {
                                                                  count: row.recipient_count,
                                                              },
                                                          )}
                                                </td>
                                                <td>
                                                    <StatusTag
                                                        group="NotificationDeliveryStatus"
                                                        value={row.status}
                                                    />
                                                    {row.forecast_status && (
                                                        <div className="fine">
                                                            {label(
                                                                "PreparedMessageKind",
                                                                row.forecast_status,
                                                            )}
                                                        </div>
                                                    )}
                                                </td>
                                                <td>
                                                    {time(row.collected_at)}
                                                </td>
                                                <td>
                                                    {row.source_age_seconds ==
                                                    null
                                                        ? MISSING
                                                        : t("sourceAge", {
                                                              hours: Math.floor(
                                                                  row.source_age_seconds /
                                                                      3600,
                                                              ),
                                                              minutes:
                                                                  Math.floor(
                                                                      (row.source_age_seconds %
                                                                          3600) /
                                                                          60,
                                                                  ),
                                                          })}
                                                    {row.stale && (
                                                        <StatusTag
                                                            group="Freshness"
                                                            value="stale"
                                                        />
                                                    )}
                                                </td>
                                                <td>{time(row.sent_at)}</td>
                                                <td>
                                                    {row.late
                                                        ? t("lateDelivery")
                                                        : ""}
                                                    {row.last_error
                                                        ? label(
                                                              "NotificationDeliveryError",
                                                              row.last_error,
                                                          )
                                                        : MISSING}
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
            )}
        </Resource>
    );
}
