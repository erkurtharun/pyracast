import { rowsOf, type Row } from "./reminder-fields";
import {
    edited,
    type Addresses,
    type Answer,
    type Body,
    type Draft,
    type Target,
} from "./reminder-draft";

export const identity = (address: string) =>
    address.replace(/[A-Z]/g, (letter) => letter.toLowerCase());
export const typedIdentity = (text: string) => identity(text.trim());

const keyOf = ({ kind, address, memberships }: Target) =>
    JSON.stringify([kind, identity(address), memberships]);

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

export type Control = { target: Target; key: string; sent: boolean };

export function controlsOf(addresses: Addresses, form: Form) {
    const typed = new Map(
        addresses.added
            .filter((item) => item.eligible)
            .map((item) => [identity(item.address), item]),
    );
    const listed = new Set(form.rows.map((row) => typedIdentity(row.text)));
    const controls = new Map<string, Control>();
    const offer = (
        kind: Target["kind"],
        { address, memberships }: Omit<Target, "kind">,
        sent: boolean,
    ) => {
        const target: Target = {
            kind,
            address,
            memberships: [...memberships].sort((a, b) => a - b),
        };
        controls.set(identity(address), { target, key: keyOf(target), sent });
    };
    for (const member of addresses.members)
        if (!typed.has(identity(member.address))) offer("member", member, true);
    for (const [key, item] of typed) offer("typed", item, listed.has(key));
    return controls;
}

export function bodyOf(answer: Answer, addresses: Addresses, draft: Draft) {
    const form = formOf(answer, draft);
    const controls = controlsOf(addresses, form);
    const address_roles: NonNullable<Body["address_roles"]> = [];
    const member_roles: NonNullable<Body["member_roles"]> = [];
    for (const [key, { address, role }] of draft.decisions) {
        const control = controls.get(identity(address));
        if (control?.key !== key || !control.sent) continue;
        const { kind, ...target } = control.target;
        if (kind === "typed") address_roles.push({ ...target, role });
        if (target.memberships.length) member_roles.push({ ...target, role });
    }
    return {
        enabled: form.enabled,
        times: entered(form.times),
        language: form.language,
        include_workspace_members: form.include,
        recipients: entered(form.rows),
        hide_recipients: form.hide,
        expected_revision:
            (edited(draft) ? draft.base : undefined) ?? answer.revision,
        ...(address_roles.length > 0 && { address_roles }),
        ...(member_roles.length > 0 && { member_roles }),
    } satisfies Body;
}
