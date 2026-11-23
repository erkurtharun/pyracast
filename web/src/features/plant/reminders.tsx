"use client";
import { useTranslations } from "next-intl";
import { ScrollRegion } from "@/ui/scroll-region";
import dynamic from "next/dynamic";
import { useEffect, useEffectEvent, useId, useRef } from "react";
import type { Region, RegionScope } from "@/lib/api";
import { MISSING } from "@/lib/format/number";
import { useFormat } from "@/lib/format/use-format";
import { useApiQuery } from "@/lib/api/query";
import { ActionForm } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { Empty } from "@/ui/ui";
import { StatusTag, useLabel } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { sectionUnavailable } from "@/lib/on-demand";
import type { Answer } from "./reminder-draft";
import { ReminderFields, rowsOf, type Change } from "./reminder-fields";
import { ReminderOutcome } from "./reminder-line";
type Delivery = Answer["deliveries"][number];
const PATH = "/api/tenants/{tenant_id}/regions/{region_id}/reminders/";
const ReminderEditor = dynamic(
    () =>
        import("./reminder-recipients").then(
            (module) => module.ReminderEditor,
            sectionUnavailable,
        ),
    { loading: EditorLoading },
);
function EditorLoading() {
    const t = useTranslations("Reminders");
    return <p className="fine">{t("loadingEmailReminders")}</p>;
}

const READ_ONLY: Change = {
    enabled: () => undefined,
    times: () => undefined,
    language: () => undefined,
};

function RecipientCount({ answer }: { answer: Answer }) {
    const t = useTranslations("Reminders");
    return (
        <>
            <p className="recipient-count">
                {t("recipientCountOnly", {
                    count: answer.recipient_count,
                    limit: answer.recipient_limit,
                })}
            </p>
            <p className="fine">
                {t(
                    answer.hide_recipients
                        ? "recipientsHidden"
                        : "recipientsVisible",
                )}
            </p>
        </>
    );
}

function ReminderView({ answer, region }: { answer: Answer; region: Region }) {
    const t = useTranslations("Reminders");
    return (
        <ActionForm
            submit={t("saveReminders")}
            permitted={false}
            onSubmit={() => Promise.resolve()}
        >
            <ReminderFields
                answer={answer}
                region={region}
                fields={{
                    enabled: answer.enabled,
                    times: rowsOf(answer.times),
                    language: answer.language,
                }}
                change={READ_ONLY}
            >
                <RecipientCount answer={answer} />
            </ReminderFields>
        </ActionForm>
    );
}

type ReminderProps = {
    scope: RegionScope;
    region: Region;
};

export function Reminders(props: ReminderProps) {
    return <ReminderSettings key={props.scope.region_id} {...props} />;
}

function DeliveryRecipients({
    delivery,
    addresses,
}: {
    delivery: Delivery;
    addresses: boolean;
}) {
    const t = useTranslations("Reminders");
    if (addresses && delivery.recipient_roles)
        return delivery.recipient_roles.map(({ address, role }, index) => (
            <div key={index}>
                <span>{address}</span>{" "}
                <span className="field-hint">{t(`roles.${role}`)}</span>
            </div>
        ));
    if (addresses && delivery.recipients)
        return delivery.recipients.map((email, index) => (
            <div key={index}>{email}</div>
        ));
    return t("deliveryRecipientCount", { count: delivery.recipient_count });
}

function DeliveryHistory({
    deliveries,
    timezone,
    addresses,
}: {
    deliveries: Delivery[];
    timezone: string;
    addresses: boolean;
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
                                        <DeliveryRecipients
                                            delivery={row}
                                            addresses={addresses}
                                        />
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
    const resource = useApiQuery(PATH, { path: scope });
    return (
        <Resource resource={resource} loading={t("loadingEmailReminders")}>
            {(answer) => (
                <ReminderPanel
                    answer={answer}
                    reload={resource.reload}
                    scope={scope}
                    region={region}
                />
            )}
        </Resource>
    );
}

function ReminderPanel({
    answer,
    reload,
    scope,
    region,
}: ReminderProps & { answer: Answer; reload: () => Promise<Answer> }) {
    const t = useTranslations("Reminders");
    const headingId = useId();
    const addresses = useCanManageWorkspace() ? answer.addresses : null;
    const managing = addresses !== null;
    const lose = useEffectEvent(() => {
        // RefreshFailed reports a failure.
        reload().catch(() => undefined);
    });
    const was = useRef(managing);
    useEffect(() => {
        if (was.current && !managing) lose();
        was.current = managing;
    }, [managing]);
    return (
        <>
            <section className="panel" aria-labelledby={headingId}>
                <div className="section-heading">
                    <div>
                        <h2 id={headingId}>{t("emailReminders")}</h2>
                    </div>
                    <StatusTag group="ReminderState" value={answer.state} />
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
                {addresses ? (
                    <ReminderEditor
                        answer={answer}
                        addresses={addresses}
                        reload={reload}
                        scope={scope}
                        region={region}
                    />
                ) : (
                    <ReminderView answer={answer} region={region} />
                )}
            </section>
            <DeliveryHistory
                deliveries={answer.deliveries}
                timezone={region.timezone}
                addresses={managing}
            />
        </>
    );
}
