"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import { flushSync } from "react-dom";
import type { RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { CheckboxField } from "@/ui/form";
import { Notice } from "@/ui/ui";
import {
    addressingOf,
    committedDraft,
    filled,
    memberKey,
    settled,
    type AddedRow,
    type Addresses,
    type Answer,
    type Choice,
    type Draft,
    type MemberRow,
    type Role,
    type TypedRow,
} from "./reminder-draft";
import styles from "./reminder-recipients.module.css";
import type { Outcome, RecipientState, Section } from "./reminders";
import { useRowFocus } from "./row-focus";

type Known = Section["known"];

function knownOf(rows: TypedRow[], addresses: Addresses, known: Known) {
    const learnt = { ...known };
    filled(rows).forEach((row, index) => {
        const added = addresses.added[index];
        if (added) learnt[row.id] = { text: row.committed.trim(), added };
    });
    return learnt;
}

function editing({ change }: RecipientState) {
    return {
        update: (apply: (draft: Draft) => Draft, decided = true) =>
            change((section) => {
                const draft = apply(section.draft);
                if (draft === section.draft) return section;
                const rows = new Set(draft.rows.map((row) => row.id));
                return {
                    ...section,
                    draft,
                    known: Object.fromEntries(
                        Object.entries(section.known).filter(([id]) =>
                            rows.has(Number(id)),
                        ),
                    ),
                };
            }, decided),
        record: (asked: number, outcome: Outcome | null) =>
            change((section, generation) => {
                if (generation !== asked) return section;
                const addresses =
                    outcome?.status === "answered"
                        ? outcome.answer.addresses
                        : null;
                return {
                    ...section,
                    check: outcome && { generation, ...outcome },
                    ...(addresses && {
                        known: knownOf(
                            section.draft.rows,
                            addresses,
                            section.known,
                        ),
                        members: addresses.members,
                    }),
                };
            }, false),
    };
}

const PREVIEW =
    "/api/tenants/{tenant_id}/regions/{region_id}/reminders/preview/";
const ROLES = ["to", "cc", "bcc"] as const satisfies readonly Role[];
const visibleRole = (role: Role) => role !== "bcc";

function focusChecked(name: string) {
    const checked = [...document.getElementsByName(name)].find(
        (input) => input instanceof HTMLInputElement && input.checked,
    );
    checked?.focus();
}

type Status =
    | { status: "answered"; answer: Answer }
    | { status: "checking" | "changed" | "editing" }
    | { status: "refused"; message: string }
    | { status: "failed"; retry: () => void };

function outcomeOf(error: unknown): Outcome {
    return error instanceof ApiError && [400, 409].includes(error.status)
        ? { status: "refused", error }
        : { status: "failed" };
}

function useCheck(
    recipients: RecipientState,
    scope: RegionScope,
    record: ReturnType<typeof editing>["record"],
) {
    const { generation, unsaved, check, base, draft, section } = recipients;
    const settings =
        section.attempt?.generation === generation
            ? section.attempt
            : {
                  enabled: base.enabled,
                  times: base.times,
                  language: base.language,
              };
    const asking = unsaved && check === null;
    const asked = useRef(-1);
    const ask = () => {
        asked.current = generation;
        void api
            .post(PREVIEW, {
                path: scope,
                body: {
                    enabled: settings.enabled,
                    times: settings.times,
                    language: settings.language,
                    expected_revision: base.revision,
                    ...addressingOf(draft),
                },
            })
            .then(
                (answer) => record(generation, { status: "answered", answer }),
                (error: unknown) => record(generation, outcomeOf(error)),
            );
    };
    useEffect(() => {
        if (asking && asked.current !== generation) ask();
    });
    return () => {
        record(generation, null);
        ask();
    };
}

function statusOf(
    { section, check, newer, unsaved, answered, base, draft }: RecipientState,
    retry: () => void,
): Status {
    if (newer || section.saved?.settled) return { status: "changed" };
    if (draft.rows.some((row) => !settled(row))) return { status: "editing" };
    if (section.saved) return { status: "checking" };
    if (check?.status === "refused") {
        const fields = Object.values(check.error.fields);
        return {
            status: "refused",
            message: fields.length ? fields.join(" ") : check.error.message,
        };
    }
    if (!unsaved) return { status: "answered", answer: base };
    if (answered) return { status: "answered", answer: answered };
    return check?.status === "failed"
        ? { status: "failed", retry }
        : { status: "checking" };
}

type Shown = { checked: Role; set: boolean } | null;
type TypedView = {
    row: TypedRow;
    added: AddedRow | undefined;
    member: MemberRow | undefined;
    shown: Shown | undefined;
};

function rowsOf(recipients: RecipientState, status: Status) {
    const { draft, addresses, section } = recipients;
    const present =
        status.status === "answered" ? status.answer.addresses : null;
    const answered = new Map(
        filled(draft.rows).map((row, index) => [row.id, present?.added[index]]),
    );
    const members = present?.members ?? section.members;
    const learntFor = (row: TypedRow) =>
        [
            section.known[row.id],
            addresses.added[row.id] && {
                text: addresses.recipients[row.id]?.trim(),
                added: addresses.added[row.id],
            },
        ].find((entry) => entry?.text === row.committed.trim())?.added;
    const membersByKey = new Map(
        members.map((member) => [memberKey(member.memberships), member]),
    );
    const shownAs = (answer: { role: Choice; placement: Role }): Shown =>
        present
            ? { checked: answer.placement, set: answer.role !== null }
            : null;
    const typed: TypedView[] = draft.rows.map((row) => {
        const added = !settled(row)
            ? undefined
            : present
              ? answered.get(row.id)
              : learntFor(row);
        const member =
            added?.eligible && added.memberships.length
                ? membersByKey.get(memberKey(added.memberships))
                : undefined;
        return {
            row,
            added,
            member,
            shown: added?.eligible && !draft.hide ? shownAs(added) : undefined,
        };
    });
    const merged = new Set(
        typed.flatMap((item) =>
            item.member ? [memberKey(item.member.memberships)] : [],
        ),
    );
    return {
        typed,
        members:
            draft.include && !draft.hide
                ? members.map((member) => {
                      const key = memberKey(member.memberships);
                      return {
                          member,
                          key,
                          shown: merged.has(key) ? undefined : shownAs(member),
                      };
                  })
                : [],
    };
}

function summaryOf(answer: Answer) {
    const { members, added } = answer.addresses ?? { members: [], added: [] };
    const sent = added.filter((item) => item.eligible);
    const typed = new Set(
        sent.flatMap((item) =>
            item.memberships.length ? [memberKey(item.memberships)] : [],
        ),
    );
    const placed = [
        ...(answer.include_workspace_members ? members : [])
            .filter((member) => !typed.has(memberKey(member.memberships)))
            .map((member) => ({
                address: member.address,
                role: member.placement,
                outside: false,
            })),
        ...sent.map((item) => ({
            address: item.address,
            role: item.placement,
            outside: !item.memberships.length,
        })),
    ];
    const listed = (role: Role) =>
        placed.filter((item) => item.role === role).map((item) => item.address);
    const to = listed("to");
    const cc = listed("cc");
    const count = answer.recipient_count;
    return {
        to,
        cc,
        hidden: Math.max(0, count - to.length - cc.length),
        outside: placed.filter(
            (item) => item.outside && visibleRole(item.role),
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
                            checked={shown?.checked === value}
                            onChange={() => onChoose(value)}
                        />
                        {t(`roles.${value}`)}
                    </label>
                ))}
            </fieldset>
            <span className={styles.state}>
                {children}
                {shown && t(shown.set ? "chosenRole" : "defaultRole")}
                {shown?.set && (
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

function StateLine({ status }: { status: Status }) {
    const t = useTranslations("Reminders");
    switch (status.status) {
        case "answered":
            return null;
        case "refused":
            return <p className={styles.warning}>{status.message}</p>;
        case "failed":
            return (
                <>
                    <p>{t("checkFailed")}</p>
                    <button
                        type="button"
                        className="button secondary"
                        onClick={status.retry}
                    >
                        {t("checkAgain")}
                    </button>
                </>
            );
        case "changed":
            return <p>{t("changedElsewhere")}</p>;
        case "editing":
            return <p>{t("leaveAddressField")}</p>;
        case "checking":
            return <p>{t("checking")}</p>;
    }
}

function Summary({
    unsaved,
    status,
    heading,
    hold,
}: {
    unsaved: boolean;
    status: Status;
    heading: RefObject<HTMLHeadingElement | null>;
    hold: string | undefined;
}) {
    const t = useTranslations("Reminders");
    const view = status.status === "answered" ? summaryOf(status.answer) : null;
    return (
        <div className={styles.summary}>
            <h3 tabIndex={-1} ref={heading} aria-describedby={hold}>
                {t("whoSeesWhat")}
            </h3>
            <div aria-live="polite">
                {hold && (
                    <p className={styles.unsaved} id={hold}>
                        {t("saveWaitsForCheck")}
                    </p>
                )}
                <StateLine status={status} />
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
                        {unsaved && (
                            <p className={styles.unsaved}>
                                {t("notSavedYet")}
                                {view.to.length + view.cc.length > 0 && (
                                    <> {t("savingShowsAddresses")}</>
                                )}
                            </p>
                        )}
                    </>
                )}
            </div>
        </div>
    );
}

// A field left during a press commits at that press's end, so its row does not move under the pointer.
function useDeferredCommit(commit: (id: number) => void) {
    const pressed = useRef(false);
    const pending = useRef<(() => void) | null>(null);
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
            pending.current?.();
        };
    }, []);
    return (id: number) => {
        if (!pressed.current) return commit(id);
        pending.current?.();
        const stop = () => {
            document.removeEventListener("click", done, true);
            document.removeEventListener("pointercancel", done, true);
            pending.current = null;
        };
        const done = () => {
            stop();
            commit(id);
        };
        document.addEventListener("click", done, true);
        document.addEventListener("pointercancel", done, true);
        pending.current = stop;
    };
}

export function RecipientSection({
    recipients,
    scope,
    heading,
    hold,
}: {
    recipients: RecipientState;
    scope: RegionScope;
    heading: RefObject<HTMLHeadingElement | null>;
    hold: string | undefined;
}) {
    const t = useTranslations("Reminders");
    const { base, addresses, draft } = recipients;
    const { update, record } = editing(recipients);
    const status = statusOf(recipients, useCheck(recipients, scope, record));
    const present = status.status === "answered" ? status.answer : null;
    const { prefix, focusRow } = useRowFocus(draft.rows);
    const pendingFocus = useRef<string | null>(null);
    const commitOnBlur = useDeferredCommit((id) =>
        update((now) => committedDraft(now, id)),
    );
    useEffect(() => {
        if (!present || !pendingFocus.current) return;
        focusChecked(pendingFocus.current);
        pendingFocus.current = null;
    });
    const rows = rowsOf(recipients, status);
    const visible = !draft.hide;
    const groupName = (key: string) => `${prefix}-role-${key}`;
    const setRows = (change: (rows: TypedRow[]) => TypedRow[]) =>
        update((now) => {
            const committed = committedDraft(now);
            const next = change(committed.rows);
            const kept = new Set(next.map((row) => row.id));
            return {
                ...committed,
                rows: next,
                next: Math.max(
                    committed.next,
                    ...next.map((row) => row.id + 1),
                ),
                typed: Object.fromEntries(
                    Object.entries(committed.typed).filter(([id]) =>
                        kept.has(Number(id)),
                    ),
                ),
            };
        });
    const memberChoice = (now: Draft, member: MemberRow, role: Choice) => {
        const key = memberKey(member.memberships);
        const stored = addresses.members.find(
            (item) => memberKey(item.memberships) === key,
        );
        const unchanged =
            stored &&
            (role === null
                ? stored.role === null
                : stored.memberships.length === 1 && stored.role === role);
        const members = { ...now.members };
        if (unchanged) delete members[key];
        else
            members[key] = {
                memberships: member.memberships,
                address: member.address,
                role,
            };
        return { ...now, members };
    };
    const typedChoice = (
        now: Draft,
        { row, added, member }: TypedView,
        role: Choice,
    ): Draft =>
        added
            ? {
                  ...now,
                  typed: {
                      ...now.typed,
                      [row.id]: {
                          role,
                          memberships: added.memberships,
                          ...(member && {
                              member: {
                                  memberships: member.memberships,
                                  address: member.address,
                              },
                          }),
                      },
                  },
              }
            : now;
    const choose =
        (name: string, apply: (now: Draft, role: Choice) => Draft) =>
        (role: Choice) => {
            if (role === null) pendingFocus.current = name;
            update((now) => apply(now, role));
        };
    const focusSummary = (change: (draft: Draft) => Draft) => {
        flushSync(() => update(change));
        heading.current?.focus();
    };
    const withDefaults = (now: Draft): Draft => {
        let next = now;
        for (const { member, shown } of rows.members)
            if (shown?.set) next = memberChoice(next, member, null);
        for (const item of rows.typed)
            if (item.shown?.set) next = typedChoice(next, item, null);
        return next;
    };
    const anySet =
        rows.members.some((item) => item.shown?.set) ||
        rows.typed.some((item) => item.shown?.set);
    return (
        <fieldset className="reminder-recipients">
            <legend>{t("emailRecipients")}</legend>
            <span className="recipient-count fine" aria-live="polite">
                {present &&
                    t("quotaCount", {
                        count: present.recipient_count,
                        limit: present.recipient_limit,
                    })}
            </span>
            <p className="fine">{t("groupPrivacy")}</p>
            {present && present.recipient_count > present.recipient_limit && (
                <Notice>
                    {t("recipientLimitExceeded", {
                        limit: present.recipient_limit,
                    })}
                </Notice>
            )}
            {!visible && !base.hide_recipients && <StateLine status={status} />}
            {!visible && base.hide_recipients && (
                <div className={styles.review}>
                    {present ? (
                        <p>
                            {t(
                                base.hidden_by_departure
                                    ? "hiddenByDeparture"
                                    : "hiddenToday",
                            )}
                        </p>
                    ) : (
                        <StateLine status={status} />
                    )}
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
                                    {shown === undefined ? (
                                        <span className={styles.state}>
                                            {t("setUnderOtherAddresses")}
                                        </span>
                                    ) : (
                                        <Placement
                                            name={groupName(`m-${key}`)}
                                            address={member.address}
                                            shown={shown}
                                            onChoose={choose(
                                                groupName(`m-${key}`),
                                                (now, role) =>
                                                    memberChoice(
                                                        now,
                                                        member,
                                                        role,
                                                    ),
                                            )}
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
            {rows.typed.map((item, index) => {
                const { row, added, member, shown } = item;
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
                                    onChange={(event) => {
                                        const text = event.target.value;
                                        update(
                                            (now) => ({
                                                ...now,
                                                rows: now.rows.map((item) =>
                                                    item.id === row.id
                                                        ? { ...item, text }
                                                        : item,
                                                ),
                                            }),
                                            false,
                                        );
                                    }}
                                    onBlur={() => commitOnBlur(row.id)}
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
                        {present && added && !added.eligible && (
                            <p className="fine">{t("addsNoRecipient")}</p>
                        )}
                        {visible &&
                            !added &&
                            settled(row) &&
                            row.committed.trim() !== "" &&
                            status.status === "checking" && (
                                <p className="fine">{t("checkingAddress")}</p>
                            )}
                        {added && shown !== undefined && (
                            <Placement
                                name={groupName(`t-${row.id}`)}
                                address={added.address}
                                shown={shown}
                                onChoose={choose(
                                    groupName(`t-${row.id}`),
                                    (now, role) => typedChoice(now, item, role),
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
                            !added.memberships.length &&
                            visibleRole(shown.checked) && (
                                <p className={styles.warning}>
                                    {t("outsideWarning", {
                                        address: added.address,
                                    })}
                                </p>
                            )}
                    </div>
                );
            })}
            <button
                className="button secondary"
                type="button"
                onClick={() => {
                    const id = draft.next;
                    focusRow(id);
                    setRows((now) => [...now, { id, text: "", committed: "" }]);
                }}
            >
                {t("addAddress")}
            </button>
            {visible && (
                <Summary
                    unsaved={recipients.unsaved}
                    status={status}
                    heading={heading}
                    hold={hold}
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
