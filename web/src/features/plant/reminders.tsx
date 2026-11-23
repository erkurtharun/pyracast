"use client";
import { useTranslations } from "next-intl";
import { ScrollRegion } from "@/ui/scroll-region";
import dynamic from "next/dynamic";
import {
    Fragment,
    createContext,
    useCallback,
    useContext,
    useEffect,
    useId,
    useRef,
    useState,
    type ReactNode,
    type RefObject,
} from "react";
import type { components } from "@/lib/schema";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { MISSING } from "@/lib/format/number";
import { useFormat } from "@/lib/format/use-format";
import { isLanguage } from "@/i18n/language";
import { useApiAnswer, useApiQuery, useRefresh } from "@/lib/api/query";
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
import { sectionUnavailable } from "@/lib/on-demand";
import {
    addressingOf,
    initialDraft,
    sameAddressing,
    withTexts,
    type Answer,
    type Draft,
} from "./reminder-draft";
import { ReminderOutcome } from "./reminder-line";
import { useRowFocus } from "./row-focus";
type ReminderBody = components["schemas"]["ReminderInput"];
type Delivery = Answer["deliveries"][number];
const PATH = "/api/tenants/{tenant_id}/regions/{region_id}/reminders/";
const PREVIEW =
    "/api/tenants/{tenant_id}/regions/{region_id}/reminders/preview/";
const RecipientSection = dynamic(
    () =>
        import("./reminder-recipients").then(
            (module) => module.RecipientSection,
            sectionUnavailable,
        ),
    { loading: RecipientsLoading },
);
const Loading = createContext<{
    line: RefObject<HTMLParagraphElement | null> | null;
    held: ReactNode;
}>({ line: null, held: null });
function RecipientsLoading() {
    const t = useTranslations("Reminders");
    const { line, held } = useContext(Loading);
    return (
        <>
            <p className="fine" tabIndex={-1} ref={line}>
                {t("loadingEmailReminders")}
            </p>
            {held}
        </>
    );
}

export type RecipientState = NonNullable<
    ReturnType<typeof useRecipientSection>
>;
type Section = { base: Answer; draft: Draft; generation: number };

const fresh = (answer: Answer, generation: number): Section | null =>
    answer.addresses
        ? {
              base: answer,
              draft: initialDraft(answer, answer.addresses),
              generation,
          }
        : null;
const unsavedIn = ({ base, draft }: Section) =>
    !base.addresses ||
    !sameAddressing(draft, initialDraft(base, base.addresses));
const NOTHING: Draft = {
    hide: true,
    include: false,
    rows: [],
    members: {},
    typed: {},
};

const adopt = (state: Section | null, answer: Answer) =>
    state && answer.revision === state.base.revision
        ? { ...state, base: answer }
        : state && answer.addresses && unsavedIn(state)
          ? state
          : fresh(answer, (state?.generation ?? 0) + 1);

function useRecipientSection(
    answer: Answer,
    scope: RegionScope,
    onStale: () => void,
) {
    const [state, setState] = useState(() => fresh(answer, 0));
    const current = adopt(state, answer);
    const unsaved = current !== null && unsavedIn(current);
    const base = current?.base ?? answer;
    const preview = useApiAnswer(
        PREVIEW,
        {
            path: scope,
            body: {
                enabled: base.enabled,
                times: base.times,
                language: base.language,
                expected_revision: base.revision,
                ...addressingOf(current?.draft ?? NOTHING),
            },
        },
        unsaved,
    );
    const changed =
        unsaved &&
        preview.error instanceof ApiError &&
        preview.error.code === "reminder_schedule_changed";
    useEffect(() => {
        if (changed) onStale();
    }, [changed, onStale]);
    if (!current?.base.addresses || !answer.addresses) return null;
    return {
        base: current.base,
        addresses: current.base.addresses,
        draft: current.draft,
        unsaved,
        preview,
        shown: !unsaved || preview.current,
        generation: current.generation,
        update: (change: (draft: Draft) => Draft) =>
            setState((previous) => {
                const now = adopt(previous, answer);
                return now && { ...now, draft: change(now.draft) };
            }),
        accept: (saved: Answer) =>
            setState((previous) =>
                fresh(saved, (previous?.generation ?? 0) + 1),
            ),
    };
}
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
function DeliveryTimes({ data }: { data: Answer }) {
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
    scope,
    save,
    onStale,
}: {
    answer: Answer;
    region: Region;
    scope: RegionScope;
    save: (body: ReminderBody) => Promise<Answer>;
    onStale: () => void;
}) {
    const t = useTranslations("Reminders");
    const format = useFormat();
    const canManage = useCanManageWorkspace();
    const recipients = useRecipientSection(answer, scope, onStale);
    const [held, setHeld] = useState(false);
    const heading = useRef<HTMLHeadingElement>(null);
    const loading = useRef<HTMLParagraphElement>(null);
    const base = recipients?.base ?? answer;
    const heldLine = held && <p className="fine">{t("checkBeforeStarting")}</p>;
    return (
        <div
            onSubmitCapture={(event) => {
                if (!recipients || !(event.target instanceof HTMLFormElement))
                    return;
                const values = new FormData(event.target);
                const draft = withTexts(
                    recipients.draft,
                    formTexts(values, "recipients"),
                );
                if (draft !== recipients.draft)
                    recipients.update((now) =>
                        withTexts(now, formTexts(values, "recipients")),
                    );
                const shown =
                    draft === recipients.draft &&
                    heading.current !== null &&
                    recipients.shown;
                if (values.get("enabled") !== "on" || draft.hide || shown) {
                    setHeld(false);
                    return;
                }
                event.preventDefault();
                event.stopPropagation();
                setHeld(true);
                (heading.current ?? loading.current)?.focus();
            }}
        >
            <ActionForm
                submit={t("saveReminders")}
                permitted={canManage && answer.addresses !== null}
                onSubmit={async (values) => {
                    if (!recipients) return;
                    const language = formText(values, "language");
                    recipients.accept(
                        await save({
                            enabled: values.get("enabled") === "on",
                            language: isLanguage(language) ? language : "en",
                            times: entered(values, "times"),
                            expected_revision: base.revision,
                            ...addressingOf(
                                withTexts(
                                    recipients.draft,
                                    formTexts(values, "recipients"),
                                ),
                            ),
                        }),
                    );
                }}
            >
                <Fragment
                    key={`${base.revision}-${recipients?.generation ?? 0}`}
                >
                    <CheckboxField
                        label={t("enableForecastEmailsForThisPlant")}
                        name="enabled"
                        defaultChecked={base.enabled}
                    />
                    {region.status !== "approved" && (
                        <Notice>{t("approvalRequired")}</Notice>
                    )}
                    <DeliveryTimes data={base} />
                    {recipients ? (
                        <Loading.Provider
                            value={{ line: loading, held: heldLine }}
                        >
                            <RecipientSection
                                recipients={recipients}
                                heading={heading}
                                held={heldLine}
                            />
                        </Loading.Provider>
                    ) : (
                        <RecipientCount answer={answer} />
                    )}
                    <Select
                        label={t("emailLanguage")}
                        name="language"
                        defaultValue={base.language}
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
                                    base.daily_reminder_limit == null
                                        ? "?"
                                        : format.number(
                                              base.daily_reminder_limit,
                                          ),
                            })}
                        </p>
                    </details>
                </Fragment>
            </ActionForm>
        </div>
    );
}

function DeliveryRecipients({ delivery }: { delivery: Delivery }) {
    const t = useTranslations("Reminders");
    if (delivery.recipient_roles)
        return delivery.recipient_roles.map(({ address, role }, index) => (
            <div key={index}>
                <span>{address}</span>{" "}
                <span className="field-hint">{t(`roles.${role}`)}</span>
            </div>
        ));
    if (delivery.recipients)
        return delivery.recipients.map((email, index) => (
            <div key={index}>{email}</div>
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
    const onStale = useCallback(
        () => setForm((current) => ({ ...current, stale: true })),
        [],
    );
    const save = async (body: ReminderBody) => {
        let saved: Answer;
        try {
            saved = await api.put(PATH, { path: scope, body });
            resource.replace(saved);
        } catch (error) {
            if (
                error instanceof ApiError &&
                error.code === "reminder_schedule_changed"
            )
                onStale();
            throw error;
        }
        setForm((current) => ({ ...current, stale: false }));
        await refresh.workspace(scope.tenant_id);
        return saved;
    };
    return (
        <Resource resource={resource} loading={t("loadingEmailReminders")}>
            {(answer) => (
                <>
                    <section className="panel" aria-labelledby={headingId}>
                        <div className="section-heading">
                            <div>
                                <h2 id={headingId}>{t("emailReminders")}</h2>
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
                            scope={scope}
                            save={save}
                            onStale={onStale}
                        />
                    </section>
                    <DeliveryHistory
                        deliveries={answer.deliveries}
                        timezone={region.timezone}
                    />
                </>
            )}
        </Resource>
    );
}
