"use client";
import { useTranslations } from "next-intl";
import {
    useEffect,
    useEffectEvent,
    useId,
    useLayoutEffect,
    useRef,
    useState,
} from "react";
import { flushSync } from "react-dom";
import type { Region, RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError, answerLost } from "@/lib/api/errors";
import { ActionForm, CheckboxField, useErrorMessage } from "@/ui/form";
import { Notice } from "@/ui/ui";
import {
    bodyOf,
    controlsOf,
    formOf,
    identity,
    typedIdentity,
    type Control,
    type Form,
} from "./reminder-body";
import {
    DISCLOSURE_CHANGED,
    edited,
    type Addresses,
    type Answer,
    type Body,
    type Choice,
    type Confirmed,
    type Desk,
    type Draft,
    type Ending,
    type Previewed,
    type Role,
} from "./reminder-draft";
import { ReminderFields, type Row } from "./reminder-fields";
import styles from "./reminder-recipients.module.css";
import { useRowFocus } from "./row-focus";

const PATH = "/api/tenants/{tenant_id}/regions/{region_id}/reminders/";
const PREVIEW =
    "/api/tenants/{tenant_id}/regions/{region_id}/reminders/preview/";

type Reviewed = { body: Body; answer: Previewed; draft: Draft };
type Review =
    | { status: "closed" | "previewing" | "saving" }
    | ({ status: "open" | "confirming" } & Reviewed)
    | ({ status: "failed"; message: string } & Reviewed);
const CLOSED: Review = { status: "closed" };
const UNKNOWN: Ending = { outcome: "unknown", path: "unknown" };
const SAVED: Ending = { outcome: "saved", path: "saved" };

function endingOf(error: unknown, write: boolean): Ending | null {
    if (!(error instanceof ApiError)) return write ? UNKNOWN : null;
    if (error.status === 403)
        return {
            outcome:
                error.code === "workspace_admin_required" ? "lost" : "refused",
            path: "refusal",
        };
    if (error.status === 409)
        return {
            outcome:
                error.code === "reminder_schedule_changed" ||
                (error.body as { code?: unknown } | null)?.code ===
                    DISCLOSURE_CHANGED
                    ? "changed"
                    : "refused",
            path: "conflict",
        };
    const unanswered =
        answerLost(error) ||
        error.status >= 500 ||
        (error.status >= 200 && error.status < 300);
    return write && unanswered ? UNKNOWN : null;
}

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
}: {
    answer: Answer;
    addresses: Addresses;
}) {
    const t = useTranslations("Reminders");
    const count = answer.recipient_count;
    const visible = !answer.hide_recipients && count > 0;
    const shown = groupsOf(answer);
    return (
        <div className={styles.summary}>
            <h3>{t("whoSeesWhat")}</h3>
            <p className="recipient-count fine">
                {t("quotaCount", { count, limit: answer.recipient_limit })}
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
                        {t("addsNoRecipient", { address: item.address })}
                    </p>
                ))}
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
    const { to, cc } = groupsOf(review.answer);
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
            {!to.length && !cc.length && <p>{t("noAddressShown")}</p>}
            <p>{t("reviewLater")}</p>
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

const decided = (
    draft: Draft,
    { target, key }: Control,
    role: Choice,
): Draft => ({
    ...draft,
    decisions: new Map(draft.decisions).set(key, { ...target, role }),
});

function RecipientSection({
    addresses,
    form,
    controls,
    draft,
    update,
}: {
    addresses: Addresses;
    form: Form;
    controls: Map<string, Control>;
    draft: Draft;
    update: (change: (draft: Draft) => Draft) => void;
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
    const choiceOf = (control: Control, stored: Choice) => {
        const decision = draft.decisions.get(control.key);
        return decision ? decision.role : stored;
    };
    const choose = (control: Control) => (role: Choice) =>
        update((now) => decided(now, control, role));
    const setRows = (change: (rows: Row[]) => Row[]) =>
        update((now) => ({ ...now, rows: change(now.rows ?? form.rows) }));

    const shownMembers = visible && form.include;
    const shown = [...controls.values()].filter((control) =>
        control.target.kind === "member"
            ? shownMembers
            : visible && control.sent,
    );
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
                    <h3 className={styles.listHeading}>
                        {t("workspaceMembers", {
                            count: addresses.members.length,
                        })}
                    </h3>
                    {addresses.members.length ? (
                        <ul className={styles.members}>
                            {addresses.members.map((member) => {
                                const id = member.memberships.join();
                                const control = controls.get(
                                    identity(member.address),
                                );
                                return (
                                    <li key={id} className={styles.member}>
                                        <span className={styles.identity}>
                                            <span>{member.address}</span>
                                            <span className="field-hint">
                                                {t(`kinds.${member.kind}`)}
                                            </span>
                                        </span>
                                        {control?.target.kind === "member" ? (
                                            <RoleControl
                                                name={`${prefix}-m-${id}`}
                                                address={member.address}
                                                checked={choiceOf(
                                                    control,
                                                    member.role,
                                                )}
                                                choose={choose(control)}
                                            />
                                        ) : (
                                            control?.sent && (
                                                <span className={styles.state}>
                                                    {t(
                                                        "setUnderOtherAddresses",
                                                    )}
                                                </span>
                                            )
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
                const key = typedIdentity(row.text);
                const item = added.get(key);
                const member = members.get(key);
                const control = controls.get(key);
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
                        {visible &&
                            item &&
                            control?.target.kind === "typed" && (
                                <div className={styles.placement}>
                                    <RoleControl
                                        name={`${prefix}-t-${row.id}`}
                                        address={item.address}
                                        checked={choiceOf(control, item.role)}
                                        choose={choose(control)}
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
            {shown.length > 0 && (
                <button
                    type="button"
                    className="button secondary"
                    onClick={() =>
                        update((now) =>
                            shown.reduce(
                                (draft, control) =>
                                    decided(draft, control, null),
                                now,
                            ),
                        )
                    }
                >
                    {t("useDefaults")}
                </button>
            )}
        </fieldset>
    );
}

export function ReminderEditor({
    answer,
    addresses,
    desk,
    scope,
    region,
}: {
    answer: Answer;
    addresses: Addresses;
    desk: Desk;
    scope: RegionScope;
    region: Region;
}) {
    const t = useTranslations("Reminders");
    const errorMessage = useErrorMessage();
    const [review, setReview] = useState(CLOSED);
    const wrapper = useRef<HTMLDivElement>(null);
    const { draft } = desk.panel;
    const form = formOf(answer, draft);
    const update = (change: (draft: Draft) => Draft) =>
        desk.change((now) => {
            const started = edited(now.draft);
            return {
                ...now,
                outcome: started ? now.outcome : "none",
                draft: {
                    ...change(now.draft),
                    base: started
                        ? (now.draft.base ?? answer.revision)
                        : answer.revision,
                },
            };
        });
    const rebase = useEffectEvent(() =>
        desk.change((now) =>
            now.draft.base === undefined && edited(now.draft)
                ? { ...now, draft: { ...now.draft, base: answer.revision } }
                : now,
        ),
    );
    useEffect(() => {
        rebase();
    }, []);

    const controls = controlsOf(addresses, form);
    const offered = new Set(
        [...controls.values()].map((control) => control.key),
    );
    const dropped = [...draft.decisions.keys()]
        .filter((key) => !offered.has(key))
        .join("\n");
    const drop = useEffectEvent((keys: string[]) =>
        desk.change((now) => {
            const removed = keys.flatMap((key) => {
                const decision = now.draft.decisions.get(key);
                return decision ? [decision] : [];
            });
            if (!removed.length) return now;
            const decisions = new Map(now.draft.decisions);
            for (const key of keys) decisions.delete(key);
            return {
                ...now,
                draft: { ...now.draft, decisions },
                dropped: removed.map(({ address, role }) => ({
                    address,
                    role,
                })),
            };
        }),
    );
    useEffect(() => {
        if (dropped) drop(dropped.split("\n"));
    }, [dropped]);

    const submit = async () => {
        const body = bodyOf(answer, addresses, draft);
        desk.change((now) => ({ ...now, outcome: "none", dropped: [] }));
        const hidden = body.hide_recipients === true;
        setReview({ status: hidden ? "saving" : "previewing" });
        let previewed: Previewed | undefined;
        try {
            if (hidden) await api.put(PATH, { path: scope, body });
            else
                previewed = (await api.post(PREVIEW, {
                    path: scope,
                    body,
                })) as Previewed;
        } catch (error) {
            const ending = endingOf(error, hidden);
            if (!ending) {
                setReview(CLOSED);
                throw error;
            }
            return desk.finish(ending, draft);
        }
        if (!previewed) return desk.finish(SAVED, draft);
        setReview({ status: "open", body, answer: previewed, draft });
    };

    const confirm = async () => {
        if (review.status !== "open" && review.status !== "failed") return;
        const reviewed = {
            body: review.body,
            answer: review.answer,
            draft: review.draft,
        };
        setReview({ status: "confirming", ...reviewed });
        const confirmed: Confirmed = {
            ...reviewed.body,
            disclosure: reviewed.answer.disclosure,
        };
        try {
            await api.put(PATH, { path: scope, body: confirmed });
        } catch (error) {
            const ending = endingOf(error, true);
            if (ending) return desk.finish(ending, reviewed.draft);
            return setReview({
                status: "failed",
                ...reviewed,
                message: errorMessage(error),
            });
        }
        desk.finish(SAVED, reviewed.draft);
    };

    const cancel = () => {
        flushSync(() => setReview(CLOSED));
        wrapper.current?.querySelector<HTMLElement>("[type=submit]")?.focus();
    };

    return (
        <>
            <SavedView answer={answer} addresses={addresses} />
            <div ref={wrapper}>
                <ActionForm
                    submit={t("saveReminders")}
                    pending={review.status !== "closed"}
                    success={null}
                    onSubmit={submit}
                >
                    <ReminderFields
                        answer={answer}
                        region={region}
                        fields={form}
                        change={{
                            enabled: (enabled) =>
                                update((now) => ({ ...now, enabled })),
                            times: (apply) =>
                                update((now) => ({
                                    ...now,
                                    times: apply(now.times ?? form.times),
                                })),
                            language: (language) =>
                                update((now) => ({ ...now, language })),
                        }}
                    >
                        <RecipientSection
                            addresses={addresses}
                            form={form}
                            controls={controls}
                            draft={draft}
                            update={update}
                        />
                    </ReminderFields>
                </ActionForm>
            </div>
            {"body" in review && (
                <ReviewDialog
                    review={review}
                    confirm={() => void confirm()}
                    cancel={cancel}
                />
            )}
        </>
    );
}
