import type { Route } from "@playwright/test";
import { expect, test, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import {
    openReminders,
    paged,
    reminderApi,
    tenantRow,
    user,
} from "./support/workspace";

const ADMIN = user.email;
const MEMBER = "member@example.test";
const OUTSIDE = "outside@example.test";
const TEAM = [ADMIN, MEMBER];

const group = (page: Page, address: string) =>
    page.getByRole("group", {
        name: `How ${address} receives the email`,
        exact: true,
    });
const radio = (page: Page, address: string, label: string) =>
    group(page, address).getByRole("radio", { name: label, exact: true });
const placement = (page: Page, address: string) =>
    group(page, address).locator("xpath=..");
const line = (page: Page, text: string) =>
    page.getByText(text, { exact: true });
const heading = (page: Page) =>
    page.getByRole("heading", { name: "Who sees what", exact: true });
const summaryLines = (page: Page) =>
    page.getByText(/^(To|Copy): \S+@|^Hidden: \d/);
const address = (page: Page, index: number) =>
    page.getByLabel(`Email address ${index}`, { exact: true });

async function type(page: Page, index: number, text: string) {
    await address(page, index).fill(text);
    await address(page, index).blur();
}

function sentWrites(page: Page) {
    const bodies: unknown[] = [];
    page.on("request", (request) => {
        if (request.method() === "PUT" && request.url().endsWith("/reminders/"))
            bodies.push(request.postDataJSON());
    });
    return bodies;
}

function gate(page: Page, url: string) {
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    const installed = page.route(url, async (route) => {
        await released;
        await route.fallback();
    });
    return { installed, release };
}

async function save(page: Page) {
    const request = page.waitForRequest(
        (sent) => sent.method() === "PUT" && sent.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    const body = (await request).postDataJSON();
    await expect(
        page.getByRole("button", { name: "Save reminders", exact: true }),
    ).toBeEnabled();
    return body;
}

async function openSettings(page: Page) {
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
}

const fits = (page: Page) =>
    page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
    );

test("a plant without a schedule opens unticked with the administrator in To, members in Copy and typed addresses Hidden", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await expect(
        page.getByLabel("Hide recipients from each other", { exact: true }),
    ).not.toBeChecked();
    await expect(radio(page, ADMIN, "To")).toBeChecked();
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    await expect(radio(page, OUTSIDE, "Hidden")).toBeChecked();
    for (const item of [ADMIN, MEMBER, OUTSIDE])
        await expect(placement(page, item)).toContainText("Default");
    await expect(
        page.getByRole("listitem").filter({ hasText: ADMIN }),
    ).toContainText("Workspace administrator");
    await expect(line(page, `To: ${ADMIN}`)).toBeVisible();
    await expect(line(page, `Copy: ${MEMBER}`)).toBeVisible();
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
    await expect(
        line(page, "New members are added with the default role."),
    ).toBeVisible();
    await expect(page.getByText("Not saved yet", { exact: false })).toHaveCount(
        0,
    );
    await expect(
        page.getByRole("button", { name: "Use the defaults for everyone" }),
    ).toHaveCount(0);
});

test("starting a visible schedule waits until who sees what is shown, while pausing one is saved at once", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        typed: { [OUTSIDE]: "cc" },
    });
    const writes = sentWrites(page);
    await openSettings(page);
    const editor = gate(page, "**/_next/static/**/*.js");
    try {
        await editor.installed;
        await page
            .getByRole("button", { name: "Email reminders", exact: true })
            .click();
        await page
            .getByLabel("Enable forecast emails for this plant", {
                exact: true,
            })
            .check();
        await page.getByRole("button", { name: "Save reminders" }).click();
        await expect(
            line(
                page,
                "Check who sees what before these reminders start, then save again.",
            ),
        ).toBeVisible();
        await expect(line(page, "Loading email reminders…")).toBeFocused();
        expect(writes).toEqual([]);
    } finally {
        editor.release();
    }
    await expect(radio(page, OUTSIDE, "Copy")).toBeChecked();
    expect(writes).toEqual([]);
    const body = await save(page);
    expect(body).toMatchObject({ enabled: true, hide_recipients: false });
    expect(body).not.toHaveProperty("address_roles");
    expect(body).not.toHaveProperty("member_roles");
});

test("pausing a visible schedule is saved while the recipients editor is still loading", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        enabled: true,
        typed: { [OUTSIDE]: "cc" },
    });
    await openSettings(page);
    const editor = gate(page, "**/_next/static/**/*.js");
    try {
        await editor.installed;
        await page
            .getByRole("button", { name: "Email reminders", exact: true })
            .click();
        await page
            .getByLabel("Enable forecast emails for this plant", {
                exact: true,
            })
            .uncheck();
        await expect(line(page, "Loading email reminders…")).toBeVisible();
        const body = await save(page);
        expect(body).toMatchObject({
            enabled: false,
            recipients: [OUTSIDE],
            include_workspace_members: true,
            hide_recipients: false,
        });
        expect(body).not.toHaveProperty("address_roles");
        expect(body).not.toHaveProperty("member_roles");
    } finally {
        editor.release();
    }
});

test("a reminders answer without addresses offers no save", async ({
    page,
}) => {
    await reminderApi(page, {
        members: TEAM,
        includeMembers: true,
        viewer: () => true,
    });
    await openReminders(page);
    await expect(
        line(
            page,
            "2 recipients of at most 5. In this app only the workspace administrator sees the addresses.",
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Save reminders" }),
    ).toHaveCount(0);
});

test("typing a hidden member's address keeps that member hidden and sends no role", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN, { membership: 2, address: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await type(page, 1, MEMBER);
    await expect(
        page.getByRole("listitem").filter({ hasText: MEMBER }),
    ).toContainText("Set under Other addresses");
    await expect(group(page, MEMBER)).toHaveCount(1);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Member");
    const body = await save(page);
    expect(body.recipients).toEqual([MEMBER]);
    expect(body).not.toHaveProperty("address_roles");
    expect(body).not.toHaveProperty("member_roles");
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(line(page, `To: ${ADMIN}`)).toBeVisible();
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
});

test("changing only the capitals of a typed hidden address keeps it hidden and sends no role", async ({
    page,
}) => {
    const spelled = "Member@Example.test";
    await reminderApi(page, {
        recipients: [MEMBER],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        typed: { [MEMBER]: "bcc" },
    });
    await openReminders(page);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await type(page, 1, spelled);
    await expect(radio(page, spelled, "Hidden")).toBeChecked();
    const body = await save(page);
    expect(body.recipients).toEqual([spelled]);
    expect(body).not.toHaveProperty("address_roles");
    expect(body).not.toHaveProperty("member_roles");
    await expect(radio(page, spelled, "Hidden")).toBeChecked();
});

test("a save that changes only a time sends no role at all", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [MEMBER],
        members: [ADMIN, { membership: 2, address: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await page.getByLabel("Delivery time 1", { exact: true }).fill("09:00");
    const body = await save(page);
    expect(body.times).toEqual(["09:00"]);
    expect(body).not.toHaveProperty("address_roles");
    expect(body).not.toHaveProperty("member_roles");
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
});

test("a spelling the server folds onto a member's address shows that member's Copy once the answer arrives", async ({
    page,
}) => {
    const STORED = "ops@straße.de";
    const TYPED = "ops@xn--strae-oqa.de";
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN, { membership: 2, address: STORED, choice: "cc" }],
        includeMembers: false,
        hidden: false,
        aliases: [[STORED, TYPED]],
    });
    await openReminders(page);
    const preview = gate(page, "**/reminders/preview/");
    try {
        await preview.installed;
        await type(page, 1, TYPED);
        await expect(line(page, "Checking this address…")).toBeVisible();
        await expect(line(page, "Checking who sees what…")).toBeVisible();
        await expect(group(page, TYPED)).toHaveCount(0);
        await expect(summaryLines(page)).toHaveCount(0);
    } finally {
        preview.release();
    }
    await expect(radio(page, TYPED, "Copy")).toBeChecked();
    await expect(placement(page, TYPED)).toContainText("Member");
    await expect(group(page, STORED)).toHaveCount(0);
    await expect(line(page, `Copy: ${TYPED}`)).toBeVisible();
    await expect(line(page, "Checking this address…")).toHaveCount(0);
});

test("a new address held by two members is chosen with both, an inactive address is in no count and an unchanged save sends no role", async ({
    page,
}) => {
    const SHARED = "shared@example.test";
    const GONE = "gone@example.test";
    await reminderApi(page, {
        recipients: [GONE],
        members: [
            ADMIN,
            { membership: 2, address: SHARED },
            { membership: 3, address: SHARED },
            { membership: 4, address: GONE, active: false },
        ],
        hidden: false,
        limit: 1,
    });
    await openReminders(page);
    await expect(
        line(page, "Not receiving: its member is not active."),
    ).toBeVisible();
    await expect(page.getByText("0 / 1", { exact: true })).toBeVisible();
    const add = page.getByRole("button", { name: "Add address", exact: true });
    await expect(add).toBeEnabled();
    await add.click();
    await type(page, 2, SHARED);
    await radio(page, SHARED, "To").check();
    await expect(page.getByText("1 / 1", { exact: true })).toBeVisible();
    expect(await save(page)).toMatchObject({
        recipients: [GONE, SHARED],
        address_roles: [{ address: SHARED, role: "to", memberships: [2, 3] }],
        member_roles: [{ memberships: [2, 3], address: SHARED, role: "to" }],
    });
    await expect(radio(page, SHARED, "To")).toBeChecked();
    await expect(placement(page, SHARED)).toContainText("Set by you");
    const unchanged = await save(page);
    expect(unchanged).not.toHaveProperty("address_roles");
    expect(unchanged).not.toHaveProperty("member_roles");
});

test("a refreshed answer of the same configuration keeps the unsaved choice, and a save sends it", async ({
    page,
}) => {
    await page.clock.install();
    const { deliveries } = await reminderApi(page, {
        recipients: [],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await radio(page, MEMBER, "Hidden").check();
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
    deliveries.unshift({
        status: "sent",
        scheduled_for: "2026-01-02T08:00:00Z",
    });
    const refreshed = page.waitForResponse(
        (response) =>
            response.request().method() === "GET" &&
            response.url().endsWith("/reminders/"),
    );
    await page.clock.fastForward("00:10");
    await page.evaluate(() =>
        window.dispatchEvent(new Event("visibilitychange")),
    );
    await refreshed;
    await page.getByText("Recent deliveries", { exact: true }).click();
    await expect(
        page
            .getByRole("table", { name: "Recent forecast email deliveries" })
            .locator("tbody tr"),
    ).toHaveCount(2);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(
        page.getByText("Not saved yet", { exact: false }),
    ).toBeVisible();
    expect(await save(page)).toMatchObject({
        member_roles: [{ memberships: [2], address: MEMBER, role: "bcc" }],
    });
    await expect(page.getByText("Not saved yet", { exact: false })).toHaveCount(
        0,
    );
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
});

test("a blank row and an unchanged save leave nothing unsaved", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await page
        .getByRole("button", { name: "Add address", exact: true })
        .click();
    await expect(page.getByText("Not saved yet", { exact: false })).toHaveCount(
        0,
    );
    expect((await save(page)).recipients).toEqual([OUTSIDE]);
    await expect(page.getByText("Not saved yet", { exact: false })).toHaveCount(
        0,
    );
});

test("a saved To answered as resting reads as the default, is counted Hidden and leaves nothing unsaved", async ({
    page,
}) => {
    const { persons } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: [ADMIN],
        hidden: false,
        typed: { [OUTSIDE]: "to" },
    });
    await openReminders(page);
    await expect(radio(page, OUTSIDE, "To")).toBeChecked();
    await expect(placement(page, OUTSIDE)).toContainText("Set by you");
    persons.push({ membership: 3, address: OUTSIDE });
    await page.getByLabel("Delivery time 1", { exact: true }).fill("09:00");
    const body = await save(page);
    expect(body).not.toHaveProperty("address_roles");
    await expect(radio(page, OUTSIDE, "Hidden")).toBeChecked();
    await expect(placement(page, OUTSIDE)).toContainText("Default");
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
    await expect(page.getByText("Not saved yet", { exact: false })).toHaveCount(
        0,
    );
});

test("the same outside address twice shows the server's refusal and no placement", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN],
        hidden: false,
    });
    const messages: string[] = [];
    page.on("console", (message) => messages.push(message.text()));
    await openReminders(page);
    await type(page, 1, OUTSIDE);
    await expect(radio(page, OUTSIDE, "Hidden")).toBeChecked();
    await page
        .getByRole("button", { name: "Add address", exact: true })
        .click();
    await type(page, 2, OUTSIDE);
    await expect(
        line(page, "Recipient addresses must be distinct (case-insensitive)."),
    ).toBeVisible();
    await expect(group(page, OUTSIDE)).toHaveCount(0);
    await expect(summaryLines(page)).toHaveCount(0);
    expect(messages.filter((text) => text.includes(OUTSIDE))).toEqual([]);
});

test("a check that fails says so, promises no placement and can be repeated", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN],
        hidden: false,
    });
    await openReminders(page);
    const failing = (route: Route) => route.fulfill({ status: 500, body: "" });
    await page.route("**/reminders/preview/", failing);
    await type(page, 1, OUTSIDE);
    await expect(
        line(page, "Who sees what could not be checked."),
    ).toBeVisible();
    await expect(group(page, OUTSIDE)).toHaveCount(0);
    await expect(summaryLines(page)).toHaveCount(0);
    await page.unroute("**/reminders/preview/", failing);
    await page
        .getByRole("button", { name: "Check again", exact: true })
        .click();
    await expect(radio(page, OUTSIDE, "Hidden")).toBeChecked();
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
});

test("a member whose address is typed is chosen once on the typed row, in both lists, and its default clears both", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [MEMBER],
        members: [ADMIN, { membership: 2, address: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Set by you");
    await radio(page, MEMBER, "Copy").check();
    expect(await save(page)).toMatchObject({
        address_roles: [{ address: MEMBER, role: "cc", memberships: [2] }],
        member_roles: [{ memberships: [2], address: MEMBER, role: "cc" }],
    });
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    await page
        .getByRole("button", { name: `Use default for ${MEMBER}`, exact: true })
        .click();
    await expect(placement(page, MEMBER)).toContainText("Default");
    await expect(radio(page, MEMBER, "Copy")).toBeFocused();
    expect(await save(page)).toMatchObject({
        address_roles: [{ address: MEMBER, role: null, memberships: [2] }],
        member_roles: [{ memberships: [2], address: MEMBER, role: null }],
    });
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Default");
});

test("a schedule saved hidden saves hidden until its addressing is reviewed and saved", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: true,
    });
    await openReminders(page);
    const hide = page.getByLabel("Hide recipients from each other", {
        exact: true,
    });
    await expect(hide).toBeChecked();
    await expect(
        line(
            page,
            "Every recipient is hidden today. You can address the administrator directly and keep members in copy.",
        ),
    ).toBeVisible();
    await expect(
        line(
            page,
            "Everyone is in hidden copy (Bcc). No recipient sees another address.",
        ),
    ).toBeVisible();
    await expect(page.getByRole("radio")).toHaveCount(0);
    await expect(page.getByText("Who sees what")).toHaveCount(0);
    const hidden = await save(page);
    expect(hidden).toMatchObject({ hide_recipients: true });
    expect(hidden).not.toHaveProperty("address_roles");
    expect(hidden).not.toHaveProperty("member_roles");
    await page
        .getByRole("button", { name: "Review addressing", exact: true })
        .click();
    await expect(hide).not.toBeChecked();
    await expect(heading(page)).toBeFocused();
    await expect(line(page, `To: ${ADMIN}`)).toBeVisible();
    await expect(line(page, `Copy: ${MEMBER}`)).toBeVisible();
    await expect(
        line(
            page,
            "Not saved yet. Saving shows the To and Copy addresses to every recipient.",
        ),
    ).toBeVisible();
    const visible = await save(page);
    expect(visible).toMatchObject({ hide_recipients: false });
    expect(visible).not.toHaveProperty("member_roles");
    await expect(hide).not.toBeChecked();
    await expect(page.getByText("Not saved yet", { exact: false })).toHaveCount(
        0,
    );
    await expect(
        page.getByRole("button", { name: "Review addressing" }),
    ).toHaveCount(0);
});

test("a schedule hidden by a departure says so, keeps the choices and is reviewed like any hidden schedule", async ({
    page,
}) => {
    const notice =
        "A member left the workspace, so every recipient is hidden again. Your choices are kept.";
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN, { membership: 2, address: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hiddenByDeparture: true,
    });
    await openReminders(page);
    await expect(line(page, notice)).toBeVisible();
    await save(page);
    await expect(line(page, notice)).toHaveCount(0);
    await expect(
        line(
            page,
            "Every recipient is hidden today. You can address the administrator directly and keep members in copy.",
        ),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Review addressing", exact: true })
        .click();
    await expect(heading(page)).toBeFocused();
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Set by you");
    await expect(line(page, `To: ${ADMIN}`)).toBeVisible();
});

test("a chosen segment is sent and reads as set, and each way back returns to the default", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await radio(page, OUTSIDE, "Copy").check();
    await expect(placement(page, OUTSIDE)).toContainText("Set by you");
    await expect(
        line(
            page,
            `${OUTSIDE} does not belong to a workspace member and will be visible to every recipient.`,
        ),
    ).toHaveCount(2);
    await radio(page, MEMBER, "Hidden").check();
    expect(await save(page)).toMatchObject({
        address_roles: [{ address: OUTSIDE, role: "cc", memberships: [] }],
        member_roles: [{ memberships: [2], address: MEMBER, role: "bcc" }],
    });
    await expect(radio(page, OUTSIDE, "Copy")).toBeChecked();
    await expect(placement(page, OUTSIDE)).toContainText("Set by you");
    await page
        .getByRole("button", {
            name: `Use default for ${OUTSIDE}`,
            exact: true,
        })
        .click();
    await expect(radio(page, OUTSIDE, "Hidden")).toBeChecked();
    await expect(radio(page, OUTSIDE, "Hidden")).toBeFocused();
    await expect(placement(page, OUTSIDE)).toContainText("Default");
    await expect(placement(page, MEMBER)).toContainText("Set by you");
    await page
        .getByRole("button", {
            name: "Use the defaults for everyone",
            exact: true,
        })
        .click();
    await expect(heading(page)).toBeFocused();
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Default");
    await expect(
        page.getByRole("button", { name: "Use the defaults for everyone" }),
    ).toHaveCount(0);
    expect(await save(page)).toMatchObject({
        address_roles: [{ address: OUTSIDE, role: null, memberships: [] }],
        member_roles: [{ memberships: [2], address: MEMBER, role: null }],
    });
    await radio(page, OUTSIDE, "To").check();
    await type(page, 1, "other@example.test");
    await expect(radio(page, "other@example.test", "Hidden")).toBeChecked();
    await expect(placement(page, "other@example.test")).toContainText(
        "Default",
    );
    const edited = await save(page);
    expect(edited.recipients).toEqual(["other@example.test"]);
    expect(edited).not.toHaveProperty("address_roles");
});

test("a chosen Copy stays Copy with nobody in To while members at their default move to To", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await radio(page, ADMIN, "Copy").check();
    await expect(line(page, `To: ${MEMBER}`)).toBeVisible();
    await expect(radio(page, MEMBER, "To")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Default");
    await expect(line(page, `Copy: ${ADMIN}`)).toBeVisible();
    await expect(
        page.getByText("Nobody is in To", { exact: false }),
    ).toHaveCount(0);
    await radio(page, MEMBER, "Copy").check();
    await expect(line(page, `Copy: ${ADMIN}, ${MEMBER}`)).toBeVisible();
    await expect(radio(page, ADMIN, "Copy")).toBeChecked();
    await expect(
        line(page, "Nobody is in To: the email's To line is empty."),
    ).toBeVisible();
    expect(await save(page)).toMatchObject({
        member_roles: [
            { memberships: [1], address: ADMIN, role: "cc" },
            { memberships: [2], address: MEMBER, role: "cc" },
        ],
    });
    await expect(radio(page, ADMIN, "Copy")).toBeChecked();
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
});

test("the summary and the warnings follow every change, a typed member has one control and an inactive member's row is in no count", async ({
    page,
}) => {
    const GONE = "gone@example.test";
    await reminderApi(page, {
        recipients: [MEMBER, GONE],
        members: [
            ADMIN,
            MEMBER,
            { membership: 3, address: GONE, active: false },
        ],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    const memberRow = page.getByRole("listitem").filter({ hasText: MEMBER });
    await expect(memberRow).toContainText("Set under Other addresses");
    await expect(memberRow.getByRole("radio")).toHaveCount(0);
    await expect(group(page, MEMBER)).toHaveCount(1);
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    await expect(
        line(page, "Not receiving: its member is not active."),
    ).toBeVisible();
    await expect(group(page, GONE)).toHaveCount(0);
    await expect(page.getByText("2 / 5", { exact: true })).toBeVisible();
    const live = page.locator("[aria-live=polite]").filter({
        hasText: "Everyone who receives the email",
    });
    await expect(live.getByText(`To: ${ADMIN}`, { exact: true })).toBeVisible();
    await expect(
        live.getByText(`Copy: ${MEMBER}`, { exact: true }),
    ).toBeVisible();
    await expect(live.getByText(/^Hidden:/)).toHaveCount(0);
    await radio(page, MEMBER, "Hidden").check();
    await expect(
        live.getByText("Hidden: 1 address", { exact: true }),
    ).toBeVisible();
    await expect(live.getByText(/^Copy:/)).toHaveCount(0);
    await radio(page, ADMIN, "Hidden").check();
    await expect(
        live.getByText("Hidden: 2 addresses", { exact: true }),
    ).toBeVisible();
    await expect(
        live.getByText(
            "All recipients are hidden: the email shows no recipient address.",
            { exact: true },
        ),
    ).toBeVisible();
    expect(await save(page)).toMatchObject({
        recipients: [MEMBER, GONE],
        address_roles: [{ address: MEMBER, role: "bcc", memberships: [2] }],
        member_roles: [
            { memberships: [1], address: ADMIN, role: "bcc" },
            { memberships: [2], address: MEMBER, role: "bcc" },
        ],
    });
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Set by you");
});

test("a change refused after a member's address changed offers a reload that shows the new address with its choice", async ({
    page,
}) => {
    const { persons } = await reminderApi(page, {
        recipients: [],
        members: [ADMIN, { membership: 2, address: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    persons[1]!.address = "renamed@example.test";
    await radio(page, MEMBER, "Copy").check();
    await expect(
        line(page, "The email reminders changed. Reload before saving."),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Reload reminders", exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(page.locator(".notice[role=alert]")).toContainText(
        "The email reminders changed. Reload before saving.",
    );
    await page
        .getByRole("button", { name: "Reload reminders", exact: true })
        .click();
    await expect(radio(page, "renamed@example.test", "Hidden")).toBeChecked();
    await expect(placement(page, "renamed@example.test")).toContainText(
        "Set by you",
    );
    await expect(group(page, MEMBER)).toHaveCount(0);
});

test("the history shows the roles recorded for sent and uncertain deliveries to the administrator only, whatever changed since", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        deliveries: [
            { status: "sent" },
            {
                status: "uncertain",
                sent_at: null,
                last_error: "mail_outcome_uncertain",
            },
            { status: "failed", sent_at: null },
            { status: "sent", recipient_roles: null },
        ],
    });
    await openReminders(page);
    await radio(page, MEMBER, "Hidden").check();
    await save(page);
    await page.getByText("Recent deliveries", { exact: true }).click();
    const recipients = page
        .getByRole("table", { name: "Recent forecast email deliveries" })
        .locator("tbody tr td:nth-child(2)");
    await expect(recipients).toHaveText([
        `${ADMIN} To${MEMBER} Copy`,
        `${ADMIN} To${MEMBER} Copy`,
        `${ADMIN}${MEMBER}`,
        `${ADMIN}${MEMBER}`,
    ]);
});

test("the mock refuses before it changes anything, keeps the revision of an unchanged save and keeps recorded history", async ({
    page,
}) => {
    let viewing = false;
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        viewer: () => viewing,
    });
    await page.goto("/app/");
    const url = "/api/tenants/1/regions/1/reminders/";
    const call = (method: string, body?: object) =>
        page.evaluate(
            async ({ url, method, body }) => {
                const response = await fetch(url, {
                    method,
                    headers: { "content-type": "application/json" },
                    body: body && JSON.stringify(body),
                });
                return { status: response.status, json: await response.json() };
            },
            { url, method, body },
        );
    const saved = {
        enabled: false,
        include_workspace_members: true,
        language: "en",
        recipients: [OUTSIDE],
        times: ["08:00"],
        expected_revision: 0,
        hide_recipients: false,
    };
    const before = (await call("GET")).json;
    expect((await call("PUT", saved)).json.revision).toBe(0);
    for (const refused of [
        {
            ...saved,
            address_roles: [
                { address: "else@example.test", role: "to", memberships: [] },
            ],
        },
        {
            ...saved,
            member_roles: [{ memberships: [0], address: MEMBER, role: "to" }],
        },
        {
            ...saved,
            member_roles: [{ memberships: [3], address: MEMBER, role: "bcc" }],
        },
        { ...saved, recipients: [OUTSIDE, "OUTSIDE@example.test"] },
    ])
        expect((await call("PUT", refused)).status).not.toBe(200);
    viewing = true;
    expect(
        (await call("PUT", { ...saved, include_workspace_members: false }))
            .status,
    ).toBe(403);
    viewing = false;
    expect((await call("GET")).json).toEqual(before);
    const changed = await call("PUT", {
        ...saved,
        member_roles: [{ memberships: [2], address: MEMBER, role: "bcc" }],
    });
    expect(changed.json.revision).toBe(1);
    expect(changed.json.addresses.members[1].placement).toBe("bcc");
    expect(changed.json.deliveries).toEqual(before.deliveries);
});

for (const [hidden, mode] of [
    [true, "Recipients cannot see each other's addresses."],
    [
        false,
        "Recipients may see each other's addresses in the email; the workspace administrator decides.",
    ],
] as const) {
    test(`a viewer reads the count and the mode line but no address or role when hidden is ${hidden}`, async ({
        page,
    }) => {
        await reminderApi(page, {
            recipients: [],
            members: TEAM,
            includeMembers: true,
            hidden,
            viewer: () => true,
        });
        await page.route("**/api/tenants/", (route) =>
            route.fulfill({
                json: paged([
                    tenantRow({
                        id: 1,
                        name: "Sunfield Energy",
                        role: "viewer",
                    }),
                ]),
            }),
        );
        await openReminders(page);
        await expect(
            line(
                page,
                "2 recipients of at most 5. In this app only the workspace administrator sees the addresses.",
            ),
        ).toBeVisible();
        await expect(line(page, mode)).toBeVisible();
        await expect(page.getByRole("radio")).toHaveCount(0);
        await page.getByText("Recent deliveries", { exact: true }).click();
        await expect(
            page.getByRole("cell", { name: "2 recipients", exact: true }),
        ).toBeVisible();
        await expect(page.getByText(MEMBER)).toHaveCount(0);
        await expect(page.getByText("Copy", { exact: true })).toHaveCount(0);
    });
}

test("keyboard users reach each group at its checked segment and change it with the arrows", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await page.getByLabel("Include workspace members", { exact: true }).focus();
    await page.keyboard.press("Tab");
    await expect(radio(page, ADMIN, "To")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(radio(page, MEMBER, "Copy")).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(radio(page, MEMBER, "Hidden")).toBeFocused();
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(line(page, "Hidden: 2 addresses")).toBeVisible();
    await page.keyboard.press("Tab");
    await expect(
        page.getByRole("button", { name: `Use default for ${MEMBER}` }),
    ).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(address(page, 1)).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(radio(page, OUTSIDE, "Hidden")).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(radio(page, OUTSIDE, "Copy")).toBeChecked();
    await expect(line(page, `Copy: ${OUTSIDE}`)).toBeVisible();
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
});

test("the section has no horizontal scroll at 320px and no accessibility violation at 390px", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [
            OUTSIDE,
            "a.very.long.recipient.address@subdomain.example.test",
        ],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await page.setViewportSize({ width: 320, height: 900 });
    await openReminders(page);
    await radio(page, OUTSIDE, "Copy").check();
    await radio(page, ADMIN, "Copy").check();
    await expect(
        page.getByText("does not belong", { exact: false }).first(),
    ).toBeVisible();
    await expect(line(page, `To: ${MEMBER}`)).toBeVisible();
    expect(await fits(page)).toBe(true);
    const outside = await page
        .getByRole("region", { name: "Email reminders" })
        .locator("input:visible, button:visible")
        .evaluateAll((controls) =>
            controls
                .filter((control) => {
                    const box = control.getBoundingClientRect();
                    return box.left < -1 || box.right > innerWidth + 1;
                })
                .map((control) => control.outerHTML.slice(0, 80)),
        );
    expect(outside).toEqual([]);
    const segment = await radio(page, ADMIN, "To")
        .locator("xpath=..")
        .boundingBox();
    expect(segment!.height).toBeGreaterThanOrEqual(44);
    await page.setViewportSize({ width: 390, height: 900 });
    expect(await accessibilityViolations(page)).toEqual([]);
});

test("the section reads in Turkish with every text of the screen", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: true,
    });
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { user, language: "tr", csrf_token: "signed-token" },
        }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Işınım", exact: true }).click();
    await page.getByRole("button", { name: "Ayarlar", exact: true }).click();
    await page
        .getByRole("button", { name: "E-posta hatırlatıcıları", exact: true })
        .click();
    await expect(
        page.getByLabel("Alıcıları birbirinden gizle", { exact: true }),
    ).toBeChecked();
    await page
        .getByRole("button", { name: "Adreslemeyi gözden geçir", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Kim neyi görür", exact: true }),
    ).toBeFocused();
    const turkish = page.getByRole("group", {
        name: `${OUTSIDE} e-postayı nasıl alır`,
        exact: true,
    });
    await expect(
        turkish.getByRole("radio", { name: "Gizli", exact: true }),
    ).toBeChecked();
    await turkish.getByRole("radio", { name: "Bilgi", exact: true }).check();
    await expect(turkish.locator("xpath=..")).toContainText("Sizin seçiminiz");
    await expect(line(page, `Kime: ${ADMIN}`)).toBeVisible();
    await expect(line(page, `Bilgi: ${MEMBER}, ${OUTSIDE}`)).toBeVisible();
    await expect(
        line(
            page,
            "Henüz kaydedilmedi. Kaydedince Kime ve Bilgi adreslerini her alıcı görür.",
        ),
    ).toBeVisible();
    await expect(
        line(
            page,
            `${OUTSIDE} bir çalışma alanı üyesine ait değil ve her alıcıya görünecek.`,
        ),
    ).toHaveCount(2);
    await expect(
        line(page, "Yeni üyeler varsayılan rolle eklenir."),
    ).toBeVisible();
});
