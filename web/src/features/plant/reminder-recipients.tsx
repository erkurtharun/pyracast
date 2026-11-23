"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import { flushSync } from "react-dom";
import type { components } from "@/lib/schema";
import { CheckboxField } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { ApiError } from "@/lib/api/errors";
import {
    filled,
    memberKey,
    type Addresses,
    type Answer,
    type Choice,
    type Draft,
    type Role,
    type TypedRow,
} from "./reminder-draft";
import styles from "./reminder-recipients.module.css";
import type { RecipientState } from "./reminders";
import { useRowFocus } from "./row-focus";

type Schemas = components["schemas"];
type MemberRow = Schemas["MemberAddressOutput"];
type AddedRow = Schemas["AddedAddressOutput"];
const ROLES = ["to", "cc", "bcc"] as const satisfies readonly Role[];
const visibleRole = (role: Role) => role !== "bcc";

function focusChecked(name: string) {
    const checked = [...document.getElementsByName(name)].find(
        (input) => input instanceof HTMLInputElement && input.checked,
    );
    checked?.focus();
}

type Preview =
    | { status: "answered"; answer: Answer }
    | { status: "checking" }
    | { status: "refused"; message: string }
    | { status: "failed"; retry: () => void };
type Basis = { addresses: Addresses; recipients: string[] };

function previewOf({ base, unsaved, preview }: RecipientState): Preview {
    if (!unsaved) return { status: "answered", answer: base };
    if (preview.current && preview.data?.answer.addresses)
        return { status: "answered", answer: preview.data.answer };
    const error = preview.error;
    if (error instanceof ApiError && [400, 409].includes(error.status)) {
        const fields = Object.values(error.fields);
        return {
            status: "refused",
            message: fields.length ? fields.join(" ") : error.message,
        };
    }
    if (error) return { status: "failed", retry: preview.retry };
    return { status: "checking" };
}

// The last answer the rows are drawn from; while the present draft is checked it is an earlier one.
function basisOf({ addresses, unsaved, preview }: RecipientState): Basis {
    const answered = preview.data?.answer.addresses;
    return unsaved && answered && preview.data
        ? { addresses: answered, recipients: preview.data.body.recipients }
        : { addresses, recipients: addresses.recipients };
}

// The answer has one entry per sent text, in order.
function answersOf(rows: TypedRow[], basis: Basis) {
    const used = new Set<number>();
    const answers = new Map<number, AddedRow>();
    for (const row of filled(rows)) {
        const at = basis.recipients.findIndex(
            (text, index) => !used.has(index) && text === row.text.trim(),
        );
        const added = basis.addresses.added[at];
        if (at < 0 || !added) continue;
        used.add(at);
        answers.set(row.id, added);
    }
    return answers;
}

type Shown = { checked: Role; set: boolean };
type TypedView = {
    row: TypedRow;
    added: AddedRow | undefined;
    member: MemberRow | undefined;
    shown: Shown | undefined;
};

function rowsOf(draft: Draft, basis: Basis, preview: Preview) {
    const current = preview.status === "answered";
    const answers = answersOf(draft.rows, basis);
    const membersByKey = new Map(
        basis.addresses.members.map((member) => [
            memberKey(member.memberships),
            member,
        ]),
    );
    const shownAs = (
        answered: { role: Choice; placement: Role },
        decision: { role: Choice } | undefined,
    ): Shown =>
        current
            ? { checked: answered.placement, set: answered.role !== null }
            : {
                  checked: decision?.role ?? answered.placement,
                  set: (decision ? decision.role : answered.role) !== null,
              };
    const typed: TypedView[] = draft.rows.map((row) => {
        const added = answers.get(row.id);
        const member =
            added?.memberships.length && added.eligible
                ? membersByKey.get(memberKey(added.memberships))
                : undefined;
        return {
            row,
            added,
            member,
            shown:
                added?.eligible && !draft.hide
                    ? shownAs(added, draft.typed[row.id])
                    : undefined,
        };
    });
    const merged = new Set(
        typed.flatMap((item) =>
            item.member ? [memberKey(item.member.memberships)] : [],
        ),
    );
    const members =
        draft.include && !draft.hide
            ? basis.addresses.members.map((member) => {
                  const key = memberKey(member.memberships);
                  return {
                      member,
                      key,
                      shown: merged.has(key)
                          ? undefined
                          : shownAs(member, draft.members[key]),
                  };
              })
            : [];
    return { typed, members };
}

function summaryOf(
    { typed, members }: ReturnType<typeof rowsOf>,
    count: number,
) {
    const placed = [
        ...members.flatMap(({ member, shown }) =>
            shown ? [{ address: member.address, role: member.placement }] : [],
        ),
        ...typed.flatMap(({ added, shown }) =>
            added && shown
                ? [
                      {
                          address: added.address,
                          role: added.placement,
                          outside: !added.memberships.length,
                      },
                  ]
                : [],
        ),
    ];
    const listed = (role: Role) =>
        placed.filter((item) => item.role === role).map((item) => item.address);
    const to = listed("to");
    const cc = listed("cc");
    return {
        to,
        cc,
        hidden: Math.max(0, count - to.length - cc.length),
        outside: placed.filter(
            (item) =>
                "outside" in item && item.outside && visibleRole(item.role),
        ),
        count,
    };
}

function Placement({
    name,
    address,
    shown,
    onChoose,
    children,
}: {
    name: string;
    address: string;
    shown: Shown;
    onChoose: (choice: Choice) => void;
    children?: ReactNode;
}) {
    const t = useTranslations("Reminders");
    return (
        <div className={styles.placement}>
            <fieldset className={styles.roles}>
                <legend className="sr-only">
                    {t("roleGroup", { address })}
                </legend>
                {ROLES.map((value) => (
                    <label key={value} className={styles.segment}>
                        <input
                            type="radio"
                            name={name}
                            value={value}
                            checked={shown.checked === value}
                            onChange={() => onChoose(value)}
                        />
                        {t(`roles.${value}`)}
                    </label>
                ))}
            </fieldset>
            <span className={styles.state}>
                {children}
                {t(shown.set ? "chosenRole" : "defaultRole")}
                {shown.set && (
                    <button
                        type="button"
                        className="text-button"
                        aria-label={t("useDefaultFor", { address })}
                        onClick={() => onChoose(null)}
                    >
                        {t("useDefault")}
                    </button>
                )}
            </span>
        </div>
    );
}

function CheckState({ preview }: { preview: Preview }) {
    const t = useTranslations("Reminders");
    if (preview.status === "refused")
        return <p className={styles.warning}>{preview.message}</p>;
    if (preview.status !== "failed") return null;
    return (
        <>
            <p>{t("checkFailed")}</p>
            <button
                type="button"
                className="button secondary"
                onClick={preview.retry}
            >
                {t("checkAgain")}
            </button>
        </>
    );
}

function Summary({
    recipients,
    preview,
    rows,
    heading,
    held,
}: {
    recipients: RecipientState;
    preview: Preview;
    rows: ReturnType<typeof rowsOf>;
    heading: RefObject<HTMLHeadingElement | null>;
    held: ReactNode;
}) {
    const t = useTranslations("Reminders");
    const { unsaved } = recipients;
    const view =
        preview.status === "answered"
            ? summaryOf(rows, preview.answer.recipient_count)
            : null;
    return (
        <div className={styles.summary}>
            <h3 tabIndex={-1} ref={heading}>
                {t("whoSeesWhat")}
            </h3>
            {held}
            <div aria-live="polite">
                {preview.status === "checking" && <p>{t("checking")}</p>}
                <CheckState preview={preview} />
                {view && (
                    <>
                        {view.to.length > 0 && (
                            <p>
                                {t("summaryTo", {
                                    addresses: view.to.join(", "),
                                })}
                            </p>
                        )}
                        {view.cc.length > 0 && (
                            <p>
                                {t("summaryCopy", {
                                    addresses: view.cc.join(", "),
                                })}
                            </p>
                        )}
                        {view.hidden > 0 && (
                            <p>{t("summaryHidden", { count: view.hidden })}</p>
                        )}
                        {view.count ? (
                            <p>{t("summarySentence")}</p>
                        ) : (
                            <p>{t("nobodyReceives")}</p>
                        )}
                        {view.outside.map((item, index) => (
                            <p key={index} className={styles.warning}>
                                {t("outsideWarning", { address: item.address })}
                            </p>
                        ))}
                        {!view.to.length && view.cc.length > 0 && (
                            <p className={styles.warning}>{t("noToWarning")}</p>
                        )}
                        {view.count > 0 &&
                            !view.to.length &&
                            !view.cc.length && (
                                <p className={styles.warning}>
                                    {t("allHiddenWarning")}
                                </p>
                            )}
                    </>
                )}
                {unsaved && (
                    <p className={styles.unsaved}>
                        {t("notSavedYet")}
                        {view && view.to.length + view.cc.length > 0 && (
                            <> {t("savingShowsAddresses")}</>
                        )}
                    </p>
                )}
            </div>
        </div>
    );
}

export function RecipientSection({
    recipients,
    heading,
    held,
}: {
    recipients: RecipientState;
    heading: RefObject<HTMLHeadingElement | null>;
    held: ReactNode;
}) {
    const t = useTranslations("Reminders");
    const { base, draft, update } = recipients;
    const preview = previewOf(recipients);
    const basis = basisOf(recipients);
    const { prefix, focusRow } = useRowFocus(draft.rows);
    const pendingFocus = useRef<string | null>(null);
    const pressed = useRef(false);
    useEffect(() => {
        const down = () => (pressed.current = true);
        const up = () => (pressed.current = false);
        document.addEventListener("pointerdown", down, true);
        document.addEventListener("pointerup", up, true);
        document.addEventListener("pointercancel", up, true);
        return () => {
            document.removeEventListener("pointerdown", down, true);
            document.removeEventListener("pointerup", up, true);
            document.removeEventListener("pointercancel", up, true);
        };
    }, []);
    useEffect(() => {
        if (preview.status !== "answered" || !pendingFocus.current) return;
        focusChecked(pendingFocus.current);
        pendingFocus.current = null;
    });
    const rows = rowsOf(draft, basis, preview);
    const visible = !draft.hide;
    const counted =
        preview.status === "answered" ? preview.answer : recipients.base;
    const groupName = (key: string) => `${prefix}-role-${key}`;
    const setRows = (change: (rows: TypedRow[]) => TypedRow[]) =>
        update((now) => {
            const next = change(now.rows);
            const kept = new Set(next.map((row) => row.id));
            return {
                ...now,
                rows: next,
                typed: Object.fromEntries(
                    Object.entries(now.typed).filter(([id]) =>
                        kept.has(Number(id)),
                    ),
                ),
            };
        });
    const commit = (id: number, text: string) =>
        update((now) => {
            const row = now.rows.find((item) => item.id === id);
            if (!row || row.text.trim() === text.trim()) return now;
            const typed = { ...now.typed };
            delete typed[id];
            return {
                ...now,
                rows: now.rows.map((item) =>
                    item.id === id ? { ...item, text } : item,
                ),
                typed,
            };
        });
    // Committing during a press would move the control under the pointer before its click lands.
    const commitOnBlur = (id: number, input: HTMLInputElement) => {
        const apply = () => commit(id, input.value);
        if (!pressed.current) return apply();
        document.addEventListener("click", apply, {
            capture: true,
            once: true,
        });
    };
    const chooseMember = (member: MemberRow, role: Choice) =>
        update((now) => ({
            ...now,
            members: {
                ...now.members,
                [memberKey(member.memberships)]: {
                    memberships: member.memberships,
                    address: member.address,
                    role,
                },
            },
        }));
    const typedDecision = (
        added: AddedRow,
        member: MemberRow | undefined,
        role: Choice,
    ) => ({
        role,
        memberships: added.memberships,
        ...(member && {
            member: {
                memberships: member.memberships,
                address: member.address,
            },
        }),
    });
    const chooseTyped = (
        id: number,
        added: AddedRow,
        member: MemberRow | undefined,
        role: Choice,
    ) =>
        update((now) => ({
            ...now,
            typed: { ...now.typed, [id]: typedDecision(added, member, role) },
        }));
    const choose = (name: string, apply: (role: Choice) => void) => {
        return (role: Choice) => {
            if (role === null) pendingFocus.current = name;
            apply(role);
        };
    };
    const focusSummary = (change: (draft: Draft) => Draft) => {
        flushSync(() => update(change));
        heading.current?.focus();
    };
    const withDefaults = (now: Draft): Draft => {
        const members = { ...now.members };
        for (const { member, key, shown } of rows.members)
            if (shown?.set)
                members[key] = {
                    memberships: member.memberships,
                    address: member.address,
                    role: null,
                };
        const typed = { ...now.typed };
        for (const { row, added, member, shown } of rows.typed)
            if (added && shown?.set)
                typed[row.id] = typedDecision(added, member, null);
        return { ...now, members, typed };
    };
    const anySet =
        rows.members.some((item) => item.shown?.set) ||
        rows.typed.some((item) => item.shown?.set);
    return (
        <fieldset className="reminder-recipients">
            <legend>{t("emailRecipients")}</legend>
            <span className="recipient-count fine" aria-live="polite">
                {t("quotaCount", {
                    count: counted.recipient_count,
                    limit: counted.recipient_limit,
                })}
            </span>
            <p className="fine">{t("groupPrivacy")}</p>
            {counted.recipient_count > counted.recipient_limit && (
                <Notice>
                    {t(
                        counted.recipient_limit_exceeded
                            ? "recipientLimitExceeded"
                            : "recipientLimit",
                        { limit: counted.recipient_limit },
                    )}
                </Notice>
            )}
            {base.hide_recipients && draft.hide && (
                <div className={styles.review}>
                    <p>
                        {t(
                            base.hidden_by_departure
                                ? "hiddenByDeparture"
                                : "hiddenToday",
                        )}
                    </p>
                    <button
                        type="button"
                        className="button secondary"
                        onClick={() =>
                            focusSummary((now) => ({ ...now, hide: false }))
                        }
                    >
                        {t("reviewAddressing")}
                    </button>
                </div>
            )}
            <CheckboxField
                label={t("hideRecipients")}
                name="hide_recipients"
                checked={draft.hide}
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
                checked={draft.include}
                onChange={(include) => update((now) => ({ ...now, include }))}
            />
            <p className="fine">{t("membershipSync")}</p>
            {visible && draft.include && (
                <p className="fine">{t("newMembersDefault")}</p>
            )}
            {visible && draft.include ? (
                <>
                    <p className="recipient-label">
                        {t("workspaceMembers", {
                            count: rows.members.length,
                        })}
                    </p>
                    {rows.members.length ? (
                        <ul className={styles.members}>
                            {rows.members.map(({ member, key, shown }) => (
                                <li key={key} className={styles.member}>
                                    <span className={styles.identity}>
                                        <span>{member.address}</span>
                                        <span className="field-hint">
                                            {t(`kinds.${member.kind}`)}
                                        </span>
                                    </span>
                                    {shown ? (
                                        <Placement
                                            name={groupName(`m-${key}`)}
                                            address={member.address}
                                            shown={shown}
                                            onChoose={choose(
                                                groupName(`m-${key}`),
                                                (role) =>
                                                    chooseMember(member, role),
                                            )}
                                        />
                                    ) : (
                                        <span className={styles.state}>
                                            {t("setUnderOtherAddresses")}
                                        </span>
                                    )}
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <p className="fine">{t("noWorkspaceMembers")}</p>
                    )}
                </>
            ) : (
                <details className="reminder-members">
                    <summary>
                        {t("workspaceMembers", {
                            count: basis.addresses.workspace_recipients.length,
                        })}
                    </summary>
                    {basis.addresses.workspace_recipients.length ? (
                        <ul>
                            {basis.addresses.workspace_recipients.map(
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
            {rows.typed.map(({ row, added, member, shown }, index) => (
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
                                defaultValue={row.text}
                                onBlur={(event) =>
                                    commitOnBlur(row.id, event.target)
                                }
                            />
                        </label>
                        {draft.rows.length > 1 && (
                            <button
                                className="button secondary"
                                type="button"
                                aria-label={t("removeEmailAddress", {
                                    index: index + 1,
                                })}
                                onClick={() => {
                                    const neighbour =
                                        draft.rows[index + 1] ??
                                        draft.rows[index - 1];
                                    if (neighbour) focusRow(neighbour.id);
                                    setRows((now) =>
                                        now.filter(
                                            (other) => other.id !== row.id,
                                        ),
                                    );
                                }}
                            >
                                <span aria-hidden="true">&times;</span>
                            </button>
                        )}
                    </div>
                    {added && !added.eligible && (
                        <p className="fine">{t("notReceiving")}</p>
                    )}
                    {visible &&
                        !added &&
                        row.text.trim() &&
                        preview.status === "checking" && (
                            <p className="fine">{t("checkingAddress")}</p>
                        )}
                    {added && shown && (
                        <Placement
                            name={groupName(`t-${row.id}`)}
                            address={added.address}
                            shown={shown}
                            onChoose={choose(groupName(`t-${row.id}`), (role) =>
                                chooseTyped(row.id, added, member, role),
                            )}
                        >
                            {member && (
                                <span className="field-hint">
                                    {t(`kinds.${member.kind}`)}
                                </span>
                            )}
                        </Placement>
                    )}
                    {added &&
                        shown &&
                        preview.status === "answered" &&
                        !added.memberships.length &&
                        visibleRole(added.placement) && (
                            <p className={styles.warning}>
                                {t("outsideWarning", {
                                    address: added.address,
                                })}
                            </p>
                        )}
                </div>
            ))}
            <button
                className="button secondary"
                type="button"
                onClick={() => {
                    const id =
                        Math.max(-1, ...draft.rows.map((row) => row.id)) + 1;
                    focusRow(id);
                    setRows((now) => [...now, { id, text: "" }]);
                }}
            >
                {t("addAddress")}
            </button>
            {!visible && <CheckState preview={preview} />}
            {visible && (
                <Summary
                    recipients={recipients}
                    preview={preview}
                    rows={rows}
                    heading={heading}
                    held={held}
                />
            )}
            {visible && anySet && (
                <button
                    type="button"
                    className="button secondary"
                    onClick={() => focusSummary(withDefaults)}
                >
                    {t("useDefaults")}
                </button>
            )}
        </fieldset>
    );
}
