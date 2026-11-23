import type { components } from "@/lib/schema";
import type { Language } from "@/i18n/language";
import type { Row } from "./reminder-fields";

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

export type Target = {
    kind: "typed" | "member";
    address: string;
    memberships: number[];
};
type Decision = Target & { role: Choice };

export type Draft = {
    enabled?: boolean;
    times?: Row[];
    language?: Language;
    hide?: boolean;
    include?: boolean;
    rows?: Row[];
    decisions: ReadonlyMap<string, Decision>;
    base?: number;
};
export const EMPTY: Draft = { decisions: new Map() };

export const edited = ({ decisions, ...fields }: Draft) =>
    decisions.size > 0 ||
    Object.entries(fields).some(
        ([name, value]) => name !== "base" && value !== undefined,
    );

export type Outcome =
    "none" | "saved" | "changed" | "refused" | "lost" | "unknown";
export type Ending = {
    outcome: Exclude<Outcome, "none">;
    path: "saved" | "conflict" | "refusal" | "unknown";
};
export type Panel = {
    draft: Draft;
    outcome: Outcome;
    dropped: { address: string; role: Choice }[];
};
export type Desk = {
    panel: Panel;
    change: (change: (now: Panel) => Panel) => void;
    finish: (ending: Ending, sent: Draft) => void;
};
