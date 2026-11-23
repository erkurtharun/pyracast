import { expect, test } from "@playwright/test";
import {
    addressing,
    addressingFields,
    chosen,
    initialSection,
    unsaved,
    withDefaults,
    type AddedAnswer,
    type AddressesAnswer,
    type MemberAnswer,
    type ReminderAnswer,
    type Section,
} from "../src/features/plant/reminder-addressing";

const ADMIN = "admin@example.test";
const MEMBER = "member@example.test";
const OUTSIDE = "outside@example.test";

const member = (
    membership: number,
    address: string,
    fields: Partial<MemberAnswer> = {},
): MemberAnswer => ({
    membership,
    address,
    kind: "member",
    role: null,
    ...fields,
});
const added = (
    address: string,
    fields: Partial<AddedAnswer> = {},
): AddedAnswer => ({
    address,
    role: null,
    eligible: true,
    memberships: [],
    ...fields,
});
const team = [member(1, ADMIN, { kind: "administrator" }), member(2, MEMBER)];

function answer(
    members: MemberAnswer[],
    typed: AddedAnswer[],
    fields: Partial<ReminderAnswer> = {},
): { answer: ReminderAnswer; addresses: AddressesAnswer } {
    const addresses = {
        recipients: typed.map((entry) => entry.address),
        workspace_recipients: members.map((entry) => entry.address),
        members,
        added: typed,
    };
    return {
        addresses,
        answer: {
            enabled: true,
            state: "sending",
            revision: 3,
            include_workspace_members: true,
            hide_recipients: false,
            language: "en",
            times: ["08:00"],
            recipient_limit: 10,
            recipient_count: 0,
            workspace_member_count: members.length,
            recipient_limit_exceeded: false,
            daily_reminder_limit: 10,
            addresses,
            deliveries: [],
            ...fields,
        },
    };
}

function opened(
    members: MemberAnswer[],
    typed: AddedAnswer[],
    fields: Partial<ReminderAnswer> = {},
) {
    const { answer: read, addresses } = answer(members, typed, fields);
    return { addresses, section: initialSection(read, addresses) };
}

function placed(section: Section, addresses: AddressesAnswer) {
    const view = addressing(section, addresses);
    return {
        to: view.to,
        cc: view.cc,
        hidden: view.hidden,
        members: view.members.map((item) =>
            item.typed ? "typed" : `${item.role}${item.set ? " set" : ""}`,
        ),
        typed: view.typed.map((item) =>
            item.role === null
                ? "none"
                : `${item.role}${item.set ? " set" : ""}`,
        ),
        outside: view.outside,
        noTo: view.noTo,
        allHidden: view.allHidden,
    };
}

test("step 1: the hidden mode places every recipient in Bcc whatever is chosen", () => {
    const { addresses, section } = opened(
        [member(1, ADMIN, { kind: "administrator", role: "to" })],
        [added(OUTSIDE, { role: "cc" })],
        { hide_recipients: true },
    );
    expect(placed(section, addresses)).toMatchObject({
        to: [],
        cc: [],
        hidden: 2,
        allHidden: true,
    });
});

test("step 2: Hidden chosen for a member or a typed address wins over the default", () => {
    const { addresses, section } = opened(
        [
            member(1, ADMIN, { kind: "administrator", role: "bcc" }),
            member(2, MEMBER),
        ],
        [added(OUTSIDE, { role: "bcc" })],
    );
    expect(placed(section, addresses)).toMatchObject({
        to: [MEMBER],
        cc: [],
        hidden: 2,
        members: ["bcc set", "to"],
        typed: ["bcc set"],
    });
});

test("step 3: a member's choice is placed as chosen, also for the administrator", () => {
    const { addresses, section } = opened(
        [
            member(1, ADMIN, { kind: "administrator", role: "cc" }),
            member(2, MEMBER, { role: "to" }),
        ],
        [],
    );
    expect(placed(section, addresses)).toMatchObject({
        to: [MEMBER],
        cc: [ADMIN],
        members: ["cc set", "to set"],
    });
});

test("step 4: a typed choice that counts is placed and one that rests reads as the default", () => {
    const { addresses, section } = opened(team, [
        added(OUTSIDE, { role: "to" }),
        added("resting@example.test", { role: null, memberships: [7] }),
    ]);
    expect(placed(section, addresses)).toMatchObject({
        to: [ADMIN, OUTSIDE],
        cc: [MEMBER],
        hidden: 1,
        typed: ["to set", "bcc"],
        outside: [OUTSIDE],
    });
});

test("step 5: default members follow the administrator in Copy, or go to To while nobody is", () => {
    const withAdministrator = opened(team, []);
    expect(
        placed(withAdministrator.section, withAdministrator.addresses),
    ).toMatchObject({ to: [ADMIN], cc: [MEMBER], members: ["to", "cc"] });
    const withoutAdministrator = opened(
        [member(2, MEMBER), member(3, "third@example.test")],
        [],
    );
    expect(
        placed(withoutAdministrator.section, withoutAdministrator.addresses),
    ).toMatchObject({
        to: [MEMBER, "third@example.test"],
        cc: [],
        noTo: false,
    });
});

test("step 6: a typed address without a choice is Hidden, a member's address too while members are not included", () => {
    const { addresses, section } = opened(
        team,
        [added(OUTSIDE), added(ADMIN, { memberships: [1] })],
        { include_workspace_members: false },
    );
    expect(placed(section, addresses)).toMatchObject({
        to: [],
        cc: [],
        hidden: 2,
        members: [],
        typed: ["bcc", "bcc"],
        allHidden: true,
    });
});

test("a chosen Copy with nobody in To stays Copy and warns, while default members move to To", () => {
    const alone = opened(
        [member(1, ADMIN, { kind: "administrator", role: "cc" })],
        [],
    );
    expect(placed(alone.section, alone.addresses)).toMatchObject({
        to: [],
        cc: [ADMIN],
        noTo: true,
    });
    const joined = opened(
        [
            member(1, ADMIN, { kind: "administrator", role: "cc" }),
            member(2, MEMBER),
        ],
        [],
    );
    expect(placed(joined.section, joined.addresses)).toMatchObject({
        to: [MEMBER],
        cc: [ADMIN],
        members: ["cc set", "to"],
        noTo: false,
    });
});

test("several members of one address are one row as the answer presents them", () => {
    const shared = "shared@example.test";
    const { addresses, section } = opened(
        [member(3, shared, { kind: "administrator", role: "bcc" })],
        [added("Shared@Example.test ", { role: "bcc", memberships: [3, 4] })],
    );
    expect(placed(section, addresses)).toMatchObject({
        to: [],
        hidden: 1,
        members: ["typed"],
        typed: ["bcc set"],
    });
    expect(addressingFields(section, addresses)).toEqual({
        hide_recipients: false,
        address_roles: [
            {
                address: "Shared@Example.test",
                role: "bcc",
                memberships: [3, 4],
            },
        ],
        member_roles: [{ membership: 3, address: shared, role: null }],
    });
});

test("a typed member address follows the member default while members are included", () => {
    const { addresses, section } = opened(team, [
        added(MEMBER, { memberships: [2] }),
    ]);
    expect(placed(section, addresses)).toMatchObject({
        to: [ADMIN],
        cc: [MEMBER],
        members: ["to", "typed"],
        typed: ["cc"],
        outside: [],
    });
});

test("a row whose members are all inactive receives nothing and is in no count", () => {
    const { addresses, section } = opened(team, [
        added("gone@example.test", { eligible: false, role: "to" }),
    ]);
    const view = placed(section, addresses);
    expect(view).toMatchObject({
        to: [ADMIN],
        cc: [MEMBER],
        hidden: 0,
        typed: ["none"],
    });
});

test("a blank row is no recipient and has no control", () => {
    const { addresses, section } = opened(team, []);
    expect(section.rows).toEqual([{ id: 0, text: "", choice: null }]);
    expect(placed(section, addresses).typed).toEqual(["none"]);
});

test("the save names every typed choice with its members and every member row with a control", () => {
    const { addresses, section } = opened(
        [...team, member(3, "third@example.test", { role: "cc" })],
        [
            added(OUTSIDE, { role: "cc" }),
            added("resting@example.test", { memberships: [9] }),
        ],
    );
    const edited: Section = {
        ...section,
        rows: [
            ...section.rows,
            { id: 2, text: " Third@example.test", choice: "to" },
            { id: 3, text: "new@example.test", choice: "bcc" },
        ],
        members: { ...section.members, 2: "bcc" },
    };
    expect(addressingFields(edited, addresses)).toEqual({
        hide_recipients: false,
        address_roles: [
            { address: OUTSIDE, role: "cc", memberships: [] },
            { address: "Third@example.test", role: "to", memberships: [3] },
            { address: "new@example.test", role: "bcc", memberships: [] },
        ],
        member_roles: [
            { membership: 1, address: ADMIN, role: null },
            { membership: 2, address: MEMBER, role: "bcc" },
            { membership: 3, address: "third@example.test", role: null },
        ],
    });
});

test("a hidden save keeps the typed choices and names no member", () => {
    const { addresses, section } = opened(
        [member(1, ADMIN, { kind: "administrator", role: "cc" })],
        [added(OUTSIDE, { role: "bcc" })],
        { hide_recipients: true },
    );
    expect(addressingFields(section, addresses)).toEqual({
        hide_recipients: true,
        address_roles: [{ address: OUTSIDE, role: "bcc", memberships: [] }],
    });
});

test("members not included and not typed are not named, so they keep their choice", () => {
    const { addresses, section } = opened(
        [member(1, ADMIN, { kind: "administrator", role: "cc" })],
        [added(OUTSIDE)],
        { include_workspace_members: false },
    );
    expect(addressingFields(section, addresses).member_roles).toEqual([]);
});

test("the section differs from what was saved until every change is undone", () => {
    const { section } = opened(team, [added(OUTSIDE)]);
    expect(unsaved(section, section)).toBe(false);
    const changed: Section = {
        ...section,
        members: { ...section.members, 2: "bcc" },
    };
    expect(unsaved(changed, section)).toBe(true);
    expect(
        unsaved(
            { ...changed, members: { ...changed.members, 2: null } },
            section,
        ),
    ).toBe(false);
    expect(unsaved({ ...section, hide: true }, section)).toBe(true);
    expect(unsaved({ ...section, rows: [] }, section)).toBe(true);
});

test("the defaults for everyone clear only the choices that show a control", () => {
    const { addresses, section } = opened(
        [
            member(1, ADMIN, { kind: "administrator", role: "cc" }),
            member(2, MEMBER, { role: "bcc" }),
        ],
        [
            added(OUTSIDE, { role: "to" }),
            added("gone@example.test", { role: "bcc", eligible: false }),
            added(MEMBER, { role: "bcc", memberships: [2] }),
        ],
    );
    const view = addressing(section, addresses);
    expect(chosen(section, view)).toBe(true);
    const reset = withDefaults(section, view);
    expect(reset.rows.map((row) => row.choice)).toEqual([null, "bcc", null]);
    expect(reset.members).toEqual({ 1: null, 2: "bcc" });
    expect(chosen(reset, addressing(reset, addresses))).toBe(false);
});
