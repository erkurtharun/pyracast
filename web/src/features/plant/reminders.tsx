"use client";
import { useTranslations } from "next-intl";
import { ScrollRegion } from "@/ui/scroll-region";
import dynamic from "next/dynamic";
import {
    useEffect,
    useEffectEvent,
    useId,
    useRef,
    useState,
    type RefObject,
} from "react";
import { flushSync } from "react-dom";
import type { Region, RegionScope } from "@/lib/api";
import { MISSING } from "@/lib/format/number";
import { useFormat } from "@/lib/format/use-format";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import { ActionForm } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { contactEmail } from "@/lib/public-metadata";
import { Notice } from "@/ui/ui";
import { StatusTag, useLabel } from "@/ui/vocabulary";
import { useCanManageWorkspace } from "@/ui/workspace-access";
import { sectionUnavailable } from "@/lib/on-demand";
import {
    EMPTY,
    edited,
    type Answer,
    type Desk,
    type Outcome,
    type Panel,
} from "./reminder-draft";
import { ReminderFields, rowsOf, type Change } from "./reminder-fields";
import { ReminderOutcome } from "./reminder-line";
import styles from "./reminders.module.css";
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

const START: Panel = { draft: EMPTY, outcome: "none", dropped: [] };

const READ_ONLY: Change = {
    enabled: () => undefined,
    times: () => undefined,
    language: () => undefined,
};

function RecipientCount({ answer }: { answer: Answer }) {
    const t = useTranslations("Reminders");
    return (
        <p className="recipient-count">
            {t("recipientCountOnly", {
                count: answer.recipient_count,
                limit: answer.recipient_limit,
            })}
        </p>
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
                                    <td>
                                        {time(row.collected_at)}
                                        {row.stale && (
                                            <StatusTag
                                                group="Freshness"
                                                value="stale"
                                            />
                                        )}
                                    </td>
                                    <td>{time(row.sent_at)}</td>
                                    <td>
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
                <p>{t("noDeliveriesYet")}</p>
            )}
        </details>
    );
}

function OutcomeLine({
    outcome,
    kept,
    line,
}: {
    outcome: Outcome;
    kept: boolean;
    line: RefObject<HTMLParagraphElement | null>;
}) {
    const t = useTranslations("Reminders");
    const common = useTranslations("Common");
    if (outcome === "none") return null;
    return (
        <p
            className={`${styles.outcome} ${styles.focusable}`}
            tabIndex={-1}
            ref={line}
        >
            {outcome === "saved" ? common("saved") : t(`outcomes.${outcome}`)}
            {kept &&
                outcome !== "saved" &&
                outcome !== "lost" &&
                ` ${t("editsKept", { outcome })}`}
        </p>
    );
}

function ReminderSettings({ scope, region }: ReminderProps) {
    const t = useTranslations("Reminders");
    const headingId = useId();
    const resource = useApiQuery(PATH, { path: scope });
    const refresh = useRefresh();
    const canManage = useCanManageWorkspace();
    const [panel, setPanel] = useState(START);
    const heading = useRef<HTMLHeadingElement>(null);
    const line = useRef<HTMLParagraphElement>(null);
    const focused = useRef<EventTarget | null>(null);
    const answer = resource.data;
    const carried = !!answer?.addresses;
    const access = !canManage || resource.denied ? false : answer && carried;
    const was = useRef<boolean | undefined>(undefined);
    const lose = useEffectEvent(() => {
        setPanel((now) => ({ ...now, draft: EMPTY, dropped: [] }));
        resource.discard();
    });
    useEffect(() => {
        if (access) was.current = true;
        else if (
            access === false &&
            (was.current || (was.current === undefined && carried))
        ) {
            lose();
            was.current = false;
        }
    }, [access, carried]);
    useEffect(() => {
        if (
            focused.current instanceof Node &&
            !focused.current.isConnected &&
            document.activeElement === document.body
        )
            heading.current?.focus();
    });
    const desk: Desk = {
        panel,
        change: setPanel,
        finish: ({ outcome, path }, sent) => {
            resource.discard();
            let own = false;
            flushSync(() =>
                setPanel((now) => {
                    // A new object: React skips an identical state, and the discarded read must leave in this commit.
                    if (now.draft !== sent) return { ...now };
                    own = true;
                    focused.current = null;
                    return {
                        draft:
                            path === "conflict" || path === "unknown"
                                ? { ...now.draft, base: undefined }
                                : EMPTY,
                        outcome,
                        dropped: [],
                    };
                }),
            );
            if (own) line.current?.focus();
            if (path === "refusal") {
                if (own) was.current = false;
                void refresh.tenants();
            }
            if (path === "saved")
                void refresh.workspace(scope.tenant_id, {
                    path: PATH,
                    options: { path: scope },
                });
        },
    };
    return (
        <div
            className={styles.panel}
            onFocus={(event) => {
                focused.current = event.target;
            }}
        >
            <section className="panel" aria-labelledby={headingId}>
                <div className="section-heading">
                    <div>
                        <h2
                            id={headingId}
                            className={styles.focusable}
                            tabIndex={-1}
                            ref={heading}
                        >
                            {t("emailReminders")}
                        </h2>
                    </div>
                    {answer && (
                        <StatusTag group="ReminderState" value={answer.state} />
                    )}
                </div>
                <div aria-live="polite">
                    <OutcomeLine
                        outcome={panel.outcome}
                        kept={edited(panel.draft)}
                        line={line}
                    />
                    {access &&
                        panel.dropped.map(({ address, role }, index) => (
                            <p key={index} className="fine">
                                {t("choiceRemoved", {
                                    address,
                                    role: role
                                        ? t(`roles.${role}`)
                                        : t("defaultRole"),
                                })}
                            </p>
                        ))}
                </div>
                <Resource
                    resource={resource}
                    loading={t("loadingEmailReminders")}
                >
                    {(answer) => (
                        <ReminderPanel
                            answer={answer}
                            desk={desk}
                            scope={scope}
                            region={region}
                        />
                    )}
                </Resource>
            </section>
            {answer && (
                <DeliveryHistory
                    deliveries={answer.deliveries}
                    timezone={region.timezone}
                    addresses={access === true}
                />
            )}
        </div>
    );
}

function ReminderPanel({
    answer,
    desk,
    scope,
    region,
}: ReminderProps & { answer: Answer; desk: Desk }) {
    const t = useTranslations("Reminders");
    const addresses = useCanManageWorkspace() ? answer.addresses : null;
    return (
        <>
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
            {answer.state === "provider_attention" && (
                <Notice>
                    <p>
                        <strong>{t("emailPaused")}</strong>
                    </p>
                    <p>{t("noNewForecastData", { contact: contactEmail })}</p>
                </Notice>
            )}
            {addresses ? (
                <ReminderEditor
                    answer={answer}
                    addresses={addresses}
                    desk={desk}
                    scope={scope}
                    region={region}
                />
            ) : (
                <ReminderView answer={answer} region={region} />
            )}
        </>
    );
}
