"use client";
import { useTranslations } from "next-intl";
import { ScrollRegion } from "@/ui/scroll-region";
import { Fragment, useEffect, useId, useRef, useState } from "react";
import type { components } from "@/lib/schema";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { MISSING } from "@/lib/format/number";
import { useFormat } from "@/lib/format/use-format";
import { isLanguage } from "@/i18n/language";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import {
    ActionForm,
    AsyncButton,
    CheckboxField,
    Select,
    formText,
    formTexts,
} from "@/ui/form";
import { Resource } from "@/ui/resource";
import { Empty, Notice } from "@/ui/ui";
import { StatusTag, useLabel } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";
type ReminderSchedule = components["schemas"]["ReminderOutput"];
type Delivery = ReminderSchedule["deliveries"][number];
type Outcome = Pick<Delivery, "scheduled_for" | "status" | "last_error">;
export type ReminderSummary = components["schemas"]["ReminderSummaryOutput"];
const PATH = "/api/tenants/{tenant_id}/regions/{region_id}/reminders/";
function ReminderOutcome({
    outcome,
    timezone,
}: {
    outcome: Outcome;
    timezone: string;
}) {
    const t = useTranslations("Reminders");
    const format = useFormat();
    const label = useLabel();
    return (
        <>
            {t("lastEmail", {
                status: label("NotificationDeliveryStatus", outcome.status),
                scheduled: format.time(outcome.scheduled_for, timezone),
            })}
            {outcome.last_error &&
                ` ${label("NotificationDeliveryError", outcome.last_error)}`}
        </>
    );
}
export function ReminderLine({
    reminder,
    timezone,
}: {
    reminder: ReminderSummary;
    timezone: string;
}) {
    const t = useTranslations("Reminders");
    const label = useLabel();
    return (
        <p className="fine">
            {t("dailyEmail", { state: label("ReminderState", reminder.state) })}
            {reminder.last_delivery && (
                <>
                    {" · "}
                    <ReminderOutcome
                        outcome={reminder.last_delivery}
                        timezone={timezone}
                    />
                </>
            )}
        </p>
    );
}
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
    times,
    limit,
}: {
    times: string[];
    limit: number | null;
}) {
    const t = useTranslations("Reminders");

    const [rows, setRows] = useState(() =>
        (times.length ? times : [""]).map((value, id) => ({
            id,
            value,
        })),
    );
    const { prefix, focusRow } = useRowFocus(rows);
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
function RecipientFields({
    data,
    edited,
}: {
    data: ReminderSchedule;
    edited: Edited;
}) {
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
    return (
        <RecipientRows
            data={data}
            members={data.addresses.workspace_recipients}
            edited={edited}
        />
    );
}
function RecipientRows({
    data,
    members,
    edited,
}: {
    data: ReminderSchedule;
    members: string[];
    edited: Edited;
}) {
    const t = useTranslations("Reminders");
    const [rows, setRows] = useState(() =>
        (edited.addresses?.recipients.length
            ? edited.addresses.recipients
            : [""]
        ).map((value, id) => ({
            id,
            value,
        })),
    );
    const { prefix, focusRow } = useRowFocus(rows);
    const [includeMembers, setIncludeMembers] = useState(
        edited.include_workspace_members,
    );
    // A hint only: the server decides the limit and which addresses count.
    const count = new Set(
        [
            ...rows.map((row) => row.value.trim()).filter(Boolean),
            ...(includeMembers ? members : []),
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
                        count: members.length,
                    })}
                </summary>
                {members.length ? (
                    <ul>
                        {members.map((email) => (
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

function entered(form: FormData, name: string) {
    return formTexts(form, name)
        .map((value) => value.trim())
        .filter(Boolean);
}

type Edited = Pick<
    ReminderSchedule,
    "enabled" | "times" | "include_workspace_members" | "language" | "revision"
> & {
    addresses: Pick<
        NonNullable<ReminderSchedule["addresses"]>,
        "recipients"
    > | null;
};

function ReminderSettings({ scope, region }: ReminderProps) {
    const t = useTranslations("Reminders");
    const resource = useApiQuery(PATH, { path: scope });
    return (
        <Resource resource={resource} loading={t("loadingEmailReminders")}>
            {(data) => (
                <ReminderPanel
                    // Edits read without addresses would save an empty recipient list.
                    key={data.addresses ? "addresses" : "count"}
                    scope={scope}
                    region={region}
                    data={data}
                    reload={resource.reload}
                />
            )}
        </Resource>
    );
}

function ReminderPanel({
    scope,
    region,
    data,
    reload,
}: ReminderProps & {
    data: ReminderSchedule;
    reload: () => Promise<ReminderSchedule>;
}) {
    const t = useTranslations("Reminders");
    const refresh = useRefresh();
    const canManage = useCanManageWorkspace();
    const format = useFormat();
    const label = useLabel();
    const headingId = useId();
    const time = (value: string | null | undefined) =>
        format.time(value, region.timezone);
    const [form, setForm] = useState<{
        generation: number;
        saves: number;
        stale: boolean;
        edited: Edited;
    }>({ generation: 0, saves: 0, stale: false, edited: data });
    const { edited } = form;
    return (
        <>
            <section className="panel" aria-labelledby={headingId}>
                <div className="section-heading">
                    <div>
                        <h2 id={headingId}>{t("emailReminders")}</h2>
                    </div>
                    <StatusTag group="ReminderState" value={data.state} />
                </div>
                <p className="fine" role="status">
                    {data.deliveries[0] ? (
                        <ReminderOutcome
                            outcome={data.deliveries[0]}
                            timezone={region.timezone}
                        />
                    ) : (
                        t("noEmailYet")
                    )}
                </p>
                {form.stale && (
                    <AsyncButton
                        variant="secondary"
                        onClick={async () => {
                            let current: ReminderSchedule;
                            try {
                                current = await reload();
                            } catch {
                                // RefreshFailed reports it and the offer stays.
                                return;
                            }
                            setForm((shown) => ({
                                ...shown,
                                generation: shown.generation + 1,
                                stale: false,
                                edited: current,
                            }));
                        }}
                    >
                        {t("reloadReminders")}
                    </AsyncButton>
                )}
                <ActionForm
                    key={form.generation}
                    permitted={canManage && !!data.addresses}
                    submit={t("saveReminders")}
                    onSubmit={async (values) => {
                        const language = formText(values, "language");
                        let saved: ReminderSchedule;
                        try {
                            saved = await api.put(PATH, {
                                path: scope,
                                body: {
                                    enabled: values.get("enabled") === "on",
                                    recipients: entered(values, "recipients"),
                                    include_workspace_members:
                                        values.get(
                                            "include_workspace_members",
                                        ) === "on",
                                    language: isLanguage(language)
                                        ? language
                                        : "en",
                                    times: entered(values, "times"),
                                    expected_revision: edited.revision,
                                },
                            });
                        } catch (error) {
                            if (
                                error instanceof ApiError &&
                                error.code === "reminder_schedule_changed"
                            )
                                setForm((current) => ({
                                    ...current,
                                    stale: true,
                                }));
                            throw error;
                        }
                        setForm((current) => ({
                            ...current,
                            saves: current.saves + 1,
                            stale: false,
                            edited: saved,
                        }));
                        await refresh.workspace(scope.tenant_id);
                    }}
                >
                    <Fragment key={form.saves}>
                        <CheckboxField
                            label={t("enableForecastEmailsForThisPlant")}
                            name="enabled"
                            defaultChecked={edited.enabled}
                        />
                        {region.status !== "approved" && (
                            <Notice>{t("approvalRequired")}</Notice>
                        )}
                        <DeliveryTimes
                            times={edited.times}
                            limit={data.daily_reminder_limit}
                        />
                        <RecipientFields data={data} edited={edited} />
                        <Select
                            label={t("emailLanguage")}
                            name="language"
                            defaultValue={edited.language}
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
                    </Fragment>
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
                                        <td>{time(row.scheduled_for)}</td>
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
                                        <td>{time(row.collected_at)}</td>
                                        <td>
                                            {row.source_age_seconds == null
                                                ? MISSING
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
                                            {row.stale && (
                                                <StatusTag
                                                    group="Freshness"
                                                    value="stale"
                                                />
                                            )}
                                        </td>
                                        <td>{time(row.sent_at)}</td>
                                        <td>
                                            {row.late ? t("lateDelivery") : ""}
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
    );
}
