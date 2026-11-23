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
    useLayoutEffect,
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
import { sectionUnavailable } from "@/lib/on-demand";
import {
    addressingOf,
    committedDraft,
    filled,
    initialDraft,
    sameAddressing,
    type AddedRow,
    type Addresses,
    type Answer,
    type Draft,
    type TypedRow,
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
type Known = Record<number, { text: string; added: AddedRow }>;
export type Outcome =
    | { status: "answered"; answer: Answer }
    | { status: "refused"; error: ApiError }
    | { status: "failed" };
type Section = {
    base: Answer;
    takenAt: number;
    draft: Draft;
    known: Known;
    check: (Outcome & { generation: number }) | null;
};
type FormState = { generation: number; mount: number; section: Section | null };

function knownOf(rows: TypedRow[], addresses: Addresses, known: Known = {}) {
    const learnt = { ...known };
    filled(rows).forEach((row, index) => {
        const added = addresses.added[index];
        if (added) learnt[row.id] = { text: row.committed.trim(), added };
    });
    return learnt;
}

function fresh(answer: Answer, takenAt: number): Section | null {
    if (!answer.addresses) return null;
    const draft = initialDraft(answer, answer.addresses);
    return {
        base: answer,
        takenAt,
        draft,
        known: knownOf(draft.rows, answer.addresses),
        check: null,
    };
}

const unsavedIn = ({ base, draft }: Section) =>
    !base.addresses ||
    !sameAddressing(draft, initialDraft(base, base.addresses));

function adopted(form: FormState, answer: Answer, takenAt: number) {
    const { section } = form;
    if (!answer.addresses)
        return section
            ? { ...form, generation: form.generation + 1, section: null }
            : form;
    if (!section)
        return {
            generation: form.generation + 1,
            mount: form.mount + 1,
            section: fresh(answer, takenAt),
        };
    if (
        takenAt === section.takenAt ||
        answer.revision !== section.base.revision
    )
        return form;
    const next = { ...section, base: answer, takenAt, check: null };
    return {
        ...form,
        generation: form.generation + 1,
        section: unsavedIn(next)
            ? next
            : { ...next, known: knownOf(next.draft.rows, answer.addresses) },
    };
}

function useRecipientSection(
    answer: Answer,
    takenAt: number,
    onStale: () => void,
) {
    const [form, setForm] = useState<FormState>(() => ({
        generation: 0,
        mount: 0,
        section: fresh(answer, takenAt),
    }));
    const current = adopted(form, answer, takenAt);
    if (current !== form) setForm(current);
    const { generation, section } = current;
    const unsaved = section !== null && unsavedIn(section);
    const check =
        section?.check?.generation === generation ? section.check : null;
    const present = !unsaved
        ? (section?.base ?? null)
        : check?.status === "answered" && check.answer.addresses
          ? check.answer
          : null;
    const changed =
        (check?.status === "refused" &&
            check.error.code === "reminder_schedule_changed") ||
        (section !== null && answer.revision > section.base.revision);
    useEffect(() => {
        if (changed) onStale();
    }, [changed, onStale]);
    if (!section?.base.addresses) return null;
    return {
        base: section.base,
        addresses: section.base.addresses,
        draft: section.draft,
        known: section.known,
        generation,
        unsaved,
        present,
        check,
        mount: current.mount,
        update: (change: (draft: Draft) => Draft, decided = true) =>
            setForm((form) => {
                if (!form.section) return form;
                const draft = change(form.section.draft);
                return draft === form.section.draft
                    ? form
                    : {
                          ...form,
                          generation: form.generation + Number(decided),
                          section: { ...form.section, draft },
                      };
            }),
        record: (asked: number, outcome: Outcome | null) =>
            setForm((form) =>
                form.generation === asked && form.section
                    ? {
                          ...form,
                          section: {
                              ...form.section,
                              check: outcome && {
                                  generation: asked,
                                  ...outcome,
                              },
                              known:
                                  outcome?.status === "answered" &&
                                  outcome.answer.addresses
                                      ? knownOf(
                                            form.section.draft.rows,
                                            outcome.answer.addresses,
                                            form.section.known,
                                        )
                                      : form.section.known,
                          },
                      }
                    : form,
            ),
        accept: (saved: Answer, takenAt: number) =>
            setForm((form) => ({
                generation: form.generation + 1,
                mount: form.mount + 1,
                section: fresh(saved, takenAt),
            })),
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
    takenAt,
    region,
    scope,
    save,
    onStale,
}: {
    answer: Answer;
    takenAt: number;
    region: Region;
    scope: RegionScope;
    save: (body: ReminderBody) => Promise<Answer>;
    onStale: () => void;
}) {
    const t = useTranslations("Reminders");
    const format = useFormat();
    const canManage = useCanManageWorkspace();
    const refresh = useRefresh();
    const recipients = useRecipientSection(answer, takenAt, onStale);
    const [held, setHeld] = useState(false);
    const heldId = useId();
    const heading = useRef<HTMLHeadingElement>(null);
    const loading = useRef<HTMLParagraphElement>(null);
    const latest = useRef({ answer, takenAt });
    useLayoutEffect(() => {
        latest.current = { answer, takenAt };
    });
    const base = recipients?.base ?? answer;
    const hold = held ? heldId : undefined;
    return (
        <div
            onSubmitCapture={(event) => {
                if (!recipients || !(event.target instanceof HTMLFormElement))
                    return;
                const draft = committedDraft(recipients.draft);
                if (draft !== recipients.draft)
                    recipients.update((now) => committedDraft(now));
                const ready =
                    draft.hide ||
                    sameAddressing(
                        draft,
                        initialDraft(recipients.base, recipients.addresses),
                    ) ||
                    (draft === recipients.draft &&
                        heading.current !== null &&
                        recipients.present !== null);
                if (ready) {
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
                    const saved = await save({
                        enabled: values.get("enabled") === "on",
                        language: isLanguage(language) ? language : "en",
                        times: entered(values, "times"),
                        expected_revision: base.revision,
                        ...addressingOf(committedDraft(recipients.draft)),
                    });
                    if (latest.current.answer.addresses)
                        recipients.accept(saved, latest.current.takenAt);
                    await refresh.workspace(scope.tenant_id);
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
    const headingId = useId();
    const resource = useApiQuery(PATH, { path: scope });
    const [form, setForm] = useState({ generation: 0, stale: false });
    const onStale = useCallback(
        () => setForm((current) => ({ ...current, stale: true })),
        [],
    );
    const save = async (body: ReminderBody) => {
        try {
            const saved = await api.put(PATH, { path: scope, body });
            setForm((current) => ({ ...current, stale: false }));
            return saved;
        } catch (error) {
            if (
                error instanceof ApiError &&
                error.code === "reminder_schedule_changed"
            )
                onStale();
            throw error;
        }
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
                            takenAt={resource.updatedAt}
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
