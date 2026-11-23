"use client";
import { useTranslations } from "next-intl";
import {
    useEffect,
    useEffectEvent,
    useId,
    useLayoutEffect,
    useRef,
    useState,
    type Dispatch,
    type RefObject,
    type SetStateAction,
} from "react";
import { flushSync } from "react-dom";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { useRefresh } from "@/lib/api/query";
import { ActionForm, CheckboxField, useErrorMessage } from "@/ui/form";
import { Notice } from "@/ui/ui";
import {
    DISCLOSURE_CHANGED,
    EMPTY,
    bodyOf,
    formOf,
    identity,
    type Addresses,
    type Answer,
    type Body,
    type Choice,
    type Confirmed,
    type Draft,
    type Form,
    type Previewed,
    type Role,
} from "./reminder-draft";
import { ReminderFields, type Row } from "./reminder-fields";
import styles from "./reminder-recipients.module.css";
import { useRowFocus } from "./row-focus";

const PATH = "/api/tenants/{tenant_id}/regions/{region_id}/reminders/";
const PREVIEW =
    "/api/tenants/{tenant_id}/regions/{region_id}/reminders/preview/";

type Review =
    | { status: "closed" | "previewing" | "saving" }
    | { status: "open" | "confirming"; body: Body; answer: Previewed }
    | { status: "failed"; body: Body; answer: Previewed; message: string };
type After = {
    cause: "saved" | "changed";
    status: "loading" | "failed";
    focus: boolean;
} | null;
type Outcome = "saved" | "cancelled";
type Decision = {
    resolve: (outcome: Outcome) => void;
    reject: (error: unknown) => void;
};
const CLOSED: Review = { status: "closed" };

const conflicted = (error: unknown) =>
    error instanceof ApiError &&
    (error.code === "reminder_schedule_changed" ||
        (error.body as { code?: unknown } | null)?.code === DISCLOSURE_CHANGED);

const OPTIONS = [null, "to", "cc", "bcc"] as const satisfies readonly Choice[];

type Listed = { address: string; outside: boolean };

function groupsOf(answer: Answer) {
    const { members, added } = answer.addresses ?? { members: [], added: [] };
    const seen = new Set<string>();
    const to: Listed[] = [];
    const cc: Listed[] = [];
    const place = (address: string, placement: Role, outside: boolean) => {
        const key = identity(address);
        if (placement === "bcc" || seen.has(key)) return;
        seen.add(key);
        (placement === "to" ? to : cc).push({ address, outside });
    };
    if (answer.include_workspace_members)
        for (const member of members)
            place(member.address, member.placement, false);
    for (const item of added)
        if (item.eligible)
            place(item.address, item.placement, !item.memberships.length);
    return {
        to,
        cc,
        hidden: Math.max(0, answer.recipient_count - to.length - cc.length),
    };
}

function Group({ label, items }: { label: string; items: Listed[] }) {
    const t = useTranslations("Reminders");
    if (!items.length) return null;
    return (
        <>
            <dt>{label}</dt>
            {items.map((item, index) => (
                <dd key={index}>
                    {item.address}
                    {item.outside && (
                        <span className={styles.outside}>
                            {t("outsideMark")}
                        </span>
                    )}
                </dd>
            ))}
        </>
    );
}

function Groups({ answer }: { answer: Answer }) {
    const t = useTranslations("Reminders");
    const { to, cc, hidden } = groupsOf(answer);
    return (
        <>
            {to.length + cc.length > 0 && (
                <dl className={styles.groups}>
                    <Group label={t("roles.to")} items={to} />
                    <Group label={t("copyGroup")} items={cc} />
                </dl>
            )}
            {!to.length && cc.length > 0 && (
                <p className={styles.warning}>{t("noToWarning")}</p>
            )}
            <p>{t("summaryHidden", { count: hidden })}</p>
        </>
    );
}

function SavedView({
    answer,
    addresses,
    after,
    retry,
    heading,
    line,
    status,
}: {
    answer: Answer;
    addresses: Addresses;
    after: After;
    retry: () => void;
    heading: RefObject<HTMLHeadingElement | null>;
    line: RefObject<HTMLParagraphElement | null>;
    status: RefObject<HTMLDivElement | null>;
}) {
    const t = useTranslations("Reminders");
    const common = useTranslations("Common");
    const count = answer.recipient_count;
    const visible = !answer.hide_recipients && count > 0;
    const shown = groupsOf(answer);
    return (
        <div className={styles.summary}>
            <h3 tabIndex={-1} ref={heading}>
                {t("whoSeesWhat")}
            </h3>
            <div aria-live="polite" ref={status}>
                {after && (
                    <div className={styles.line}>
                        <p tabIndex={-1} ref={line}>
                            {t(
                                after.cause === "saved"
                                    ? after.status === "loading"
                                        ? "savedLoading"
                                        : "savedFailed"
                                    : after.status === "loading"
                                      ? "readingAgain"
                                      : "readFailed",
                            )}
                        </p>
                        {after.status === "failed" && (
                            <button
                                type="button"
                                className="button secondary"
                                onClick={retry}
                            >
                                {common("retry")}
                            </button>
                        )}
                    </div>
                )}
            </div>
            {!after && (
                <>
                    <p className="recipient-count fine">
                        {t("quotaCount", {
                            count,
                            limit: answer.recipient_limit,
                        })}
                    </p>
                    {count > answer.recipient_limit && (
                        <Notice>
                            {t("recipientLimitExceeded", {
                                limit: answer.recipient_limit,
                            })}
                        </Notice>
                    )}
                    {answer.hide_recipients && (
                        <p>
                            {t(
                                answer.hidden_by_departure
                                    ? "hiddenByDeparture"
                                    : "hiddenToday",
                            )}
                        </p>
                    )}
                    {visible && (
                        <>
                            <Groups answer={answer} />
                            <p>{t("summarySentence")}</p>
                            {!shown.to.length && !shown.cc.length && (
                                <p className={styles.warning}>
                                    {t("allHiddenWarning")}
                                </p>
                            )}
                        </>
                    )}
                    {!count && <p>{t("nobodyReceives")}</p>}
                    {addresses.added
                        .filter((item) => !item.eligible)
                        .map((item, index) => (
                            <p key={index} className="fine">
                                {t("addsNoRecipient", {
                                    address: item.address,
                                })}
                            </p>
                        ))}
                </>
            )}
        </div>
    );
}

function ReviewDialog({
    review,
    confirm,
    cancel,
}: {
    review: Extract<Review, { body: unknown }>;
    confirm: () => void;
    cancel: () => void;
}) {
    const t = useTranslations("Reminders");
    const common = useTranslations("Common");
    const id = useId();
    const dialog = useRef<HTMLDialogElement>(null);
    const heading = useRef<HTMLHeadingElement>(null);
    const alert = useRef<HTMLDivElement>(null);
    const confirming = review.status === "confirming";
    useLayoutEffect(() => {
        dialog.current?.showModal();
        heading.current?.focus();
    }, []);
    useLayoutEffect(() => {
        if (dialog.current && !dialog.current.open) dialog.current.showModal();
    });
    useEffect(() => {
        if (review.status === "failed") alert.current?.focus();
    }, [review]);
    return (
        <dialog
            ref={dialog}
            className={styles.dialog}
            aria-labelledby={`${id}-title`}
            aria-describedby={`${id}-sentence`}
            onCancel={(event) => {
                event.preventDefault();
                if (!confirming) cancel();
            }}
            onClose={() => {
                if (!confirming) cancel();
            }}
        >
            <h2 id={`${id}-title`} tabIndex={-1} ref={heading}>
                {t("reviewTitle")}
            </h2>
            <p id={`${id}-sentence`}>{t("reviewSentence")}</p>
            <Groups answer={review.answer} />
            {review.status === "failed" && (
                <div
                    className="notice error"
                    role="alert"
                    tabIndex={-1}
                    ref={alert}
                >
                    <strong>{common("saveFailed")}</strong>
                    <p>{review.message}</p>
                </div>
            )}
            <div className={styles.actions}>
                <button
                    type="button"
                    className="button"
                    aria-disabled={confirming}
                    onClick={confirm}
                >
                    {confirming ? common("saving") : t("confirmSave")}
                </button>
                <button
                    type="button"
                    className="button secondary"
                    aria-disabled={confirming}
                    onClick={() => {
                        if (!confirming) cancel();
                    }}
                >
                    {t("backToEditing")}
                </button>
            </div>
        </dialog>
    );
}

function RoleControl({
    name,
    address,
    checked,
    choose,
}: {
    name: string;
    address: string;
    checked: Choice;
    choose: (choice: Choice) => void;
}) {
    const t = useTranslations("Reminders");
    return (
        <fieldset className={styles.roles}>
            <legend className="sr-only">{t("roleGroup", { address })}</legend>
            {OPTIONS.map((value) => (
                <label key={value ?? "default"} className={styles.segment}>
                    <input
                        type="radio"
                        name={name}
                        value={value ?? "default"}
                        checked={checked === value}
                        onChange={() => choose(value)}
                    />
                    {value ? t(`roles.${value}`) : t("defaultRole")}
                </label>
            ))}
        </fieldset>
    );
}

const withChoice = (
    choices: Record<string, Choice>,
    key: string,
    stored: Choice,
    choice: Choice,
) =>
    choice === stored
        ? Object.fromEntries(
              Object.entries(choices).filter(([other]) => other !== key),
          )
        : { ...choices, [key]: choice };

type Control = { key: string; stored: Choice };

function RecipientSection({
    addresses,
    form,
    draft,
    update,
}: {
    addresses: Addresses;
    form: Form;
    draft: Draft;
    update: Dispatch<SetStateAction<Draft>>;
}) {
    const t = useTranslations("Reminders");
    const visible = !form.hide;
    const { prefix, focusRow } = useRowFocus(form.rows);
    const added = new Map(
        addresses.added.map((item) => [identity(item.address), item]),
    );
    const members = new Map(
        addresses.members.map((member) => [identity(member.address), member]),
    );
    const listed = new Set(
        form.rows
            .map((row) => identity(row.text))
            .filter((key) => added.has(key)),
    );
    const choiceOf = (key: string, stored: Choice) =>
        key in draft.choices ? (draft.choices[key] ?? null) : stored;
    const choose = (key: string, stored: Choice) => (choice: Choice) =>
        update((now) => ({
            ...now,
            choices: withChoice(now.choices, key, stored, choice),
        }));
    const setRows = (change: (rows: Row[]) => Row[]) =>
        update((now) => ({ ...now, rows: change(now.rows ?? form.rows) }));

    const orphans = Object.keys(draft.choices).filter(
        (key) => !listed.has(key) && !members.has(key),
    );
    const discard = useEffectEvent((removed: string[]) =>
        update((now) => ({
            ...now,
            choices: Object.fromEntries(
                Object.entries(now.choices).filter(
                    ([key]) => !removed.includes(key),
                ),
            ),
            removed,
        })),
    );
    const orphaned = orphans.join("\n");
    useEffect(() => {
        if (orphaned) discard(orphaned.split("\n"));
    }, [orphaned]);

    const membersHeading = useRef<HTMLHeadingElement>(null);
    const focused = useRef<string | null>(null);
    useLayoutEffect(() => {
        const key = focused.current;
        if (
            key === null ||
            addresses.members.some(
                (member) => member.memberships.join() === key,
            )
        )
            return;
        focused.current = null;
        if (document.activeElement === document.body)
            membersHeading.current?.focus();
    });

    const shownMembers = visible && form.include;
    const controls: Control[] = [
        ...(shownMembers ? addresses.members : [])
            .filter((member) => !listed.has(identity(member.address)))
            .map((member) => ({
                key: identity(member.address),
                stored: member.role,
            })),
        ...(visible ? addresses.added : [])
            .filter(
                (item) => item.eligible && listed.has(identity(item.address)),
            )
            .map((item) => ({
                key: identity(item.address),
                stored: item.role,
            })),
    ];
    return (
        <fieldset className="reminder-recipients">
            <legend>{t("emailRecipients")}</legend>
            <p className="fine">{t("groupPrivacy")}</p>
            <CheckboxField
                label={t("hideRecipients")}
                name="hide_recipients"
                checked={form.hide}
                onChange={(hide) => update((now) => ({ ...now, hide }))}
            />
            {visible ? (
                <>
                    <p className="fine">{t("addressingRule")}</p>
                    <p className="fine">{t("roleKey")}</p>
                </>
            ) : (
                <p className="fine">{t("hiddenHelper")}</p>
            )}
            <CheckboxField
                label={t("includeWorkspaceMembers")}
                name="include_workspace_members"
                checked={form.include}
                onChange={(include) => update((now) => ({ ...now, include }))}
            />
            <p className="fine">{t("membershipSync")}</p>
            {shownMembers ? (
                <>
                    <p className="fine">{t("newMembersDefault")}</p>
                    <h3
                        className={styles.listHeading}
                        tabIndex={-1}
                        ref={membersHeading}
                    >
                        {t("workspaceMembers", {
                            count: addresses.members.length,
                        })}
                    </h3>
                    {addresses.members.length ? (
                        <ul className={styles.members}>
                            {addresses.members.map((member) => {
                                const key = identity(member.address);
                                const id = member.memberships.join();
                                return (
                                    <li
                                        key={id}
                                        className={styles.member}
                                        onFocus={() => {
                                            focused.current = id;
                                        }}
                                    >
                                        <span className={styles.identity}>
                                            <span>{member.address}</span>
                                            <span className="field-hint">
                                                {t(`kinds.${member.kind}`)}
                                            </span>
                                        </span>
                                        {listed.has(key) ? (
                                            <span className={styles.state}>
                                                {t("setUnderOtherAddresses")}
                                            </span>
                                        ) : (
                                            <RoleControl
                                                name={`${prefix}-m-${id}`}
                                                address={member.address}
                                                checked={choiceOf(
                                                    key,
                                                    member.role,
                                                )}
                                                choose={choose(
                                                    key,
                                                    member.role,
                                                )}
                                            />
                                        )}
                                    </li>
                                );
                            })}
                        </ul>
                    ) : (
                        <p className="fine">{t("noWorkspaceMembers")}</p>
                    )}
                </>
            ) : (
                <details className="reminder-members">
                    <summary>
                        {t("workspaceMembers", {
                            count: addresses.workspace_recipients.length,
                        })}
                    </summary>
                    {addresses.workspace_recipients.length ? (
                        <ul>
                            {addresses.workspace_recipients.map(
                                (email, index) => (
                                    <li key={index}>{email}</li>
                                ),
                            )}
                        </ul>
                    ) : (
                        <p className="fine">{t("noWorkspaceMembers")}</p>
                    )}
                </details>
            )}
            <p className="recipient-label">{t("otherAddresses")}</p>
            {form.rows.map((row, index) => {
                const key = identity(row.text);
                const item = added.get(key);
                const member = members.get(key);
                const pending = visible && !!key && !item;
                return (
                    <div className={styles.entry} key={row.id}>
                        <div className="recipient-row">
                            <label
                                className="field"
                                htmlFor={`${prefix}-${row.id}`}
                            >
                                {t("emailAddress", { index: index + 1 })}
                                <input
                                    id={`${prefix}-${row.id}`}
                                    name="recipients"
                                    type="email"
                                    autoCapitalize="none"
                                    autoCorrect="off"
                                    value={row.text}
                                    aria-describedby={
                                        pending
                                            ? `${prefix}-${row.id}-role`
                                            : undefined
                                    }
                                    onChange={(event) => {
                                        const text = event.target.value;
                                        setRows((rows) =>
                                            rows.map((other) =>
                                                other.id === row.id
                                                    ? { ...other, text }
                                                    : other,
                                            ),
                                        );
                                    }}
                                />
                            </label>
                            {form.rows.length > 1 && (
                                <button
                                    className="button secondary"
                                    type="button"
                                    aria-label={t("removeEmailAddress", {
                                        index: index + 1,
                                    })}
                                    onClick={() => {
                                        const neighbour =
                                            form.rows[index + 1] ??
                                            form.rows[index - 1];
                                        if (neighbour) focusRow(neighbour.id);
                                        setRows((rows) =>
                                            rows.filter(
                                                (other) => other.id !== row.id,
                                            ),
                                        );
                                    }}
                                >
                                    <span aria-hidden="true">&times;</span>
                                </button>
                            )}
                        </div>
                        {visible && item?.eligible && (
                            <div className={styles.placement}>
                                <RoleControl
                                    name={`${prefix}-t-${row.id}`}
                                    address={item.address}
                                    checked={choiceOf(key, item.role)}
                                    choose={choose(key, item.role)}
                                />
                                {member && (
                                    <span className="field-hint">
                                        {t(`kinds.${member.kind}`)}
                                    </span>
                                )}
                            </div>
                        )}
                        {pending && (
                            <p className="fine" id={`${prefix}-${row.id}-role`}>
                                {t("roleOnceSaved")}
                            </p>
                        )}
                    </div>
                );
            })}
            <button
                className="button secondary"
                type="button"
                onClick={() => {
                    const id =
                        Math.max(-1, ...form.rows.map((row) => row.id)) + 1;
                    focusRow(id);
                    setRows((rows) => [...rows, { id, text: "" }]);
                }}
            >
                {t("addAddress")}
            </button>
            {controls.length > 0 && (
                <button
                    type="button"
                    className="button secondary"
                    onClick={() =>
                        update((now) => ({
                            ...now,
                            choices: controls.reduce(
                                (choices, { key, stored }) =>
                                    withChoice(choices, key, stored, null),
                                now.choices,
                            ),
                        }))
                    }
                >
                    {t("useDefaults")}
                </button>
            )}
            <div aria-live="polite">
                {draft.removed?.map((address, index) => (
                    <p key={index} className="fine">
                        {t("choiceRemoved", { address })}
                    </p>
                ))}
            </div>
        </fieldset>
    );
}

export function ReminderEditor({
    answer,
    addresses,
    reload,
    scope,
    region,
}: {
    answer: Answer;
    addresses: Addresses;
    reload: () => Promise<Answer>;
    scope: RegionScope;
    region: Region;
}) {
    const t = useTranslations("Reminders");
    const refresh = useRefresh();
    const errorMessage = useErrorMessage();
    const [draft, setDraft] = useState(EMPTY);
    const [review, setReview] = useState(CLOSED);
    const [after, setAfter] = useState<After>(null);
    const [cancelled, setCancelled] = useState(false);
    const decision = useRef<Decision | null>(null);
    const reads = useRef(0);
    const wrapper = useRef<HTMLDivElement>(null);
    const heading = useRef<HTMLHeadingElement>(null);
    const line = useRef<HTMLParagraphElement>(null);
    const status = useRef<HTMLDivElement>(null);
    const form = formOf(answer, draft);

    const readAgain = (next: NonNullable<After>) => {
        const read = ++reads.current;
        flushSync(() => setAfter(next));
        if (next.focus) line.current?.focus();
        reload().then(
            () => {
                if (read !== reads.current) return;
                const move = !!status.current?.contains(document.activeElement);
                flushSync(() => {
                    if (next.cause === "saved") setDraft(EMPTY);
                    setAfter(null);
                });
                if (move) heading.current?.focus();
            },
            () => {
                if (read === reads.current)
                    setAfter({ ...next, status: "failed" });
            },
        );
    };

    const submit = async () => {
        const { body, saved } = bodyOf(answer, addresses, draft);
        setCancelled(false);
        setReview({ status: body.hide_recipients ? "saving" : "previewing" });
        let outcome: Outcome = "saved";
        try {
            if (body.hide_recipients)
                await api.put(PATH, { path: scope, body });
            else {
                const previewed = (await api.post(PREVIEW, {
                    path: scope,
                    body,
                })) as Previewed;
                outcome = await new Promise<Outcome>((resolve, reject) => {
                    decision.current = { resolve, reject };
                    setReview({ status: "open", body, answer: previewed });
                });
            }
        } catch (error) {
            setReview(CLOSED);
            if (!conflicted(error)) throw error;
            readAgain({ cause: "changed", status: "loading", focus: false });
            throw new ApiError(t("conflict"));
        } finally {
            decision.current = null;
        }
        if (outcome === "cancelled") {
            flushSync(() => {
                setReview(CLOSED);
                setCancelled(true);
            });
            wrapper.current
                ?.querySelector<HTMLElement>("[type=submit]")
                ?.focus();
            return;
        }
        setReview(CLOSED);
        setDraft(saved);
        readAgain({ cause: "saved", status: "loading", focus: true });
        void refresh.workspace(scope.tenant_id, {
            path: PATH,
            options: { path: scope },
        });
    };

    const confirm = async () => {
        if (review.status !== "open" && review.status !== "failed") return;
        const { body, answer: previewed } = review;
        setReview({ status: "confirming", body, answer: previewed });
        const confirmed: Confirmed = {
            ...body,
            disclosure: previewed.disclosure,
        };
        try {
            await api.put(PATH, { path: scope, body: confirmed });
        } catch (error) {
            if (conflicted(error)) decision.current?.reject(error);
            else
                setReview({
                    status: "failed",
                    body,
                    answer: previewed,
                    message: errorMessage(error),
                });
            return;
        }
        decision.current?.resolve("saved");
    };

    return (
        <>
            <SavedView
                answer={answer}
                addresses={addresses}
                after={after}
                retry={() =>
                    after &&
                    readAgain({ ...after, status: "loading", focus: true })
                }
                heading={heading}
                line={line}
                status={status}
            />
            <div ref={wrapper}>
                <ActionForm
                    submit={t("saveReminders")}
                    permitted={after?.cause !== "saved"}
                    pending={review.status !== "closed"}
                    success={cancelled ? null : undefined}
                    onSubmit={submit}
                >
                    <ReminderFields
                        answer={answer}
                        region={region}
                        fields={form}
                        change={{
                            enabled: (enabled) =>
                                setDraft((now) => ({ ...now, enabled })),
                            times: (apply) =>
                                setDraft((now) => ({
                                    ...now,
                                    times: apply(now.times ?? form.times),
                                })),
                            language: (language) =>
                                setDraft((now) => ({ ...now, language })),
                        }}
                    >
                        <RecipientSection
                            addresses={addresses}
                            form={form}
                            draft={draft}
                            update={setDraft}
                        />
                    </ReminderFields>
                </ActionForm>
            </div>
            {"body" in review && (
                <ReviewDialog
                    review={review}
                    confirm={() => void confirm()}
                    cancel={() => decision.current?.resolve("cancelled")}
                />
            )}
        </>
    );
}
