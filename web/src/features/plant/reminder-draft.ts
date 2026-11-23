import type { components } from "@/lib/schema";

type Schemas = components["schemas"];
export type Answer = Schemas["ReminderOutput"];
export type Addresses = Schemas["ReminderAddressesOutput"];
export type AddedRow = Schemas["AddedAddressOutput"];
export type MemberRow = Schemas["MemberAddressOutput"];
export type Role = Schemas["RecipientRoleEnum"];
export type Choice = Role | null;
type MemberRole = Schemas["MemberRoleInput"];
type Addressing = Pick<
    Schemas["ReminderInput"],
    | "recipients"
    | "include_workspace_members"
    | "hide_recipients"
    | "address_roles"
    | "member_roles"
>;

export type TypedRow = { id: number; text: string; committed: string };
type TypedDecision = {
    role: Choice;
    memberships: number[];
    member?: { memberships: number[]; address: string };
};
export type Draft = {
    hide: boolean;
    include: boolean;
    rows: TypedRow[];
    next: number;
    members: Record<string, MemberRole>;
    typed: Record<number, TypedDecision>;
};

export const memberKey = (memberships: number[]) => memberships.join(",");

export function initialDraft(answer: Answer, addresses: Addresses): Draft {
    const rows = addresses.recipients.map((text, id) => ({
        id,
        text,
        committed: text,
    }));
    return {
        hide: answer.hide_recipients,
        include: answer.include_workspace_members,
        rows: rows.length ? rows : [{ id: 0, text: "", committed: "" }],
        next: Math.max(rows.length, 1),
        members: {},
        typed: {},
    };
}

export const filled = (rows: TypedRow[]) =>
    rows.filter((row) => row.committed.trim());

export const settled = (row: TypedRow) =>
    row.text.trim() === row.committed.trim();

export function addressingOf(draft: Draft): Addressing {
    const rows = filled(draft.rows);
    const members = new Map(
        Object.values(draft.members).map((entry) => [
            memberKey(entry.memberships),
            entry,
        ]),
    );
    const address_roles = rows.flatMap((row) => {
        const decision = draft.typed[row.id];
        if (!decision) return [];
        if (decision.member)
            members.set(memberKey(decision.member.memberships), {
                ...decision.member,
                role: decision.role,
            });
        return [
            {
                address: row.committed.trim(),
                role: decision.role,
                memberships: decision.memberships,
            },
        ];
    });
    return {
        recipients: rows.map((row) => row.committed.trim()),
        include_workspace_members: draft.include,
        hide_recipients: draft.hide,
        ...(address_roles.length > 0 && { address_roles }),
        ...(members.size > 0 && { member_roles: [...members.values()] }),
    };
}

export const sameAddressing = (left: Draft, right: Draft) =>
    JSON.stringify(addressingOf(left)) === JSON.stringify(addressingOf(right));

export function committedDraft(draft: Draft, only?: number): Draft {
    const changed = draft.rows.filter(
        (row) => !settled(row) && (only === undefined || row.id === only),
    );
    if (!changed.length) return draft;
    const typed = { ...draft.typed };
    for (const row of changed) delete typed[row.id];
    return {
        ...draft,
        rows: draft.rows.map((row) =>
            changed.includes(row) ? { ...row, committed: row.text } : row,
        ),
        typed,
    };
}
