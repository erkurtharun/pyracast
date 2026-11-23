import type { components } from "@/lib/schema";

type Schemas = components["schemas"];
export type Role = Schemas["RecipientRoleEnum"];
export type Choice = Role | null;
export const ROLES = ["to", "cc", "bcc"] as const satisfies readonly Role[];

export type MemberAnswer = {
    membership: number;
    address: string;
    kind: Schemas["ReminderMemberKindEnum"];
    role: Choice;
};
export type AddedAnswer = {
    address: string;
    role: Choice;
    eligible: boolean;
    memberships: number[];
};
export type AddressesAnswer = {
    recipients: string[];
    workspace_recipients: string[];
    members: MemberAnswer[];
    added: AddedAnswer[];
};
export type ReminderAnswer = Omit<Schemas["ReminderOutput"], "addresses"> & {
    addresses: AddressesAnswer | null;
};
export type AddressRole = {
    address: string;
    role: Role;
    memberships: number[];
};
export type MemberRole = { membership: number; address: string; role: Choice };

type TypedRow = { id: number; text: string; choice: Choice };
export type Section = {
    hide: boolean;
    include: boolean;
    rows: TypedRow[];
    members: Record<number, Choice>;
};

type MemberView =
    | { member: MemberAnswer; typed: true }
    | { member: MemberAnswer; typed: false; role: Role; set: boolean };
type TypedView = {
    row: TypedRow;
    address: string;
    eligible: boolean;
    role: Role | null;
    set: boolean;
};
export type Addressing = {
    members: MemberView[];
    typed: TypedView[];
    to: string[];
    cc: string[];
    hidden: number;
    outside: string[];
    noTo: boolean;
    allHidden: boolean;
};

type Candidate = {
    address: string;
    explicit: Choice;
    member: MemberAnswer | undefined;
};

const addressKey = (address: string) => address.trim().toLowerCase();

export function initialSection(
    answer: ReminderAnswer,
    addresses: AddressesAnswer,
): Section {
    const rows = addresses.added.map((entry, id) => ({
        id,
        text: entry.address,
        choice: entry.role,
    }));
    return {
        hide: answer.hide_recipients,
        include: answer.include_workspace_members,
        rows: rows.length ? rows : [{ id: 0, text: "", choice: null }],
        members: Object.fromEntries(
            addresses.members.map((member) => [member.membership, member.role]),
        ),
    };
}

function comparable(section: Section) {
    return JSON.stringify([
        section.hide,
        section.include,
        section.rows.map((row) => [row.text, row.choice]),
        section.members,
    ]);
}

export function unsaved(section: Section, saved: Section) {
    return comparable(section) !== comparable(saved);
}

function answered(text: string, addresses: AddressesAnswer) {
    const key = addressKey(text);
    return addresses.added.find((entry) => addressKey(entry.address) === key);
}

function memberOf(text: string, addresses: AddressesAnswer) {
    const key = addressKey(text);
    const entry = answered(text, addresses);
    return addresses.members.find(
        (member) =>
            addressKey(member.address) === key ||
            entry?.memberships.includes(member.membership),
    );
}

function membershipsOf(text: string, addresses: AddressesAnswer) {
    const entry = answered(text, addresses);
    if (entry) return entry.memberships;
    const member = memberOf(text, addresses);
    return member ? [member.membership] : [];
}

function place(
    candidates: Candidate[],
    { hide, include }: { hide: boolean; include: boolean },
): Role[] {
    if (hide) return candidates.map(() => "bcc");
    const placed = candidates.map(({ explicit, member }) => {
        if (explicit) return explicit;
        if (!include || !member) return "bcc";
        return member.kind === "administrator" ? "to" : null;
    });
    const fallback = placed.includes("to") ? "cc" : "to";
    return placed.map((role) => role ?? fallback);
}

export function addressing(
    section: Section,
    addresses: AddressesAnswer,
): Addressing {
    const typedKeys = new Set(
        section.rows
            .filter((row) => row.text.trim())
            .map((row) => addressKey(row.text)),
    );
    const members = section.include ? addresses.members : [];
    const included = members.filter(
        (member) => !typedKeys.has(addressKey(member.address)),
    );
    const typed = section.rows.map((row) => ({
        row,
        address: row.text.trim(),
        eligible: answered(row.text, addresses)?.eligible !== false,
    }));
    const listed = typed.filter((item) => item.address && item.eligible);
    const candidates: Candidate[] = [
        ...included.map((member) => ({
            address: member.address,
            explicit: section.members[member.membership] ?? null,
            member,
        })),
        ...listed.map(({ row, address }) => ({
            address,
            explicit: row.choice,
            member: memberOf(row.text, addresses),
        })),
    ];
    const roles = place(candidates, section);
    const placed = new Map<object, Role>([
        ...included.map((member, index) => [member, roles[index]!] as const),
        ...listed.map(
            (item, index) => [item, roles[included.length + index]!] as const,
        ),
    ]);
    const shown = (role: Role) =>
        candidates.filter((_, index) => roles[index] === role);
    const to = shown("to").map((item) => item.address);
    const cc = shown("cc").map((item) => item.address);
    return {
        members: members.map((member): MemberView => {
            const role = placed.get(member);
            return role
                ? {
                      member,
                      typed: false,
                      role,
                      set: section.members[member.membership] != null,
                  }
                : { member, typed: true };
        }),
        typed: typed.map((item) => ({
            ...item,
            role: placed.get(item) ?? null,
            set: item.row.choice !== null,
        })),
        to,
        cc,
        hidden: candidates.length - to.length - cc.length,
        outside: [...shown("to"), ...shown("cc")]
            .filter((item) => !item.member)
            .map((item) => item.address),
        noTo: !to.length && cc.length > 0,
        allHidden: candidates.length > 0 && !to.length && !cc.length,
    };
}

const controlled = (view: Addressing) => ({
    rows: new Set(
        view.typed.filter((item) => item.role).map((item) => item.row.id),
    ),
    members: new Set(
        view.members
            .filter((item) => !item.typed)
            .map((item) => item.member.membership),
    ),
});

export function chosen(section: Section, view: Addressing) {
    const shown = controlled(view);
    return (
        section.rows.some((row) => shown.rows.has(row.id) && row.choice) ||
        [...shown.members].some((membership) => section.members[membership])
    );
}

export function withDefaults(section: Section, view: Addressing): Section {
    const shown = controlled(view);
    return {
        ...section,
        rows: section.rows.map((row) =>
            shown.rows.has(row.id) ? { ...row, choice: null } : row,
        ),
        members: Object.fromEntries(
            Object.entries(section.members).map(([membership, choice]) => [
                membership,
                shown.members.has(Number(membership)) ? null : choice,
            ]),
        ),
    };
}

export function addressingFields(
    section: Section,
    addresses: AddressesAnswer,
): {
    hide_recipients: boolean;
    address_roles: AddressRole[];
    member_roles?: MemberRole[];
} {
    const filled = section.rows.filter((row) => row.text.trim());
    const address_roles = filled.flatMap(({ text, choice }) =>
        choice
            ? [
                  {
                      address: text.trim(),
                      role: choice,
                      memberships: membershipsOf(text, addresses),
                  },
              ]
            : [],
    );
    if (section.hide) return { hide_recipients: true, address_roles };
    const typedKeys = new Set(filled.map((row) => addressKey(row.text)));
    const member_roles = addresses.members.flatMap((member) => {
        const typed = typedKeys.has(addressKey(member.address));
        if (!typed && !section.include) return [];
        return [
            {
                membership: member.membership,
                address: member.address,
                role: typed
                    ? null
                    : (section.members[member.membership] ?? null),
            },
        ];
    });
    return { hide_recipients: false, address_roles, member_roles };
}
