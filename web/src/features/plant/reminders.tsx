"use client";
import { useTranslations } from "next-intl";
import { ScrollRegion } from "@/ui/scroll-region";
import { Fragment, useId, useState } from "react";
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
import { addressingFields, type ReminderAnswer } from "./reminder-addressing";
import {
    RecipientCount,
    RecipientSection,
    useRecipientSection,
    useRowFocus,
} from "./reminder-recipients";
type ReminderSchedule = components["schemas"]["ReminderOutput"];
type ReminderBody = components["schemas"]["ReminderInput"];
type Delivery = ReminderSchedule["deliveries"][number];
type Outcome = Pick<Delivery, "scheduled_for" | "status" | "last_error">;
export type ReminderSummary = components["schemas"]["ReminderSummaryOutput"];
const PATH = "/api/tenants/{tenant_id}/regions/{region_id}/reminders/";
// The generated schema predates the amended contract, whose answer names `memberships` on each added address.
const answerOf = (data: ReminderSchedule) => data as unknown as ReminderAnswer;
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
function DeliveryTimes({ data }: { data: ReminderAnswer }) {
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

function ReminderForm({
    answer,
    region,
    save,
}: {
    answer: ReminderAnswer;
    region: Region;
    save: (body: ReminderBody) => Promise<void>;
}) {
    const t = useTranslations("Reminders");
    const format = useFormat();
    const recipients = useRecipientSection(answer);
    return (
        <ActionForm
            submit={t("saveReminders")}
            onSubmit={async (values) => {
                const language = formText(values, "language");
                await save({
                    enabled: values.get("enabled") === "on",
                    recipients: entered(values, "recipients"),
                    include_workspace_members:
                        values.get("include_workspace_members") === "on",
                    language: isLanguage(language) ? language : "en",
                    times: entered(values, "times"),
                    expected_revision: answer.revision,
                    ...(recipients &&
                        addressingFields(
                            recipients.section,
                            recipients.addresses,
                        )),
                });
            }}
        >
            <Fragment key={JSON.stringify(answer)}>
                <CheckboxField
                    label={t("enableForecastEmailsForThisPlant")}
                    name="enabled"
                    defaultChecked={answer.enabled}
                />
                {region.status !== "approved" && (
                    <Notice>{t("approvalRequired")}</Notice>
                )}
                <DeliveryTimes data={answer} />
                {recipients ? (
                    <RecipientSection answer={answer} recipients={recipients} />
                ) : (
                    <RecipientCount answer={answer} />
                )}
                <Select
                    label={t("emailLanguage")}
                    name="language"
                    defaultValue={answer.language}
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
                                answer.daily_reminder_limit == null
                                    ? "?"
                                    : format.number(
                                          answer.daily_reminder_limit,
                                      ),
                        })}
                    </p>
                </details>
            </Fragment>
        </ActionForm>
    );
}

function DeliveryRecipients({ delivery }: { delivery: Delivery }) {
    const t = useTranslations("Reminders");
    if (delivery.recipient_roles)
        return delivery.recipient_roles.map(({ address, role }) => (
            <div key={address}>
                <span>{address}</span>{" "}
                <span className="field-hint">{t(`roles.${role}`)}</span>
            </div>
        ));
    if (delivery.recipients)
        return delivery.recipients.map((email) => (
            <div key={email}>{email}</div>
        ));
    return t("deliveryRecipientCount", { count: delivery.recipient_count });
}

function DeliveryHistory({
    deliveries,
    timezone,
}: {
    deliveries: Delivery[];
    timezone: string;
}) {
    const t = useTranslations("Reminders");
    const format = useFormat();
    const label = useLabel();
    const time = (value: string | null | undefined) =>
        format.time(value, timezone);
    return (
        <details className="panel">
            <summary>{t("recentDeliveries")}</summary>
            {deliveries.length ? (
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
                            {deliveries.map((row) => (
                                <tr key={row.id}>
                                    <td>{time(row.scheduled_for)}</td>
                                    <td className="delivery-recipients">
                                        <DeliveryRecipients delivery={row} />
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
    );
}

function ReminderSettings({ scope, region }: ReminderProps) {
    const t = useTranslations("Reminders");
    const refresh = useRefresh();
    const headingId = useId();
    const resource = useApiQuery(PATH, { path: scope });
    const [form, setForm] = useState({ generation: 0, stale: false });
    const save = async (body: ReminderBody) => {
        try {
            resource.replace(await api.put(PATH, { path: scope, body }));
        } catch (error) {
            if (
                error instanceof ApiError &&
                error.code === "reminder_schedule_changed"
            )
                setForm((current) => ({ ...current, stale: true }));
            throw error;
        }
        setForm((current) => ({ ...current, stale: false }));
        await refresh.workspace(scope.tenant_id);
    };
    return (
        <Resource resource={resource} loading={t("loadingEmailReminders")}>
            {(data) => {
                const answer = answerOf(data);
                return (
                    <>
                        <section className="panel" aria-labelledby={headingId}>
                            <div className="section-heading">
                                <div>
                                    <h2 id={headingId}>
                                        {t("emailReminders")}
                                    </h2>
                                </div>
                                <StatusTag
                                    group="ReminderState"
                                    value={answer.state}
                                />
                            </div>
                            <p className="fine" role="status">
                                {answer.deliveries[0] ? (
                                    <ReminderOutcome
                                        outcome={answer.deliveries[0]}
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
                                        try {
                                            await resource.reload();
                                        } catch {
                                            // RefreshFailed reports it and the offer stays.
                                            return;
                                        }
                                        setForm((current) => ({
                                            generation: current.generation + 1,
                                            stale: false,
                                        }));
                                    }}
                                >
                                    {t("reloadReminders")}
                                </AsyncButton>
                            )}
                            <ReminderForm
                                key={form.generation}
                                answer={answer}
                                region={region}
                                save={save}
                            />
                        </section>
                        <DeliveryHistory
                            deliveries={answer.deliveries}
                            timezone={region.timezone}
                        />
                    </>
                );
            }}
        </Resource>
    );
}
