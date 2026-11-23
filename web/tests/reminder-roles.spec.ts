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
    });
    await openReminders(page);
    await expect(
        page.getByLabel("Hide recipients from each other", { exact: true }),
    ).not.toBeChecked();
    await expect(radio(page, ADMIN, "To")).toBeChecked();
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    await expect(radio(page, OUTSIDE, "Hidden")).toBeChecked();
    for (const address of [ADMIN, MEMBER, OUTSIDE])
        await expect(placement(page, address)).toContainText("Default");
    await expect(
        page.getByRole("listitem").filter({ hasText: ADMIN }),
    ).toContainText("Workspace administrator");
    await expect(line(page, `To: ${ADMIN}`)).toBeVisible();
    await expect(line(page, `Copy: ${MEMBER}`)).toBeVisible();
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
    await expect(page.getByText("Not saved yet", { exact: false })).toHaveCount(
        0,
    );
    await expect(
        page.getByRole("button", { name: "Use the defaults for everyone" }),
    ).toHaveCount(0);
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
    expect(hidden).toMatchObject({ hide_recipients: true, address_roles: [] });
    expect(hidden).not.toHaveProperty("member_roles");
    await page
        .getByRole("button", { name: "Review addressing", exact: true })
        .click();
    await expect(hide).not.toBeChecked();
    await expect(
        page.getByRole("heading", { name: "Who sees what", exact: true }),
    ).toBeFocused();
    await expect(line(page, `To: ${ADMIN}`)).toBeVisible();
    await expect(line(page, `Copy: ${MEMBER}`)).toBeVisible();
    await expect(
        line(
            page,
            "Not saved yet. Saving shows the To and Copy addresses to every recipient.",
        ),
    ).toBeVisible();
    expect(await save(page)).toMatchObject({
        hide_recipients: false,
        address_roles: [],
        member_roles: [
            { membership: 1, address: ADMIN, role: null },
            { membership: 2, address: MEMBER, role: null },
        ],
    });
    await expect(hide).not.toBeChecked();
    await expect(page.getByText("Not saved yet", { exact: false })).toHaveCount(
        0,
    );
    await expect(
        page.getByRole("button", { name: "Review addressing" }),
    ).toHaveCount(0);
});

test("a chosen segment is sent and reads as set, and each way back returns to the default", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
    });
    await openReminders(page);
    await radio(page, OUTSIDE, "Copy").check();
    await expect(placement(page, OUTSIDE)).toContainText("Set by you");
    await expect(
        line(
            page,
            `${OUTSIDE} does not belong to a workspace member and will be visible to every recipient.`,
        ),
    ).toBeVisible();
    await radio(page, MEMBER, "Hidden").check();
    expect(await save(page)).toMatchObject({
        address_roles: [{ address: OUTSIDE, role: "cc", memberships: [] }],
        member_roles: [
            { membership: 1, address: ADMIN, role: null },
            { membership: 2, address: MEMBER, role: "bcc" },
        ],
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
    await expect(
        page.getByRole("heading", { name: "Who sees what", exact: true }),
    ).toBeFocused();
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Default");
    await expect(
        page.getByRole("button", { name: "Use the defaults for everyone" }),
    ).toHaveCount(0);
    expect(await save(page)).toMatchObject({
        address_roles: [],
        member_roles: [
            { membership: 1, address: ADMIN, role: null },
            { membership: 2, address: MEMBER, role: null },
        ],
    });
    await radio(page, OUTSIDE, "To").check();
    await page
        .getByLabel("Email address 1", { exact: true })
        .fill("other@example.test");
    await expect(radio(page, "other@example.test", "Hidden")).toBeChecked();
    await expect(placement(page, "other@example.test")).toContainText(
        "Default",
    );
    expect(await save(page)).toMatchObject({
        recipients: ["other@example.test"],
        address_roles: [],
    });
});

test("a chosen Copy stays Copy with nobody in To while members at their default move to To", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: TEAM,
        includeMembers: true,
    });
    await openReminders(page);
    await radio(page, ADMIN, "Copy").check();
    await expect(radio(page, MEMBER, "To")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Default");
    await expect(line(page, `To: ${MEMBER}`)).toBeVisible();
    await expect(line(page, `Copy: ${ADMIN}`)).toBeVisible();
    await expect(
        page.getByText("Nobody is in To", { exact: false }),
    ).toHaveCount(0);
    await radio(page, MEMBER, "Copy").check();
    await expect(radio(page, ADMIN, "Copy")).toBeChecked();
    await expect(line(page, `Copy: ${ADMIN}, ${MEMBER}`)).toBeVisible();
    await expect(
        line(
            page,
            "Nobody is in To: the email goes out with Copy addresses only.",
        ),
    ).toBeVisible();
    expect(await save(page)).toMatchObject({
        member_roles: [
            { membership: 1, address: ADMIN, role: "cc" },
            { membership: 2, address: MEMBER, role: "cc" },
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
            { membership: 1, address: ADMIN, role: "bcc" },
            { membership: 2, address: MEMBER, role: null },
        ],
    });
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Set by you");
});

test("a save refused after a member's address changed offers a reload that shows the new address with its choice", async ({
    page,
}) => {
    const { persons } = await reminderApi(page, {
        recipients: [],
        members: [ADMIN, { membership: 2, address: MEMBER, choice: "bcc" }],
        includeMembers: true,
    });
    await openReminders(page);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    persons[1]!.address = "renamed@example.test";
    await radio(page, MEMBER, "Copy").check();
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

test("a To answered without its role reads as the default, is counted Hidden and is sent again with the answered members", async ({
    page,
}) => {
    const SHARED = "shared@example.test";
    await reminderApi(page, {
        recipients: [SHARED],
        members: [ADMIN, { membership: 5, address: SHARED }],
        typed: { [SHARED]: { role: "to", members: [4] } },
    });
    await openReminders(page);
    await expect(radio(page, SHARED, "Hidden")).toBeChecked();
    await expect(placement(page, SHARED)).toContainText("Default");
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
    await radio(page, SHARED, "To").check();
    expect(await save(page)).toMatchObject({
        address_roles: [{ address: SHARED, role: "to", memberships: [5] }],
        member_roles: [{ membership: 5, address: SHARED, role: null }],
    });
    await expect(radio(page, SHARED, "To")).toBeChecked();
    await expect(placement(page, SHARED)).toContainText("Set by you");
    await expect(line(page, `To: ${SHARED}`)).toBeVisible();
});

test("the history shows roles to the administrator for sent and uncertain deliveries only", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: TEAM,
        includeMembers: true,
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
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(radio(page, OUTSIDE, "Hidden")).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(radio(page, OUTSIDE, "Copy")).toBeChecked();
    await expect(line(page, `Copy: ${OUTSIDE}`)).toBeVisible();
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
});

test("the section fits 320px and passes the accessibility scan at 390px", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [
            OUTSIDE,
            "a.very.long.recipient.address@subdomain.example.test",
        ],
        members: TEAM,
        includeMembers: true,
    });
    await page.setViewportSize({ width: 320, height: 900 });
    await openReminders(page);
    await radio(page, OUTSIDE, "Copy").check();
    await radio(page, ADMIN, "Copy").check();
    await expect(
        page.getByText("does not belong", { exact: false }),
    ).toBeVisible();
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
    ).toBeVisible();
});
