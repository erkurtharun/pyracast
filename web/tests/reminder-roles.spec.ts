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
const HELD =
    "This save waits until who sees what is shown. Check it, then save again.";
const NOT_SAVED =
    "Not saved yet. On the next send, every recipient can see the To and Copy addresses.";
const outsideWarning = (address: string) =>
    `${address} is not the verified primary address of an active member and will be visible to every recipient.`;
const NO_RECIPIENT = "This address adds no recipient.";
const UNICODE = "ops@bücher.de";
const ACE = "ops@xn--bcher-kva.de";
const DEPARTURE =
    "Every recipient was hidden again because a member left or an account was deleted. The remaining choices are kept.";

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
const counter = (page: Page) => page.locator("span.recipient-count");
const checked = (page: Page) => page.getByRole("radio", { checked: true });
const address = (page: Page, index: number) =>
    page.getByLabel(`Email address ${index}`, { exact: true });
const saveButton = (page: Page) =>
    page.getByRole("button", { name: "Save reminders", exact: true });
const reloadButton = (page: Page) =>
    page.getByRole("button", { name: "Reload reminders", exact: true });

async function type(page: Page, index: number, text: string) {
    await address(page, index).fill(text);
    await address(page, index).blur();
}

async function choose(page: Page, recipient: string, label: string) {
    await radio(page, recipient, label).click();
    await expect(radio(page, recipient, label)).toBeChecked();
}

async function review(page: Page) {
    await page
        .getByRole("button", { name: "Review addressing", exact: true })
        .click();
}

const isWrite = (method: string, url: string) =>
    method === "PUT" && url.endsWith("/reminders/");
const isPreview = (method: string, url: string) =>
    method === "POST" && url.endsWith("/reminders/preview/");
const isRead = (method: string, url: string) =>
    method === "GET" && url.endsWith("/reminders/");

function sent(page: Page, matches: (method: string, url: string) => boolean) {
    const bodies: Record<string, unknown>[] = [];
    page.on("request", (request) => {
        if (matches(request.method(), request.url()))
            bodies.push(request.postDataJSON());
    });
    return bodies;
}

function hold(page: Page, url: string, method?: string) {
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    const installed = page.route(url, async (route) => {
        if (method && route.request().method() !== method)
            return route.fallback();
        await released;
        await route.fallback();
    });
    return { installed, release };
}

async function save(page: Page) {
    const request = page.waitForRequest((request) =>
        isWrite(request.method(), request.url()),
    );
    await saveButton(page).click();
    const body = (await request).postDataJSON();
    await expect(saveButton(page)).toBeEnabled();
    return body;
}

async function refetch(page: Page) {
    const read = page.waitForResponse((response) =>
        isRead(response.request().method(), response.url()),
    );
    await page.clock.fastForward("00:10");
    await page.evaluate(() =>
        window.dispatchEvent(new Event("visibilitychange")),
    );
    return read;
}

async function watchFor(page: Page, needles: string[]) {
    await page.evaluate((needles) => {
        const seen: string[] = [];
        const look = () => {
            const shown = [
                document.body.textContent ?? "",
                ...[...document.querySelectorAll("input")].map(
                    (input) => input.value,
                ),
            ].join("\n");
            for (const needle of needles)
                if (shown.includes(needle) && !seen.includes(needle))
                    seen.push(needle);
        };
        new MutationObserver(look).observe(document.body, {
            subtree: true,
            childList: true,
            characterData: true,
            attributes: true,
        });
        (window as unknown as { seen: string[] }).seen = seen;
    }, needles);
    return () =>
        page.evaluate(() => (window as unknown as { seen: string[] }).seen);
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
    await reminderApi(page, { unsaved: true, members: TEAM });
    await openReminders(page);
    await expect(
        page.getByLabel("Hide recipients from each other", { exact: true }),
    ).not.toBeChecked();
    await expect(
        page.getByLabel("Include workspace members", { exact: true }),
    ).not.toBeChecked();
    await expect(address(page, 1)).toHaveValue("");
    await page.getByLabel("Include workspace members", { exact: true }).check();
    await type(page, 1, OUTSIDE);
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
    await expect(line(page, NOT_SAVED)).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Use the defaults for everyone" }),
    ).toHaveCount(0);
});

test("a plant without a schedule opens with no history in the language of the request", async ({
    page,
}) => {
    await page.setExtraHTTPHeaders({ "Accept-Language": "tr" });
    await reminderApi(page, { unsaved: true, members: TEAM });
    await openReminders(page);
    await expect(
        page.getByLabel("Email language", { exact: true }),
    ).toHaveValue("tr");
    await page.getByText("Recent deliveries", { exact: true }).click();
    await expect(
        page.getByText("No deliveries yet", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("table", { name: "Recent forecast email deliveries" }),
    ).toHaveCount(0);
});

test("a review after a member verified a typed address asks the server again and saves only on its answer", async ({
    page,
}) => {
    await page.clock.install();
    const { persons } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: [
            ADMIN,
            MEMBER,
            { membership: 3, primary: OUTSIDE, verified: [] },
        ],
        includeMembers: true,
        hidden: true,
    });
    const previews = sent(page, isPreview);
    const writes = sent(page, isWrite);
    await openReminders(page);
    await review(page);
    await expect(radio(page, OUTSIDE, "Hidden")).toBeChecked();
    await expect(placement(page, OUTSIDE)).toContainText("Default");
    await page
        .getByLabel("Hide recipients from each other", { exact: true })
        .check();
    persons[2]!.verified = [OUTSIDE];
    await refetch(page);
    const asked = previews.length;
    const preview = hold(page, "**/reminders/preview/");
    try {
        await preview.installed;
        await review(page);
        await expect.poll(() => previews.length).toBe(asked + 1);
        await saveButton(page).click();
        await expect(line(page, HELD)).toBeVisible();
        expect(writes).toEqual([]);
    } finally {
        preview.release();
    }
    await expect(radio(page, OUTSIDE, "Copy")).toBeChecked();
    await expect(placement(page, OUTSIDE)).toContainText("Member");
    expect(await save(page)).toMatchObject({ hide_recipients: false });
});

for (const refresh of ["held", "failed", "answered"] as const) {
    test(`a save answered after administration moved away shows no address when its refresh is ${refresh}`, async ({
        page,
    }) => {
        await laterAnswer(page, ["viewer"], refresh);
    });
}

test("a save answered after a language change and a moved administration shows no address", async ({
    page,
}) => {
    await laterAnswer(page, ["language"], "held");
});

test("a save answered after a moved administration and then a failed read shows no address", async ({
    page,
}) => {
    await laterAnswer(page, ["viewer", "failed"], "answered");
});

test("a save answered after a failed read and then a moved administration shows no address", async ({
    page,
}) => {
    await laterAnswer(page, ["failed", "viewer"], "answered");
});

async function laterAnswer(
    page: Page,
    steps: ("viewer" | "failed" | "language")[],
    refresh: "held" | "failed" | "answered",
) {
    let viewing = false;
    let failing = false;
    await page.clock.install();
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        viewer: (method) => viewing && method === "GET",
        deliveries: [
            {
                recipients: [ADMIN, MEMBER, OUTSIDE],
                recipient_roles: [
                    { address: ADMIN, role: "to" },
                    { address: MEMBER, role: "cc" },
                    { address: OUTSIDE, role: "bcc" },
                ],
                recipient_count: 3,
            },
        ],
    });
    await page.route("**/reminders/", (route) =>
        failing && route.request().method() === "GET"
            ? route.fulfill({
                  status: 503,
                  json: { code: "busy", message: "" },
              })
            : route.fallback(),
    );
    await page.route("**/api/session/language/", (route) =>
        route.fulfill({
            json: {
                user: { ...user, language_preference: "tr" },
                csrf_token: "signed-token",
                language: "tr",
            },
        }),
    );
    await openReminders(page);
    await page.getByText("Recent deliveries", { exact: true }).click();
    await page.getByLabel("Delivery time 1", { exact: true }).fill("09:00");
    const write = hold(page, "**/reminders/", "PUT");
    await write.installed;
    const writing = page.waitForRequest((request) =>
        isWrite(request.method(), request.url()),
    );
    await saveButton(page).click();
    await writing;
    for (const step of steps) {
        if (step === "viewer") {
            viewing = true;
            await refetch(page);
        } else if (step === "failed") {
            failing = true;
            await refetch(page);
            failing = false;
        } else {
            viewing = true;
            const read = page.waitForResponse((response) =>
                isRead(response.request().method(), response.url()),
            );
            await page
                .getByLabel("Language", { exact: true })
                .selectOption("tr");
            await read;
        }
    }
    await expect(page.getByText(MEMBER)).toHaveCount(0);
    const seen = await watchFor(page, [MEMBER, OUTSIDE]);
    const after = hold(page, "**/reminders/", "GET");
    await after.installed;
    if (refresh !== "held") after.release();
    failing = refresh === "failed";
    const refreshing = page.waitForRequest((request) =>
        isRead(request.method(), request.url()),
    );
    const answered = page.waitForResponse((response) =>
        isRead(response.request().method(), response.url()),
    );
    write.release();
    await refreshing;
    expect(await seen()).toEqual([]);
    after.release();
    await answered;
    expect(await seen()).toEqual([]);
    await expect(page.getByText(OUTSIDE)).toHaveCount(0);
}

test("while who sees what is checked, refused or failed no row shows a placement, a default or a count", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: true,
    });
    await openReminders(page);
    const nothingPlaced = async () => {
        await expect(checked(page)).toHaveCount(0);
        await expect(summaryLines(page)).toHaveCount(0);
        await expect(counter(page)).toHaveText("");
        for (const item of [ADMIN, MEMBER])
            await expect(placement(page, item)).not.toContainText(
                /Default|Set by you/,
            );
        await expect(
            page.getByRole("button", { name: "Use the defaults for everyone" }),
        ).toHaveCount(0);
    };
    const preview = hold(page, "**/reminders/preview/");
    try {
        await preview.installed;
        await review(page);
        await expect(line(page, "Checking who sees what…")).toBeVisible();
        await expect(group(page, ADMIN)).toHaveCount(1);
        await nothingPlaced();
    } finally {
        preview.release();
    }
    await expect(radio(page, ADMIN, "To")).toBeChecked();
    await page
        .getByRole("button", { name: "Add address", exact: true })
        .click();
    await type(page, 2, OUTSIDE);
    await expect(
        line(page, "Recipient addresses must be distinct (case-insensitive)."),
    ).toBeVisible();
    await nothingPlaced();
    const failing = (route: Route) => route.fulfill({ status: 500, body: "" });
    await page.route("**/reminders/preview/", failing);
    await page
        .getByRole("button", { name: "Remove email address 2", exact: true })
        .click();
    await expect(
        line(page, "Who sees what could not be checked."),
    ).toBeVisible();
    await nothingPlaced();
});

test("an earlier row edited into a later row's address shows no other row's answer while it is checked", async ({
    page,
}) => {
    const SECOND = "second@example.test";
    await reminderApi(page, {
        recipients: [OUTSIDE, SECOND],
        members: [ADMIN],
        hidden: false,
        typed: { [SECOND]: "cc" },
    });
    await openReminders(page);
    await expect(radio(page, SECOND, "Copy")).toBeChecked();
    const preview = hold(page, "**/reminders/preview/");
    try {
        await preview.installed;
        await type(page, 1, SECOND);
        await expect(line(page, "Checking who sees what…")).toBeVisible();
        await expect(checked(page)).toHaveCount(0);
        await expect(group(page, SECOND)).toHaveCount(1);
        await expect(group(page, OUTSIDE)).toHaveCount(0);
    } finally {
        preview.release();
    }
    await expect(
        line(page, "Recipient addresses must be distinct (case-insensitive)."),
    ).toBeVisible();
    await expect(checked(page)).toHaveCount(0);
});

test("a typed spelling and a member's spelling of one mailbox show no hidden recipient", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [ACE],
        members: [ADMIN, { membership: 2, primary: UNICODE }],
        includeMembers: true,
        hidden: false,
        wire: { [UNICODE]: ACE },
    });
    await openReminders(page);
    await expect(line(page, `To: ${ADMIN}`)).toBeVisible();
    await expect(line(page, `Copy: ${ACE}`)).toBeVisible();
    await expect(page.getByText(/^Hidden:/)).toHaveCount(0);
    await expect(page.getByText("2 / 5", { exact: true })).toBeVisible();
});

test("a reviewed hidden schedule that is paused saves only once who sees what is shown, and says why", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: true,
    });
    const writes = sent(page, isWrite);
    await openReminders(page);
    await expect(
        page.getByLabel("Enable forecast emails for this plant", {
            exact: true,
        }),
    ).not.toBeChecked();
    const preview = hold(page, "**/reminders/preview/");
    try {
        await preview.installed;
        await review(page);
        await saveButton(page).click();
        await expect(line(page, HELD)).toBeVisible();
        await expect(heading(page)).toBeFocused();
        await expect(heading(page)).toHaveAccessibleDescription(HELD);
        expect(writes).toEqual([]);
    } finally {
        preview.release();
    }
    await expect(radio(page, ADMIN, "To")).toBeChecked();
    expect(writes).toEqual([]);
    expect(await save(page)).toMatchObject({
        enabled: false,
        hide_recipients: false,
    });
});

test("a role chosen before pausing is saved only once its answer is shown", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        enabled: true,
    });
    const writes = sent(page, isWrite);
    await openReminders(page);
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    const preview = hold(page, "**/reminders/preview/");
    try {
        await preview.installed;
        await radio(page, MEMBER, "Hidden").click();
        await page
            .getByLabel("Enable forecast emails for this plant", {
                exact: true,
            })
            .uncheck();
        await saveButton(page).click();
        await expect(line(page, HELD)).toBeVisible();
        expect(writes).toEqual([]);
    } finally {
        preview.release();
    }
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    expect(writes).toEqual([]);
    expect(await save(page)).toMatchObject({
        enabled: false,
        member_roles: [{ memberships: [2], address: MEMBER, role: "bcc" }],
    });
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
    const editor = hold(page, "**/_next/static/**/*.js");
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
    await expect(saveButton(page)).toHaveCount(0);
});

test("typing a hidden member's address keeps that member hidden and sends no role", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN, { membership: 2, primary: MEMBER, choice: "bcc" }],
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
        members: [ADMIN, { membership: 2, primary: MEMBER, choice: "bcc" }],
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
    const STORED = UNICODE;
    const TYPED = ACE;
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN, { membership: 2, primary: STORED, choice: "cc" }],
        includeMembers: false,
        hidden: false,
        wire: { [UNICODE]: ACE },
    });
    await openReminders(page);
    const preview = hold(page, "**/reminders/preview/");
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
            { membership: 2, primary: SHARED },
            { membership: 3, primary: SHARED },
            { membership: 4, primary: GONE, active: false },
        ],
        hidden: false,
        limit: 1,
    });
    await openReminders(page);
    await expect(line(page, NO_RECIPIENT)).toBeVisible();
    await expect(page.getByText("0 / 1", { exact: true })).toBeVisible();
    const add = page.getByRole("button", { name: "Add address", exact: true });
    await expect(add).toBeEnabled();
    await add.click();
    await type(page, 2, SHARED);
    await choose(page, SHARED, "To");
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

test("a typed row that adds no recipient says so while the member of its mailbox shows its own placement", async ({
    page,
}) => {
    const SHARED = "shared@example.test";
    const GONE = "gone@example.test";
    await reminderApi(page, {
        recipients: [SHARED],
        members: [
            ADMIN,
            { membership: 2, primary: SHARED },
            { membership: 3, primary: GONE, active: false },
        ],
        includeMembers: true,
        hidden: false,
        typed: { [SHARED]: { role: "", members: [], ties: [3] } },
    });
    await openReminders(page);
    await expect(line(page, NO_RECIPIENT)).toBeVisible();
    await expect(group(page, SHARED)).toHaveCount(1);
    const memberRow = page.getByRole("listitem").filter({ hasText: SHARED });
    await expect(
        memberRow.getByRole("radio", { name: "Copy", exact: true }),
    ).toBeChecked();
    await expect(line(page, `Copy: ${SHARED}`)).toBeVisible();
    await expect(page.getByText("2 / 5", { exact: true })).toBeVisible();
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
    await choose(page, MEMBER, "Hidden");
    await expect(line(page, "Hidden: 1 address")).toBeVisible();
    deliveries.unshift({
        status: "sent",
        scheduled_for: "2026-01-02T08:00:00Z",
    });
    await refetch(page);
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

test("edits outside the recipients stay when another tab saves first, and a reload is offered", async ({
    page,
}) => {
    await page.clock.install();
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: true,
    });
    await openReminders(page);
    await page.getByLabel("Delivery time 1", { exact: true }).fill("09:00");
    await page.getByLabel("Email language", { exact: true }).selectOption("tr");
    await page
        .getByLabel("Enable forecast emails for this plant", { exact: true })
        .check();
    await address(page, 1).fill("typed@example.test");
    const elsewhere = await page.evaluate(async () => {
        const response = await fetch("/api/tenants/1/regions/1/reminders/", {
            method: "PUT",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
                enabled: false,
                include_workspace_members: false,
                language: "en",
                recipients: ["outside@example.test"],
                times: ["08:00"],
                expected_revision: 0,
                hide_recipients: true,
            }),
        });
        return ((await response.json()) as { revision: number }).revision;
    });
    expect(elsewhere).toBe(1);
    await refetch(page);
    await expect(reloadButton(page)).toBeVisible();
    await expect(
        page.getByLabel("Delivery time 1", { exact: true }),
    ).toHaveValue("09:00");
    await expect(
        page.getByLabel("Email language", { exact: true }),
    ).toHaveValue("tr");
    await expect(
        page.getByLabel("Enable forecast emails for this plant", {
            exact: true,
        }),
    ).toBeChecked();
    await expect(address(page, 1)).toHaveValue("typed@example.test");
    await expect(address(page, 1)).toBeFocused();
    await expect(
        page.getByLabel("Include workspace members", { exact: true }),
    ).toBeChecked();
});

for (const first of ["preview", "read"] as const) {
    test(`a saved change offers no reload when its earlier check and its refresh answer late, the ${first} first`, async ({
        page,
    }) => {
        await reminderApi(page, {
            recipients: [OUTSIDE],
            members: TEAM,
            hidden: true,
        });
        await openReminders(page);
        await expect(page.getByText("1 / 5", { exact: true })).toBeVisible();
        const preview = hold(page, "**/reminders/preview/");
        const read = hold(page, "**/reminders/", "GET");
        try {
            await Promise.all([preview.installed, read.installed]);
            const checking = page.waitForRequest((request) =>
                isPreview(request.method(), request.url()),
            );
            await page
                .getByLabel("Include workspace members", { exact: true })
                .check();
            await checking;
            const refreshing = page.waitForRequest((request) =>
                isRead(request.method(), request.url()),
            );
            const writing = page.waitForResponse((response) =>
                isWrite(response.request().method(), response.url()),
            );
            await saveButton(page).click();
            expect((await writing).status()).toBe(200);
            await refreshing;
            const seen = await watchFor(page, ["Reload reminders"]);
            for (const gate of first === "preview"
                ? [preview, read]
                : [read, preview]) {
                const answered = page.waitForResponse((response) =>
                    gate === preview
                        ? isPreview(response.request().method(), response.url())
                        : isRead(response.request().method(), response.url()),
                );
                gate.release();
                await answered;
            }
            await expect(
                page.getByText("Saved successfully.", { exact: true }),
            ).toBeVisible();
            expect(await seen()).toEqual([]);
            await expect(reloadButton(page)).toHaveCount(0);
        } finally {
            preview.release();
            read.release();
        }
    });
}

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
    persons.push({ membership: 3, primary: OUTSIDE });
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
    await expect(checked(page)).toHaveCount(0);
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
        members: [ADMIN, { membership: 2, primary: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Set by you");
    await choose(page, MEMBER, "Copy");
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

test("a member chosen Hidden and returned to the default leaves nothing to save", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await choose(page, MEMBER, "Hidden");
    await expect(line(page, NOT_SAVED)).toBeVisible();
    await page
        .getByRole("button", { name: `Use default for ${MEMBER}`, exact: true })
        .click();
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    await expect(page.getByText("Not saved yet", { exact: false })).toHaveCount(
        0,
    );
    expect(await save(page)).not.toHaveProperty("member_roles");
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
    await review(page);
    await expect(hide).not.toBeChecked();
    await expect(heading(page)).toBeFocused();
    await expect(line(page, `To: ${ADMIN}`)).toBeVisible();
    await expect(line(page, `Copy: ${MEMBER}`)).toBeVisible();
    await expect(line(page, NOT_SAVED)).toBeVisible();
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
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN, { membership: 2, primary: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hiddenByDeparture: true,
    });
    await openReminders(page);
    await expect(line(page, DEPARTURE)).toBeVisible();
    await save(page);
    await expect(line(page, DEPARTURE)).toHaveCount(0);
    await expect(
        line(
            page,
            "Every recipient is hidden today. You can address the administrator directly and keep members in copy.",
        ),
    ).toBeVisible();
    await review(page);
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
    await choose(page, OUTSIDE, "Copy");
    await expect(placement(page, OUTSIDE)).toContainText("Set by you");
    await expect(line(page, outsideWarning(OUTSIDE))).toHaveCount(2);
    await choose(page, MEMBER, "Hidden");
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
    await radio(page, OUTSIDE, "To").click();
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
    await choose(page, ADMIN, "Copy");
    await expect(line(page, `To: ${MEMBER}`)).toBeVisible();
    await expect(radio(page, MEMBER, "To")).toBeChecked();
    await expect(placement(page, MEMBER)).toContainText("Default");
    await expect(line(page, `Copy: ${ADMIN}`)).toBeVisible();
    await expect(
        page.getByText("Nobody is in To", { exact: false }),
    ).toHaveCount(0);
    await choose(page, MEMBER, "Copy");
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
            { membership: 3, primary: GONE, active: false },
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
    await expect(line(page, NO_RECIPIENT)).toBeVisible();
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
    await choose(page, MEMBER, "Hidden");
    await expect(
        live.getByText("Hidden: 1 address", { exact: true }),
    ).toBeVisible();
    await expect(live.getByText(/^Copy:/)).toHaveCount(0);
    await choose(page, ADMIN, "Hidden");
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
        members: [ADMIN, { membership: 2, primary: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hidden: false,
    });
    const writes = sent(page, isWrite);
    await openReminders(page);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    persons[1]!.primary = "renamed@example.test";
    await radio(page, MEMBER, "Copy").click();
    await expect(
        line(page, "The email reminders changed. Reload before saving."),
    ).toBeVisible();
    await expect(reloadButton(page)).toBeVisible();
    await saveButton(page).click();
    await expect(line(page, HELD)).toBeVisible();
    expect(writes).toEqual([]);
    await reloadButton(page).click();
    await expect(radio(page, "renamed@example.test", "Hidden")).toBeChecked();
    await expect(placement(page, "renamed@example.test")).toContainText(
        "Set by you",
    );
    await expect(group(page, MEMBER)).toHaveCount(0);
});

test("the history shows the roles recorded for sent and uncertain deliveries to the administrator only, whatever changed since", async ({
    page,
}) => {
    const whole = {
        recipients: TEAM,
        recipient_roles: [
            { address: ADMIN, role: "to" as const },
            { address: MEMBER, role: "cc" as const },
        ],
        recipient_count: 2,
    };
    await reminderApi(page, {
        recipients: [],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        deliveries: [
            { status: "sent", ...whole },
            {
                status: "uncertain",
                sent_at: null,
                last_error: "mail_outcome_uncertain",
                ...whole,
            },
            { status: "failed", sent_at: null, ...whole },
            {
                status: "sent",
                recipients: TEAM,
                recipient_roles: null,
                recipient_count: 2,
            },
        ],
    });
    await openReminders(page);
    await choose(page, MEMBER, "Hidden");
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

function caller(page: Page) {
    const url = "/api/tenants/1/regions/1/reminders/";
    return (method: string, body?: object, path = "") =>
        page.evaluate(
            async ({ url, method, body }) => {
                const response = await fetch(url, {
                    method,
                    headers: { "content-type": "application/json" },
                    body: body && JSON.stringify(body),
                });
                return { status: response.status, json: await response.json() };
            },
            { url: url + path, method, body },
        );
}

const SAVED = {
    enabled: false,
    include_workspace_members: true,
    language: "en",
    recipients: [OUTSIDE],
    times: ["08:00"],
    expected_revision: 0,
    hide_recipients: false,
};

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
    const call = caller(page);
    const before = (await call("GET")).json;
    expect((await call("PUT", SAVED)).json.revision).toBe(0);
    for (const [refused, status, code, field] of [
        [
            {
                address_roles: [
                    {
                        address: "else@example.test",
                        role: "to",
                        memberships: [],
                    },
                ],
            },
            400,
            "invalid_request",
            "address_roles",
        ],
        [
            {
                member_roles: [
                    { memberships: [0], address: MEMBER, role: "to" },
                ],
            },
            400,
            "invalid_request",
            "member_roles.0.memberships.0",
        ],
        [
            {
                member_roles: [
                    { memberships: [3], address: MEMBER, role: "bcc" },
                ],
            },
            409,
            "reminder_schedule_changed",
            undefined,
        ],
        [
            { recipients: [OUTSIDE, "OUTSIDE@example.test"] },
            400,
            "duplicate_recipients",
            "recipients",
        ],
    ] as const) {
        const answer = await call("PUT", { ...SAVED, ...refused });
        expect([answer.status, answer.json.code]).toEqual([status, code]);
        expect(Object.keys(answer.json.fields ?? {})).toEqual(
            field ? [field] : [],
        );
    }
    viewing = true;
    const viewer = await call("PUT", {
        ...SAVED,
        include_workspace_members: false,
    });
    expect([viewer.status, viewer.json.code]).toEqual([
        403,
        "permission_denied",
    ]);
    viewing = false;
    expect((await call("GET")).json).toEqual(before);
    const changed = await call("PUT", {
        ...SAVED,
        member_roles: [{ memberships: [2], address: MEMBER, role: "bcc" }],
    });
    expect(changed.json.revision).toBe(1);
    expect(changed.json.addresses.members[1].placement).toBe("bcc");
    expect(changed.json.deliveries).toEqual(before.deliveries);
    const preview = await call(
        "POST",
        { ...SAVED, expected_revision: 1, include_workspace_members: false },
        "preview/",
    );
    expect(preview.json.include_workspace_members).toBe(false);
    expect((await call("GET")).json).toEqual(changed.json);
});

test("the mock refuses each malformed request with the status, code and member the server names", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE, "two@example.test", "three@example.test"],
        members: TEAM,
        hidden: false,
        limit: 1,
    });
    await page.goto("/app/");
    const call = caller(page);
    const withoutRecipients = Object.fromEntries(
        Object.entries(SAVED).filter(([name]) => name !== "recipients"),
    );
    for (const [body, status, code, field] of [
        [{ ...SAVED, times: ["25:00"] }, 400, "invalid_request", "times.0"],
        [withoutRecipients, 400, "invalid_request", "recipients"],
        [{ ...SAVED, language: "de" }, 400, "invalid_request", "language"],
        [
            {
                ...SAVED,
                enabled: true,
                expected_revision: 5,
                recipients: [OUTSIDE, "a@example.test", "b@example.test"],
            },
            409,
            "reminder_schedule_changed",
            undefined,
        ],
        [
            {
                ...SAVED,
                enabled: true,
                recipients: [OUTSIDE, "a@example.test", "b@example.test"],
                member_roles: [
                    { memberships: [3], address: MEMBER, role: "bcc" },
                ],
            },
            409,
            "reminder_schedule_changed",
            undefined,
        ],
    ] as const) {
        const answer = await call("PUT", body);
        expect([answer.status, answer.json.code]).toEqual([status, code]);
        expect(Object.keys(answer.json.fields ?? {})).toEqual(
            field ? [field] : [],
        );
    }
    const repeated = await call("PUT", {
        ...SAVED,
        include_workspace_members: false,
        recipients: [OUTSIDE, "two@example.test", "three@example.test"],
        member_roles: [{ memberships: [2, 2], address: MEMBER, role: "bcc" }],
    });
    expect(repeated.status).toBe(200);
    expect(repeated.json.addresses.members[1].role).toBe("bcc");
});

test("the mock keeps a hidden typed row's ties when its member moves and ties every holder of its address", async ({
    page,
}) => {
    const ALICE = "alice@example.test";
    const MOVED = "alice.new@example.test";
    const PAT = "pat@example.test";
    const QUINN = "quinn@example.test";
    const ALIAS = "q.alt@example.test";
    const { persons } = await reminderApi(page, {
        recipients: [],
        members: [
            ADMIN,
            { membership: 2, primary: ALICE },
            { membership: 3, primary: "bob@example.test" },
            { membership: 4, primary: PAT, verified: [] },
            { membership: 5, primary: QUINN, verified: [QUINN, ALIAS] },
        ],
        includeMembers: true,
        hidden: false,
        limit: 10,
    });
    await page.goto("/app/");
    const call = caller(page);
    const hidden = (address: string, memberships: number[]) => ({
        address,
        role: "bcc",
        memberships,
    });
    const first = await call("PUT", {
        ...SAVED,
        recipients: [ALICE, PAT, ALIAS],
        address_roles: [hidden(ALICE, [2]), hidden(PAT, []), hidden(ALIAS, [])],
    });
    expect(first.status).toBe(200);
    const quinn = (answer: { addresses: { members: { address: string }[] } }) =>
        answer.addresses.members.find((row) => row.address === QUINN);
    expect(quinn(first.json)).toMatchObject({ role: null, placement: "bcc" });
    persons[1]!.primary = MOVED;
    persons[3]!.primary = "pat.new@example.test";
    persons[3]!.verified = ["pat.new@example.test"];
    const body = {
        ...SAVED,
        expected_revision: first.json.revision,
        recipients: [ALICE, PAT, ALIAS],
    };
    for (const answer of [
        await call("POST", body, "preview/"),
        await call("PUT", body),
    ]) {
        const placed = Object.fromEntries(
            (
                answer.json.addresses.members as {
                    address: string;
                    role: string | null;
                    placement: string;
                }[]
            ).map((row) => [row.address, [row.role, row.placement]]),
        );
        expect(placed).toMatchObject({
            [ADMIN]: [null, "to"],
            [MOVED]: [null, "bcc"],
            "bob@example.test": [null, "cc"],
            "pat.new@example.test": [null, "bcc"],
            [QUINN]: [null, "bcc"],
        });
    }
});

test("the mock combines the choices of two stored spellings of one mailbox and keeps the combined choice on a save", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [UNICODE, ACE],
        members: [ADMIN],
        hidden: false,
        typed: { [UNICODE]: "to" },
        wire: { [UNICODE]: ACE },
    });
    await page.goto("/app/");
    const call = caller(page);
    const stored = (await call("GET")).json;
    expect(
        stored.addresses.added.map(
            (row: { role: string | null; placement: string }) => [
                row.role,
                row.placement,
            ],
        ),
    ).toEqual([
        ["bcc", "bcc"],
        ["bcc", "bcc"],
    ]);
    expect(stored.recipient_count).toBe(1);
    const saved = await call("PUT", {
        ...SAVED,
        include_workspace_members: false,
        recipients: [ACE],
    });
    expect(saved.status).toBe(200);
    expect(saved.json.addresses.added).toMatchObject([
        { address: ACE, role: "bcc", placement: "bcc" },
    ]);
});

const DOMAIN = Array.from({ length: 4 }, () => "ä".repeat(57)).join(".");
const LONG_UNICODE = `${"o".repeat(253 - DOMAIN.length - 3)}@${DOMAIN}.de`;
const WITHOUT_WIRE_FORM = [
    "ops@a�b.de",
    "ops@example.com（x）",
    "ops@exa，mple.com",
    "ops@example.com。",
    "ops@straẞe.de",
    "ops@ᲐᲑ.ge",
    ...["K", "ſ", "İ", "ı"].map((letter) => `t${letter}st@example.com`),
    LONG_UNICODE,
];

test("the mock refuses every address the server cannot carry with the server's status, code and field", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        hidden: false,
        dailyLimit: 2,
        wire: { [UNICODE]: ACE },
    });
    await page.goto("/app/");
    const call = caller(page);
    expect(LONG_UNICODE).toHaveLength(254);
    const cases: [string[], number, string, string[]][] = [
        ...WITHOUT_WIRE_FORM.map(
            (address): [string[], number, string, string[]] => [
                [address],
                400,
                "invalid_request",
                ["recipients"],
            ],
        ),
        [["Ops <ops@example.com>"], 400, "invalid_request", ["recipients.0"]],
        [[UNICODE, ACE], 400, "duplicate_recipients", ["recipients"]],
    ];
    for (const path of ["preview/", ""]) {
        const method = path ? "POST" : "PUT";
        for (const [recipients, status, code, fields] of cases) {
            const answer = await call(method, { ...SAVED, recipients }, path);
            expect([recipients, answer.status, answer.json.code]).toEqual([
                recipients,
                status,
                code,
            ]);
            expect(Object.keys(answer.json.fields ?? {})).toEqual(fields);
        }
        const times = await call(
            method,
            { ...SAVED, times: ["07:00", "08:00", "09:00"] },
            path,
        );
        expect([times.status, times.json.code]).toEqual([
            400,
            "too_many_reminder_times",
        ]);
        expect(Object.keys(times.json.fields)).toEqual(["times"]);
    }
    const revision = (await call("GET")).json.revision;
    for (const recipients of [
        [UNICODE],
        [ACE],
        ["ops@xn--strae-oqa8r.de", "ops@xn--strasse-t2a.de"],
    ]) {
        const answer = await call(
            "POST",
            { ...SAVED, recipients, expected_revision: revision },
            "preview/",
        );
        expect([recipients, answer.status]).toEqual([recipients, 200]);
        expect(answer.json.recipient_count).toBe(
            recipients.length + TEAM.length,
        );
    }
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
            deliveries: [
                {
                    recipients: TEAM,
                    recipient_roles: [
                        { address: ADMIN, role: "to" },
                        { address: MEMBER, role: "cc" },
                    ],
                    recipient_count: 2,
                },
            ],
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

test("a press cancelled after leaving an address field commits its text and leaves no listener behind", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN],
        hidden: false,
    });
    const previews = sent(page, isPreview);
    await openReminders(page);
    const pointer = (type: string) =>
        page.evaluate(
            (type) =>
                document.body.dispatchEvent(
                    new PointerEvent(type, { bubbles: true }),
                ),
            type,
        );
    await address(page, 1).fill(OUTSIDE);
    await pointer("pointerdown");
    await address(page, 1).blur();
    await pointer("pointercancel");
    await expect(radio(page, OUTSIDE, "Hidden")).toBeChecked();
    await address(page, 1).fill("later@example.test");
    await page.evaluate(() =>
        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true })),
    );
    await type(page, 1, "final@example.test");
    await expect(radio(page, "final@example.test", "Hidden")).toBeChecked();
    expect(previews.map((body) => body.recipients)).toEqual([
        [OUTSIDE],
        ["final@example.test"],
    ]);
});

test("the section has no horizontal scroll at 320px and no accessibility violation at 390px", async ({
    page,
}) => {
    const LONG = "a.very.long.recipient.address@subdomain.example.test";
    await reminderApi(page, {
        recipients: [OUTSIDE, LONG],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await page.setViewportSize({ width: 320, height: 900 });
    await openReminders(page);
    await choose(page, LONG, "Copy");
    await choose(page, ADMIN, "Copy");
    await expect(line(page, outsideWarning(LONG))).toHaveCount(2);
    await expect(line(page, `To: ${MEMBER}`)).toBeVisible();
    expect(await fits(page)).toBe(true);
    const outside = await page
        .getByRole("region", { name: "Email reminders" })
        .locator("input:visible, button:visible, p:visible")
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
        hiddenByDeparture: true,
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
    await expect(
        line(
            page,
            "Bir üye ayrıldığı ya da bir hesap silindiği için tüm alıcılar yeniden gizlendi. Kalan seçimler korunuyor.",
        ),
    ).toBeVisible();
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
    const copy = turkish.getByRole("radio", { name: "Bilgi", exact: true });
    await expect(
        turkish.getByRole("radio", { name: "Gizli", exact: true }),
    ).toBeChecked();
    await copy.click();
    await expect(copy).toBeChecked();
    await expect(turkish.locator("xpath=..")).toContainText("Sizin seçiminiz");
    await expect(line(page, `Kime: ${ADMIN}`)).toBeVisible();
    await expect(line(page, `Bilgi: ${MEMBER}, ${OUTSIDE}`)).toBeVisible();
    await expect(
        line(
            page,
            "Henüz kaydedilmedi. Bir sonraki gönderimde tüm alıcılar Kime ve Bilgi adreslerini görebilir.",
        ),
    ).toBeVisible();
    await expect(
        line(
            page,
            `${OUTSIDE} etkin bir üyenin doğrulanmış birincil adresi değil ve her alıcıya görünecek.`,
        ),
    ).toHaveCount(2);
    await expect(
        line(page, "Yeni üyeler varsayılan rolle eklenir."),
    ).toBeVisible();
});
