"use client";
import { useTranslations } from "next-intl";
import { ScrollRegion } from "@/ui/scroll-region";
import dynamic from "next/dynamic";
import {
    Fragment,
    createContext,
    useContext,
    useEffect,
    useEffectEvent,
    useId,
    useRef,
    useState,
    type RefObject,
} from "react";
import type { components } from "@/lib/schema";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { MISSING } from "@/lib/format/number";
import { useFormat } from "@/lib/format/use-format";
import { isLanguage } from "@/i18n/language";
import { useApiQuery, useRefresh, type Loaded } from "@/lib/api/query";
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
    committedDraft,
    initialDraft,
    sameAddressing,
    type AddedRow,
    type Addresses,
    type Answer,
    type Draft,
    type MemberRow,
} from "./reminder-draft";
import { ReminderOutcome } from "./reminder-line";
import { useRowFocus } from "./row-focus";
type ReminderBody = components["schemas"]["ReminderInput"];
type Delivery = Answer["deliveries"][number];
const PATH = "/api/tenants/{tenant_id}/regions/{region_id}/reminders/";
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
    hold: string | undefined;
}>({ line: null, hold: undefined });
function RecipientsLoading() {
    const t = useTranslations("Reminders");
    const { line, hold } = useContext(Loading);
    return (
        <>
            <p
                className="fine"
                tabIndex={-1}
                ref={line}
                aria-describedby={hold}
            >
                {t("loadingEmailReminders")}
            </p>
            {hold && (
                <p className="fine" id={hold} role="status">
                    {t("saveWaitsForCheck")}
                </p>
            )}
        </>
    );
}

export type RecipientState = NonNullable<
    ReturnType<typeof useRecipientSection>
>;
export type Outcome =
    | { status: "answered"; answer: Answer }
    | { status: "refused"; error: ApiError }
    | { status: "failed" };
type Settings = Pick<ReminderBody, "enabled" | "times" | "language">;
export type Section = {
    base: Answer;
    addresses: Addresses;
    takenAt: number;
    draft: Draft;
    known: Record<number, { text: string; added: AddedRow }>;
    members: MemberRow[];
    check: (Outcome & { generation: number }) | null;
    attempt: (Settings & { generation: number }) | null;
    saved: { revision: number; settled: boolean } | null;
};
type FormState = {
    generation: number;
    mount: number;
    section: Section | null;
};
const EMPTY: FormState = {
    generation: 0,
    mount: 0,
    section: null,
};

function fresh(
    form: FormState,
    answer: Answer,
    addresses: Addresses,
    takenAt: number,
): FormState {
    return {
        generation: form.generation + 1,
        mount: form.mount + 1,
        section: {
            base: answer,
            addresses,
            takenAt,
            draft: initialDraft(answer, addresses),
            known: {},
            members: addresses.members,
            check: null,
            attempt: null,
            saved: null,
        },
    };
}

const unsavedIn = ({ base, addresses, draft }: Section) =>
    !sameAddressing(draft, initialDraft(base, addresses));

function adopted(
    form: FormState,
    answer: Answer,
    takenAt: number,
    canManage: boolean,
): FormState {
    const { section } = form;
    const addresses = canManage ? answer.addresses : null;
    if (!addresses)
        return section
            ? { ...form, generation: form.generation + 1, section: null }
            : form;
    if (!section) return fresh(form, answer, addresses, takenAt);
    if (takenAt === section.takenAt) return form;
    if (section.saved)
        return answer.revision >= section.saved.revision
            ? fresh(form, answer, addresses, takenAt)
            : form;
    if (answer.revision !== section.base.revision) return form;
    const next = {
        ...section,
        base: answer,
        addresses,
        takenAt,
        members: addresses.members,
        check: null,
    };
    return {
        ...form,
        generation: form.generation + 1,
        section: unsavedIn(next) ? next : { ...next, known: {} },
    };
}

function useRecipientSection(
    answer: Answer,
    takenAt: number,
    canManage: boolean,
) {
    const [form, setForm] = useState(() =>
        adopted(EMPTY, answer, takenAt, canManage),
    );
    const current = adopted(form, answer, takenAt, canManage);
    if (current !== form) setForm(current);
    const { generation, section } = current;
    if (!section) return null;
    const check =
        section.check?.generation === generation ? section.check : null;
    const newer = !section.saved && answer.revision > section.base.revision;
    const change = (
        apply: (section: Section, generation: number) => Section,
        advance = true,
    ) =>
        setForm((form) => {
            if (!form.section) return form;
            const generation = form.generation + Number(advance);
            const next = apply(form.section, generation);
            return next === form.section
                ? form
                : { ...form, generation, section: next };
        });
    return {
        section,
        check,
        generation,
        base: section.base,
        addresses: section.addresses,
        draft: section.draft,
        unsaved: !section.saved && unsavedIn(section),
        newer,
        answered:
            !newer &&
            !section.saved &&
            check?.status === "answered" &&
            check.answer.addresses
                ? check.answer
                : null,
        reload:
            newer ||
            section.saved?.settled === true ||
            (check?.status === "refused" &&
                check.error.code === "reminder_schedule_changed"),
        mount: current.mount,
        change,
        hold: (settings: Settings) =>
            change((section, generation) => ({
                ...section,
                draft: committedDraft(section.draft),
                attempt: { ...settings, generation },
                check: null,
            })),
        refused: (error: ApiError) =>
            change(
                (section, generation) => ({
                    ...section,
                    check: { generation, status: "refused", error },
                }),
                false,
            ),
        saved: (revision: number) =>
            change((section) => ({
                ...section,
                saved: { revision, settled: false },
                check: null,
            })),
        settled: (revision: number) =>
            change(
                (section) =>
                    section.saved?.revision === revision
                        ? { ...section, saved: { revision, settled: true } }
                        : section,
                false,
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

function settingsOf(form: FormData): Settings {
    const language = formText(form, "language");
    return {
        enabled: form.get("enabled") === "on",
        language: isLanguage(language) ? language : "en",
        times: entered(form, "times"),
    };
}

function ReminderForm({
    answer,
    takenAt,
    region,
    scope,
    reload,
}: {
    answer: Answer;
    takenAt: number;
    region: Region;
    scope: RegionScope;
    reload: () => Promise<void>;
}) {
    const t = useTranslations("Reminders");
    const format = useFormat();
    const refresh = useRefresh();
    const recipients = useRecipientSection(
        answer,
        takenAt,
        useCanManageWorkspace(),
    );
    const [held, setHeld] = useState(false);
    const heldId = useId();
    const heading = useRef<HTMLHeadingElement>(null);
    const loading = useRef<HTMLParagraphElement>(null);
    const base = recipients?.base ?? answer;
    const hold = held ? heldId : undefined;
    return (
        <div
            onSubmitCapture={(event) => {
                if (!recipients || !(event.target instanceof HTMLFormElement))
                    return;
                const draft = committedDraft(recipients.draft);
                const ready =
                    draft.hide ||
                    sameAddressing(
                        draft,
                        initialDraft(recipients.base, recipients.addresses),
                    ) ||
                    (draft === recipients.draft &&
                        heading.current !== null &&
                        recipients.answered !== null);
                if (ready) {
                    setHeld(false);
                    return;
                }
                event.preventDefault();
                event.stopPropagation();
                recipients.hold(settingsOf(new FormData(event.target)));
                setHeld(true);
                (heading.current ?? loading.current)?.focus();
            }}
        >
            {recipients?.reload && (
                <AsyncButton variant="secondary" onClick={reload}>
                    {t("reloadReminders")}
                </AsyncButton>
            )}
            <ActionForm
                submit={t("saveReminders")}
                permitted={recipients !== null}
                onSubmit={async (values) => {
                    if (!recipients) return;
                    let revision: number;
                    try {
                        ({ revision } = await api.put(PATH, {
                            path: scope,
                            body: {
                                ...settingsOf(values),
                                expected_revision: base.revision,
                                ...addressingOf(
                                    committedDraft(recipients.draft),
                                ),
                            },
                        }));
                    } catch (error) {
                        if (
                            error instanceof ApiError &&
                            error.code === "reminder_schedule_changed"
                        )
                            recipients.refused(error);
                        throw error;
                    }
                    recipients.saved(revision);
                    await refresh.workspace(scope.tenant_id);
                    recipients.settled(revision);
                }}
            >
                <Fragment key={recipients?.mount ?? 0}>
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
                        <Loading.Provider value={{ line: loading, hold }}>
                            <RecipientSection
                                recipients={recipients}
                                scope={scope}
                                heading={heading}
                                hold={hold}
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
    const addresses = useCanManageWorkspace();
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

function useRevocation(resource: Loaded<Answer>, canManage: boolean) {
    const countOnly = resource.data?.addresses === null;
    const revoke = useEffectEvent((keepShown: boolean) =>
        resource.forget({ keepShown }),
    );
    const managed = useRef(canManage);
    useEffect(() => {
        const lost = managed.current && !canManage;
        managed.current = canManage;
        if (lost || countOnly) revoke(!lost);
    }, [canManage, countOnly, resource.updatedAt]);
}

function ReminderSettings({ scope, region }: ReminderProps) {
    const t = useTranslations("Reminders");
    const headingId = useId();
    const resource = useApiQuery(PATH, { path: scope });
    const canManage = useCanManageWorkspace();
    const [generation, setGeneration] = useState(0);
    useRevocation(resource, canManage);
    const reload = async () => {
        try {
            await resource.reload();
        } catch {
            // RefreshFailed reports it and the offer stays.
            return;
        }
        setGeneration((current) => current + 1);
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
                        <ReminderForm
                            key={generation}
                            answer={answer}
                            takenAt={resource.updatedAt}
                            region={region}
                            scope={scope}
                            reload={reload}
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
