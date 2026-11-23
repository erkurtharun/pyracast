import { rowsOf, type Row } from "./reminder-fields";
import type {
    Addresses,
    Answer,
    Body,
    Decision,
    Draft,
    Target,
} from "./reminder-draft";

export const identity = (address: string) => address.trim().toLowerCase();

export const keyOf = ({ kind, address, memberships }: Target) =>
    JSON.stringify([kind, identity(address), memberships]);

const sorted = (ids: number[]) => [...ids].sort((a, b) => a - b);

export const typedTarget = (item: Addresses["added"][number]): Target => ({
    kind: "typed",
    address: item.address,
    memberships: sorted(item.memberships),
});

export const memberTarget = (member: Addresses["members"][number]): Target => ({
    kind: "member",
    address: member.address,
    memberships: sorted(member.memberships),
});

function offered(addresses: Addresses) {
    const targets = [
        ...addresses.added.filter((item) => item.eligible).map(typedTarget),
        ...addresses.members.map(memberTarget),
    ];
    return new Map(targets.map((target) => [keyOf(target), target]));
}

export const stale = (
    addresses: Addresses,
    decisions: ReadonlyMap<string, Decision>,
) => {
    const targets = offered(addresses);
    return [...decisions.keys()].filter((key) => !targets.has(key));
};

const entered = (rows: Row[]) =>
    rows.map((row) => row.text.trim()).filter(Boolean);

export function formOf(answer: Answer, draft: Draft) {
    return {
        enabled: draft.enabled ?? answer.enabled,
        times: draft.times ?? rowsOf(answer.times),
        language: draft.language ?? answer.language,
        hide: draft.hide ?? answer.hide_recipients,
        include: draft.include ?? answer.include_workspace_members,
        rows: draft.rows ?? rowsOf(answer.addresses?.recipients ?? []),
    };
}
export type Form = ReturnType<typeof formOf>;

export function typedRows(addresses: Addresses, form: Form) {
    const eligible = new Set(
        addresses.added
            .filter((item) => item.eligible)
            .map((item) => identity(item.address)),
    );
    return new Set(
        form.rows
            .map((row) => identity(row.text))
            .filter((key) => eligible.has(key)),
    );
}

export function bodyOf(answer: Answer, addresses: Addresses, draft: Draft) {
    const form = formOf(answer, draft);
    const recipients = entered(form.rows);
    const typed = typedRows(addresses, form);
    const targets = offered(addresses);
    const address_roles: NonNullable<Body["address_roles"]> = [];
    const member_roles: NonNullable<Body["member_roles"]> = [];
    for (const [key, { role }] of draft.decisions) {
        const target = targets.get(key);
        if (!target) continue;
        const { address, memberships } = target;
        const listed = typed.has(identity(address));
        if (target.kind === "typed" && listed) {
            address_roles.push({ address, role, memberships });
            if (memberships.length)
                member_roles.push({ memberships, address, role });
        } else if (target.kind === "member" && !listed)
            member_roles.push({ memberships, address, role });
    }
    return {
        enabled: form.enabled,
        times: entered(form.times),
        language: form.language,
        include_workspace_members: form.include,
        recipients,
        hide_recipients: form.hide,
        expected_revision: answer.revision,
        ...(address_roles.length > 0 && { address_roles }),
        ...(member_roles.length > 0 && { member_roles }),
    } satisfies Body;
}
