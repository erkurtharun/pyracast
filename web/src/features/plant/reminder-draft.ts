import type { components } from "@/lib/schema";

type Schemas = components["schemas"];
export type Answer = Schemas["ReminderOutput"];
export type Addresses = Schemas["ReminderAddressesOutput"];
export type Role = Schemas["RecipientRoleEnum"];
export type Choice = Role | null;
type MemberRole = Schemas["MemberRoleInput"];
export type Addressing = Pick<
    Schemas["ReminderInput"],
    | "recipients"
    | "include_workspace_members"
    | "hide_recipients"
    | "address_roles"
    | "member_roles"
>;

export type TypedRow = { id: number; text: string };
type TypedDecision = {
    role: Choice;
    memberships: number[];
    member?: { memberships: number[]; address: string };
};
export type Draft = {
    hide: boolean;
    include: boolean;
    rows: TypedRow[];
    members: Record<string, MemberRole>;
    typed: Record<number, TypedDecision>;
};

export const memberKey = (memberships: number[]) => memberships.join(",");

export function initialDraft(answer: Answer, addresses: Addresses): Draft {
    const rows = addresses.recipients.map((text, id) => ({ id, text }));
    return {
        hide: answer.hide_recipients,
        include: answer.include_workspace_members,
        rows: rows.length ? rows : [{ id: 0, text: "" }],
        members: {},
        typed: {},
    };
}

export const filled = (rows: TypedRow[]) =>
    rows.filter((row) => row.text.trim());

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
                address: row.text.trim(),
                role: decision.role,
                memberships: decision.memberships,
            },
        ];
    });
    return {
        recipients: rows.map((row) => row.text.trim()),
        include_workspace_members: draft.include,
        hide_recipients: draft.hide,
        ...(address_roles.length > 0 && { address_roles }),
        ...(members.size > 0 && { member_roles: [...members.values()] }),
    };
}

export const sameAddressing = (left: Draft, right: Draft) =>
    JSON.stringify(addressingOf(left)) === JSON.stringify(addressingOf(right));

export function withTexts(draft: Draft, texts: string[]): Draft {
    if (texts.length !== draft.rows.length) return draft;
    const edited = draft.rows.filter(
        (row, index) => texts[index]!.trim() !== row.text.trim(),
    );
    if (!edited.length) return draft;
    const typed = { ...draft.typed };
    for (const row of edited) delete typed[row.id];
    return {
        ...draft,
        rows: draft.rows.map((row, index) => ({ ...row, text: texts[index]! })),
        typed,
    };
}
