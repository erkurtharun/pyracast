"use client";
import { useTranslations } from "next-intl";
import { useRef, type RefObject } from "react";
import { flushSync } from "react-dom";
import { CheckboxField } from "@/ui/form";
import { Notice } from "@/ui/ui";
import {
    ROLES,
    addressing,
    chosen,
    withDefaults,
    type Addressing,
    type Choice,
    type ReminderAnswer,
    type Role,
    type Section,
} from "./reminder-addressing";
import styles from "./reminder-recipients.module.css";
import type { RecipientState } from "./reminders";
import { useRowFocus } from "./row-focus";

function focusChecked(name: string) {
    const checked = [...document.getElementsByName(name)].find(
        (input) => input instanceof HTMLInputElement && input.checked,
    );
    checked?.focus();
}

function Placement({
    name,
    address,
    role,
    set,
    onChoose,
}: {
    name: string;
    address: string;
    role: Role;
    set: boolean;
    onChoose: (choice: Choice) => void;
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
                            checked={role === value}
                            onChange={() => onChoose(value)}
                        />
                        {t(`roles.${value}`)}
                    </label>
                ))}
            </fieldset>
            <span className={styles.state}>
                {t(set ? "chosenRole" : "defaultRole")}
                {set && (
                    <button
                        type="button"
                        className="text-button"
                        aria-label={t("useDefaultFor", { address })}
                        onClick={() => {
                            flushSync(() => onChoose(null));
                            focusChecked(name);
                        }}
                    >
                        {t("useDefault")}
                    </button>
                )}
            </span>
        </div>
    );
}

function Summary({
    view,
    headingRef,
    unsavedSection,
}: {
    view: Addressing;
    headingRef: RefObject<HTMLHeadingElement | null>;
    unsavedSection: boolean;
}) {
    const t = useTranslations("Reminders");
    return (
        <div className={styles.summary}>
            <h3 tabIndex={-1} ref={headingRef}>
                {t("whoSeesWhat")}
            </h3>
            <div aria-live="polite">
                {view.to.length > 0 && (
                    <p>{t("summaryTo", { addresses: view.to.join(", ") })}</p>
                )}
                {view.cc.length > 0 && (
                    <p>{t("summaryCopy", { addresses: view.cc.join(", ") })}</p>
                )}
                {view.hidden > 0 && (
                    <p>{t("summaryHidden", { count: view.hidden })}</p>
                )}
                <p>{t("summarySentence")}</p>
                {view.outside.map((address) => (
                    <p key={address} className={styles.warning}>
                        {t("outsideWarning", { address })}
                    </p>
                ))}
                {view.noTo && (
                    <p className={styles.warning}>{t("noToWarning")}</p>
                )}
                {view.allHidden && (
                    <p className={styles.warning}>{t("allHiddenWarning")}</p>
                )}
                {unsavedSection && (
                    <p className={styles.unsaved}>{t("notSavedYet")}</p>
                )}
            </div>
        </div>
    );
}

export function RecipientSection({
    answer,
    recipients,
}: {
    answer: ReminderAnswer;
    recipients: RecipientState;
}) {
    const t = useTranslations("Reminders");
    const { addresses, section, update } = recipients;
    const { prefix, focusRow } = useRowFocus(section.rows);
    const headingRef = useRef<HTMLHeadingElement>(null);
    const view = addressing(section, addresses);
    const visible = !section.hide;
    const groupName = (key: string | number) => `${prefix}-role-${key}`;
    const setRows = (rows: Section["rows"]) => update({ ...section, rows });
    const chooseTyped = (id: number, choice: Choice) =>
        setRows(
            section.rows.map((row) =>
                row.id === id ? { ...row, choice } : row,
            ),
        );
    const chooseMember = (membership: number, choice: Choice) =>
        update({
            ...section,
            members: { ...section.members, [membership]: choice },
        });
    const focusSummary = (next: Section) => {
        flushSync(() => update(next));
        headingRef.current?.focus();
    };
    // A hint only: the server decides the limit and which addresses count.
    const count = new Set(
        [
            ...section.rows.map((row) => row.text.trim()).filter(Boolean),
            ...(section.include ? addresses.workspace_recipients : []),
        ].map((email) => email.toLowerCase()),
    ).size;
    return (
        <fieldset className="reminder-recipients">
            <legend>{t("emailRecipients")}</legend>
            <span className="recipient-count fine" aria-live="polite">
                {t("quotaCount", { count, limit: answer.recipient_limit })}
            </span>
            <p className="fine">{t("groupPrivacy")}</p>
            {count > answer.recipient_limit && (
                <Notice>
                    {t(
                        answer.recipient_limit_exceeded
                            ? "recipientLimitExceeded"
                            : "recipientLimit",
                        { limit: answer.recipient_limit },
                    )}
                </Notice>
            )}
            {answer.hide_recipients && section.hide && (
                <div className={styles.review}>
                    <p>{t("hiddenToday")}</p>
                    <button
                        type="button"
                        className="button secondary"
                        onClick={() =>
                            focusSummary({ ...section, hide: false })
                        }
                    >
                        {t("reviewAddressing")}
                    </button>
                </div>
            )}
            <CheckboxField
                label={t("hideRecipients")}
                name="hide_recipients"
                checked={section.hide}
                onChange={(hide) => update({ ...section, hide })}
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
                checked={section.include}
                onChange={(include) => update({ ...section, include })}
            />
            <p className="fine">{t("membershipSync")}</p>
            {visible && <p className="fine">{t("membersJoinInCopy")}</p>}
            {visible && section.include ? (
                <>
                    <p className="recipient-label">
                        {t("workspaceMembers", {
                            count: addresses.members.length,
                        })}
                    </p>
                    {view.members.length ? (
                        <ul className={styles.members}>
                            {view.members.map((item) => (
                                <li
                                    key={item.member.membership}
                                    className={styles.member}
                                >
                                    <span className={styles.identity}>
                                        <span>{item.member.address}</span>
                                        <span className="field-hint">
                                            {t(`kinds.${item.member.kind}`)}
                                        </span>
                                    </span>
                                    {item.typed ? (
                                        <span className={styles.state}>
                                            {t("setUnderOtherAddresses")}
                                        </span>
                                    ) : (
                                        <Placement
                                            name={groupName(
                                                `member-${item.member.membership}`,
                                            )}
                                            address={item.member.address}
                                            role={item.role}
                                            set={item.set}
                                            onChoose={(choice) =>
                                                chooseMember(
                                                    item.member.membership,
                                                    choice,
                                                )
                                            }
                                        />
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
                            count: addresses.workspace_recipients.length,
                        })}
                    </summary>
                    {addresses.workspace_recipients.length ? (
                        <ul>
                            {addresses.workspace_recipients.map((email) => (
                                <li key={email}>{email}</li>
                            ))}
                        </ul>
                    ) : (
                        <p className="fine">{t("noWorkspaceMembers")}</p>
                    )}
                </details>
            )}
            <p className="recipient-label">{t("otherAddresses")}</p>
            {view.typed.map((item, index) => {
                const { row } = item;
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
                                    onChange={(event) =>
                                        setRows(
                                            section.rows.map((other) =>
                                                other.id === row.id
                                                    ? {
                                                          ...other,
                                                          text: event.target
                                                              .value,
                                                          choice: null,
                                                      }
                                                    : other,
                                            ),
                                        )
                                    }
                                />
                            </label>
                            {section.rows.length > 1 && (
                                <button
                                    className="button secondary"
                                    type="button"
                                    aria-label={t("removeEmailAddress", {
                                        index: index + 1,
                                    })}
                                    onClick={() => {
                                        const neighbour =
                                            section.rows[index + 1] ??
                                            section.rows[index - 1];
                                        if (neighbour) focusRow(neighbour.id);
                                        setRows(
                                            section.rows.filter(
                                                (other) => other.id !== row.id,
                                            ),
                                        );
                                    }}
                                >
                                    <span aria-hidden="true">&times;</span>
                                </button>
                            )}
                        </div>
                        {item.address && !item.eligible ? (
                            <p className="fine">{t("notReceiving")}</p>
                        ) : (
                            visible &&
                            item.role && (
                                <Placement
                                    name={groupName(row.id)}
                                    address={item.address}
                                    role={item.role}
                                    set={item.set}
                                    onChoose={(choice) =>
                                        chooseTyped(row.id, choice)
                                    }
                                />
                            )
                        )}
                    </div>
                );
            })}
            <button
                className="button secondary"
                type="button"
                disabled={
                    count >= answer.recipient_limit ||
                    section.rows.length >= answer.recipient_limit
                }
                onClick={() => {
                    const id =
                        Math.max(-1, ...section.rows.map((row) => row.id)) + 1;
                    focusRow(id);
                    setRows([...section.rows, { id, text: "", choice: null }]);
                }}
            >
                {t("addAddress")}
            </button>
            {visible && view.to.length + view.cc.length + view.hidden > 0 && (
                <Summary
                    view={view}
                    headingRef={headingRef}
                    unsavedSection={recipients.unsaved}
                />
            )}
            {visible && chosen(section, view) && (
                <button
                    type="button"
                    className="button secondary"
                    onClick={() => focusSummary(withDefaults(section, view))}
                >
                    {t("useDefaults")}
                </button>
            )}
        </fieldset>
    );
}
