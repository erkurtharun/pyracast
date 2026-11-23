import type { components } from "@/lib/schema";
import type { Language } from "@/i18n/language";
import { rowsOf, type Row } from "./reminder-fields";

type Schemas = components["schemas"];
export type Answer = Schemas["ReminderOutput"];
export type Addresses = Schemas["ReminderAddressesOutput"];
export type Role = Schemas["RecipientRoleEnum"];
export type Choice = Role | null;
export type Body = Schemas["ReminderInput"];
// The backend schema does not carry the preview's digest of the To and Copy it shows, or the refusal of a save whose digest differs, yet.
export type Previewed = Answer & { disclosure: string };
export type Confirmed = Body & { disclosure: string };
export const DISCLOSURE_CHANGED = "reminder_disclosure_changed";

export type Draft = {
    enabled?: boolean;
    times?: Row[];
    language?: Language;
    hide?: boolean;
    include?: boolean;
    rows?: Row[];
    choices: Record<string, Choice>;
    removed?: string[];
};
export const EMPTY: Draft = { choices: {} };

export const identity = (address: string) => address.trim().toLowerCase();

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

export function bodyOf(answer: Answer, addresses: Addresses, draft: Draft) {
    const form = formOf(answer, draft);
    const recipients = entered(form.rows);
    const listed = new Set(recipients.map(identity));
    const address_roles: NonNullable<Body["address_roles"]> = [];
    const member_roles: NonNullable<Body["member_roles"]> = [];
    for (const [key, role] of Object.entries(draft.choices)) {
        const added = addresses.added.find(
            (item) => identity(item.address) === key,
        );
        if (added && listed.has(key))
            address_roles.push({
                address: added.address,
                role,
                memberships: added.memberships,
            });
        const member = addresses.members.find(
            (item) => identity(item.address) === key,
        );
        if (member)
            member_roles.push({
                memberships: member.memberships,
                address: member.address,
                role,
            });
    }
    const body: Body = {
        enabled: form.enabled,
        times: entered(form.times),
        language: form.language,
        include_workspace_members: form.include,
        recipients,
        hide_recipients: form.hide,
        expected_revision: answer.revision,
        ...(address_roles.length > 0 && { address_roles }),
        ...(member_roles.length > 0 && { member_roles }),
    };
    return { body, saved: { ...form, choices: draft.choices } };
}
