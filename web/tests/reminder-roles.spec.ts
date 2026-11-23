import type { Locator, Request } from "@playwright/test";
import { expect, test, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { errorBody } from "./support/errors";
import {
    openReminders,
    paged,
    plain,
    reminderApi,
    sessionIdentity,
    tenantRow,
    user,
} from "./support/workspace";

const ADMIN = user.email;
const MEMBER = "member@example.test";
const OUTSIDE = "outside@example.test";
const THIRD = "third@example.test";
const TEAM = [ADMIN, MEMBER];
const TITLE = "Show these addresses to every recipient?";
const SENTENCE =
    "Every recipient of these reminders will see the To and Copy addresses below.";
const CHANGED =
    "Nothing of this save was stored: the reminder settings or the people in this workspace changed.";
const CONFLICT = `${CHANGED} Your edits are kept; check them and save again.`;
const REFUSED =
    "The request was refused and nothing was saved. The page reads the saved settings again.";
const UNKNOWN =
    "The save could not be confirmed. The page reads the saved settings again.";
const KEPT = (text: string) => `${text} Your edits are kept.`;
const LOST =
    "Nothing was saved: you were no longer an administrator of this workspace.";
const SAVED_LINE = "Saved successfully.";
const LOADING = "Loading email reminders…";
const UNAVAILABLE = "The service is unavailable.";
const NO_ADDRESS_SHOWN = "No address will appear in To or Copy.";
const HIDDEN_TODAY =
    "Every recipient is hidden today. You can address the administrator directly and keep members in copy.";
const HIDDEN_HELPER =
    "When ticked, everyone gets the reminder in hidden copy (Bcc) and no recipient sees another address.";
const ONCE_SAVED = "You can choose a role once this address is saved.";
const DEPARTURE =
    "Every recipient was hidden again because a member left or an account was deleted. The remaining choices are kept.";
const OUTSIDE_MARK = "Not an active member's verified primary address";
const DROPPED = (role: string, address: string) =>
    `Your choice “${role}” for ${address} was removed: the saved recipients or the people in this workspace changed.`;
const COUNT_ONLY = (count: number) =>
    `${count} recipient${count === 1 ? "" : "s"} of at most 5. In this app only the workspace administrator sees the addresses.`;

const group = (page: Page, address: string) =>
    page.getByRole("group", {
        name: `How ${address} receives the email`,
        exact: true,
    });
const radio = (page: Page, address: string, label: string) =>
    group(page, address).getByRole("radio", { name: label, exact: true });
const line = (page: Page | Locator, text: string) =>
    page.getByText(text, { exact: true });
const panel = (page: Page) =>
    page.getByRole("region", { name: "Email reminders", exact: true });
const panelHeading = (page: Page) =>
    page.getByRole("heading", {
        name: "Email reminders",
        exact: true,
        level: 2,
    });
const heading = (page: Page) =>
    page.getByRole("heading", { name: "Who sees what", exact: true });
const savedView = (page: Page) => heading(page).locator("xpath=..");
const listed = (scope: Locator) => scope.locator("dl").locator("dt, dd");
const address = (page: Page, index: number) =>
    page.getByLabel(`Email address ${index}`, { exact: true });
const time = (page: Page, index: number) =>
    page.getByLabel(`Delivery time ${index}`, { exact: true });
const saveButton = (page: Page) =>
    page.getByRole("button", { name: "Save reminders", exact: true });
const dialog = (page: Page) =>
    page.getByRole("dialog", { name: TITLE, exact: true });
const confirmButton = (page: Page) =>
    dialog(page).getByRole("button", {
        name: "Save and show addresses",
        exact: true,
    });
const cancelButton = (page: Page) =>
    dialog(page).getByRole("button", { name: "Back to editing", exact: true });
const retryButton = (page: Page) =>
    panel(page).getByRole("button", { name: "Try again", exact: true });
const typedFields = (page: Page) => page.locator('input[name="recipients"]');
const hideBox = (page: Page) =>
    page.getByLabel("Hide recipients from each other", { exact: true });
const useDefaults = (page: Page) =>
    page.getByRole("button", {
        name: "Use the defaults for everyone",
        exact: true,
    });

const isWrite = (method: string, url: string) =>
    method === "PUT" && url.endsWith("/reminders/");
const isPreview = (method: string, url: string) =>
    method === "POST" && url.endsWith("/reminders/preview/");
const isRead = (method: string, url: string) =>
    method === "GET" && url.endsWith("/reminders/");
const isTenants = (method: string, url: string) =>
    method === "GET" && url.endsWith("/api/tenants/");

function sent(page: Page, matches: (method: string, url: string) => boolean) {
    const bodies: Record<string, unknown>[] = [];
    page.on("request", (request) => {
        if (matches(request.method(), request.url()))
            bodies.push(request.postDataJSON());
    });
    return bodies;
}

function order(page: Page) {
    const requests: Request[] = [];
    page.on("request", (request) => requests.push(request));
    return (
        before: Request,
        matches: (method: string, url: string) => boolean,
    ) =>
        requests
            .slice(requests.indexOf(before) + 1)
            .filter((request) => matches(request.method(), request.url()));
}

const nextRequest = (
    page: Page,
    matches: (method: string, url: string) => boolean,
) => page.waitForRequest((request) => matches(request.method(), request.url()));

const answerTo = (page: Page, request: Request) =>
    page.waitForResponse((response) => response.request() === request);

async function review(page: Page) {
    const request = nextRequest(page, isPreview);
    await saveButton(page).click();
    const previewed = await request;
    const answer = await (await previewed.response())!.json();
    await expect(dialog(page)).toBeVisible();
    return { previewed: previewed.postDataJSON(), answer };
}

async function saveVisible(page: Page) {
    const { previewed, answer } = await review(page);
    const request = nextRequest(page, isWrite);
    await confirmButton(page).click();
    const body = (await request).postDataJSON();
    await expect(line(page, SAVED_LINE)).toBeFocused();
    await expect(saveButton(page)).toBeEnabled();
    return { previewed, answer, body };
}

async function refetch(page: Page) {
    const read = nextRequest(page, isRead);
    await page.clock.fastForward("00:10");
    await page.evaluate(() =>
        window.dispatchEvent(new Event("visibilitychange")),
    );
    return (await read).response();
}

async function tenantsAgain(page: Page) {
    const tenants = nextRequest(page, isTenants);
    await page.clock.fastForward("00:10");
    await page.evaluate(() =>
        window.dispatchEvent(new Event("visibilitychange")),
    );
    await (await tenants).response();
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
        look();
        (window as unknown as { seen: string[] }).seen = seen;
    }, needles);
    return () =>
        page.evaluate(() => (window as unknown as { seen: string[] }).seen);
}

async function speaking(page: Page) {
    await page.route("**/api/session/language/", (route) => {
        const { language } = route.request().postDataJSON() as {
            language: "en" | "tr";
        };
        return route.fulfill({
            json: {
                user: { ...user, language_preference: language },
                csrf_token: "signed-token",
                language,
            },
        });
    });
}

async function speak(page: Page, language: "en" | "tr") {
    await page.getByLabel(/^(Language|Dil)$/).selectOption(language);
    await expect(
        page.getByRole("heading", {
            name:
                language === "en"
                    ? "Email reminders"
                    : "E-posta hatırlatıcıları",
            exact: true,
        }),
    ).toBeVisible();
}

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

async function saveElsewhere(page: Page, body: object) {
    const call = caller(page);
    const revision = (await call("GET")).json.revision;
    const current = { ...body, expected_revision: revision };
    const preview = await call("POST", current, "preview/");
    expect(preview.status).toBe(200);
    const stored = await call("PUT", {
        ...current,
        disclosure: preview.json.disclosure,
    });
    expect(stored.status).toBe(200);
    return stored.json;
}

async function asRole(page: Page, role: () => "admin" | "viewer") {
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: role() }),
            ]),
        }),
    );
}

const fits = (page: Page) =>
    page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
    );

const SAVED = {
    enabled: false,
    include_workspace_members: true,
    language: "en",
    recipients: [OUTSIDE],
    times: ["08:00"],
    expected_revision: 0,
    hide_recipients: false,
};

const SENT_TO_TEAM = {
    recipients: [ADMIN, MEMBER, OUTSIDE],
    recipient_roles: [
        { address: ADMIN, role: "to" as const },
        { address: MEMBER, role: "cc" as const },
        { address: OUTSIDE, role: "bcc" as const },
    ],
    recipient_count: 3,
};

test("address data stays hidden after administration moves, through two language switches with held reads, until an administrator read arrives", async ({
    page,
}) => {
    let role: "admin" | "viewer" = "admin";
    let answersViewer = false;
    await page.clock.install();
    const { answerLater } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        viewer: (method) => answersViewer && method === "GET",
        deliveries: [SENT_TO_TEAM],
    });
    await asRole(page, () => role);
    await speaking(page);
    await openReminders(page);
    await expect(typedFields(page)).toHaveValue(OUTSIDE);
    await page.getByText("Recent deliveries", { exact: true }).click();
    await expect(page.getByText(`${MEMBER} Copy`)).toBeVisible();
    const turkish = answerLater("GET");
    const english = answerLater("GET");
    try {
        role = "viewer";
        await speak(page, "tr");
        await turkish.committed;
        answersViewer = true;
        await expect(typedFields(page)).toHaveCount(0);
        const seen = await watchFor(page, [MEMBER, OUTSIDE]);
        await expect(page.getByRole("radio")).toHaveCount(0);
        await expect(
            page.getByRole("button", { name: "Hatırlatıcıları kaydet" }),
        ).toHaveCount(0);
        await speak(page, "en");
        await english.committed;
        expect(await seen()).toEqual([]);
        await expect(saveButton(page)).toHaveCount(0);
        const late = answerTo(page, await turkish.request);
        turkish.release();
        await late;
        const tenants = nextRequest(page, isTenants);
        await page.clock.fastForward("00:10");
        await page.evaluate(() =>
            window.dispatchEvent(new Event("visibilitychange")),
        );
        await (await tenants).response();
        await expect(line(page, COUNT_ONLY(3))).toBeVisible();
        await expect(typedFields(page)).toHaveCount(0);
        expect(await seen()).toEqual([]);
        const fresh = answerTo(page, await english.request);
        english.release();
        await fresh;
        await expect(line(page, COUNT_ONLY(3))).toBeVisible();
        expect(await seen()).toEqual([]);
        await expect(saveButton(page)).toHaveCount(0);
    } finally {
        turkish.release();
        english.release();
    }
    role = "admin";
    answersViewer = false;
    const tenants = nextRequest(page, isTenants);
    await refetch(page);
    await (await tenants).response();
    await expect(typedFields(page)).toHaveValue(OUTSIDE);
    await expect(saveButton(page)).toBeEnabled();
});

test("losing administration while the review is open closes it, sends nothing and discards the draft", async ({
    page,
}) => {
    let role: "admin" | "viewer" = "admin";
    await page.clock.install();
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        viewer: (method) => role === "viewer" && method === "GET",
    });
    await asRole(page, () => role);
    const writes = sent(page, isWrite);
    await openReminders(page);
    await radio(page, OUTSIDE, "Copy").check();
    await time(page, 1).fill("09:00");
    await review(page);
    role = "viewer";
    const tenants = nextRequest(page, isTenants);
    await page.clock.fastForward("00:10");
    await page.evaluate(() =>
        window.dispatchEvent(new Event("visibilitychange")),
    );
    await (await tenants).response();
    await expect(dialog(page)).toHaveCount(0);
    await expect(typedFields(page)).toHaveCount(0);
    await expect(line(page, COUNT_ONLY(3))).toBeVisible();
    expect(writes).toEqual([]);
    role = "admin";
    await refetch(page);
    await expect(radio(page, OUTSIDE, "Default")).toBeChecked();
    await expect(time(page, 1)).toHaveValue("08:00");
    expect(writes).toEqual([]);
});

test("a save the server refuses because administration moved, after the review, shows nothing of the addresses again and says that nothing was saved", async ({
    page,
}) => {
    let transferred = false;
    await page.clock.install();
    const { answerLater } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        viewer: () => transferred,
        deliveries: [SENT_TO_TEAM],
    });
    await asRole(page, () => (transferred ? "viewer" : "admin"));
    const after = order(page);
    await openReminders(page);
    await page.getByText("Recent deliveries", { exact: true }).click();
    await expect(page.getByText(`${MEMBER} Copy`)).toBeVisible();
    await time(page, 1).fill("09:00");
    await review(page);
    transferred = true;
    const following = answerLater("GET");
    try {
        const put = nextRequest(page, isWrite);
        const tenantsRead = nextRequest(page, isTenants);
        await confirmButton(page).click();
        const write = await put;
        const refusal = (await write.response())!;
        expect([refusal.status(), (await refusal.json()).code]).toEqual([
            403,
            "workspace_admin_required",
        ]);
        await expect(line(page, LOST)).toBeFocused();
        const seen = await watchFor(page, [MEMBER, OUTSIDE]);
        expect(await seen()).toEqual([]);
        await expect(dialog(page)).toHaveCount(0);
        await expect(page.getByRole("radio")).toHaveCount(0);
        await expect(saveButton(page)).toHaveCount(0);
        await expect(typedFields(page)).toHaveCount(0);
        const held = await following.request;
        const answered = answerTo(page, held);
        following.release();
        await answered;
        await expect(line(page, COUNT_ONLY(3))).toBeVisible();
        const tenants = await tenantsRead;
        expect(after(write, isTenants)).toContain(tenants);
        await tenants.response();
        expect(after(write, isRead)[0]).toBe(held);
        await expect(saveButton(page)).toHaveCount(0);
        expect(await seen()).toEqual([]);
    } finally {
        following.release();
    }
    transferred = false;
    await refetch(page);
    await expect(time(page, 1)).toHaveValue("08:00");
});

for (const visible of [true, false])
    test(`a ${visible ? "review" : "hidden save"} the server refuses because administration moved before Save ends the editing the same way`, async ({
        page,
    }) => {
        let transferred = false;
        const { answerLater } = await reminderApi(page, {
            recipients: [OUTSIDE],
            members: TEAM,
            includeMembers: true,
            hidden: !visible,
            viewer: () => transferred,
            deliveries: [SENT_TO_TEAM],
        });
        await asRole(page, () => (transferred ? "viewer" : "admin"));
        const after = order(page);
        await openReminders(page);
        await time(page, 1).fill("09:00");
        transferred = true;
        const following = answerLater("GET");
        try {
            const refused = nextRequest(page, visible ? isPreview : isWrite);
            const tenantsRead = nextRequest(page, isTenants);
            await saveButton(page).click();
            const request = await refused;
            const refusal = (await request.response())!;
            expect([refusal.status(), (await refusal.json()).code]).toEqual([
                403,
                "workspace_admin_required",
            ]);
            await expect(line(page, LOST)).toBeFocused();
            const seen = await watchFor(page, [MEMBER, OUTSIDE]);
            expect(await seen()).toEqual([]);
            await expect(dialog(page)).toHaveCount(0);
            await expect(saveButton(page)).toHaveCount(0);
            const held = await following.request;
            const answered = answerTo(page, held);
            following.release();
            await answered;
            await expect(line(page, COUNT_ONLY(3))).toBeVisible();
            expect(after(request, isRead)[0]).toBe(held);
            const tenants = await tenantsRead;
            expect(after(request, isTenants)).toContain(tenants);
            await tenants.response();
            expect(await seen()).toEqual([]);
            expect(after(request, isWrite)).toEqual([]);
        } finally {
            following.release();
        }
    });

test("an administrator answer read before administration moved never returns after the read that follows the loss fails", async ({
    page,
}) => {
    let role: "admin" | "viewer" = "admin";
    await page.clock.install();
    const { answerLater, failNext } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await asRole(page, () => role);
    await openReminders(page);
    await expect(typedFields(page)).toHaveValue(OUTSIDE);
    role = "viewer";
    failNext("GET");
    failNext("GET");
    await page.clock.fastForward("00:10");
    await page.evaluate(() =>
        window.dispatchEvent(new Event("visibilitychange")),
    );
    await expect(panel(page).getByRole("alert")).toHaveText(
        `${UNAVAILABLE}Try again`,
    );
    await expect(retryButton(page)).toHaveCount(1);
    await expect(typedFields(page)).toHaveCount(0);
    await saveElsewhere(page, { ...SAVED, recipients: [THIRD] });
    role = "admin";
    const seen = await watchFor(page, [OUTSIDE]);
    const following = answerLater("GET");
    try {
        const tenants = nextRequest(page, isTenants);
        await page.clock.fastForward("00:10");
        await page.evaluate(() =>
            window.dispatchEvent(new Event("visibilitychange")),
        );
        await (await tenants).response();
        const held = await following.request;
        await expect(typedFields(page)).toHaveCount(0);
        expect(await seen()).toEqual([]);
        const answered = answerTo(page, held);
        following.release();
        await answered;
        await expect(typedFields(page)).toHaveValue(THIRD);
        expect(await seen()).toEqual([]);
    } finally {
        following.release();
    }
});

test("returning to the reminders while the read after a save is held shows the loading line and then the saved configuration", async ({
    page,
}) => {
    const { answerLater } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: true,
    });
    await openReminders(page);
    await expect(line(page, HIDDEN_TODAY)).toBeVisible();
    await hideBox(page).uncheck();
    await review(page);
    const following = answerLater("GET");
    try {
        await confirmButton(page).click();
        await following.committed;
        await page
            .getByRole("button", { name: "Location", exact: true })
            .click();
        await page
            .getByRole("button", { name: "Email reminders", exact: true })
            .click();
        await expect(panel(page).getByRole("status")).toHaveText(LOADING);
        await expect(saveButton(page)).toHaveCount(0);
        await expect(typedFields(page)).toHaveCount(0);
        await expect(hideBox(page)).toHaveCount(0);
        await expect(line(page, HIDDEN_TODAY)).toHaveCount(0);
        const answered = answerTo(page, await following.request);
        following.release();
        await answered;
    } finally {
        following.release();
    }
    await expect(hideBox(page)).not.toBeChecked();
    await expect(listed(savedView(page))).toHaveText([
        "To",
        ADMIN,
        "Copy (Cc)",
        MEMBER,
    ]);
    await expect(line(page, HIDDEN_TODAY)).toHaveCount(0);
});

test("leaving the reminders while a hidden save is held and returning after its answer shows no answer from before the save", async ({
    page,
}) => {
    const SUMMARY =
        "Everyone who receives the email, hidden addresses included, sees the To and Copy addresses.";
    const { answerLater } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await expect(line(page, SUMMARY)).toBeVisible();
    await hideBox(page).check();
    const write = answerLater("PUT");
    try {
        await saveButton(page).click();
        await write.committed;
        await page
            .getByRole("button", { name: "Location", exact: true })
            .click();
        await expect(hideBox(page)).toHaveCount(0);
        const seen = await watchFor(page, [SUMMARY]);
        const held = await write.request;
        const answered = answerTo(page, held);
        const tenants = nextRequest(page, isTenants);
        write.release();
        await answered;
        await (await tenants).response();
        await page
            .getByRole("button", { name: "Email reminders", exact: true })
            .click();
        await expect(line(page, HIDDEN_TODAY)).toBeVisible();
        await expect(hideBox(page)).toBeChecked();
        expect(await seen()).toEqual([]);
    } finally {
        write.release();
    }
});

test("a save whose following read fails leaves one Try again, the reader's, and it opens the saved read for editing", async ({
    page,
}) => {
    const { failNext } = await reminderApi(page);
    await openReminders(page);
    await page.getByRole("button", { name: "Add time", exact: true }).click();
    await time(page, 2).fill("07:00");
    failNext("GET");
    const put = nextRequest(page, isWrite);
    const read = nextRequest(page, isRead);
    await saveButton(page).click();
    await (await put).response();
    await expect(line(page, SAVED_LINE)).toBeFocused();
    expect((await (await read).response())!.status()).toBe(503);
    await expect(panel(page).getByRole("alert")).toHaveText(
        `${UNAVAILABLE}Try again`,
    );
    await expect(retryButton(page)).toHaveCount(1);
    await expect(saveButton(page)).toHaveCount(0);
    await expect(time(page, 1)).toHaveCount(0);
    const again = nextRequest(page, isRead);
    await retryButton(page).click();
    await (await again).response();
    await expect(time(page, 1)).toHaveValue("07:00");
    await expect(time(page, 2)).toHaveValue("08:00");
    await expect(saveButton(page)).toBeEnabled();
    await expect(panel(page).getByRole("alert")).toHaveCount(0);
    await expect(retryButton(page)).toHaveCount(0);
});

test("a language switch while the read after a save is held and fails leaves no failure once the replacement read succeeds", async ({
    page,
}) => {
    const { answerLater, failNext } = await reminderApi(page);
    await speaking(page);
    await openReminders(page);
    await time(page, 1).fill("07:00");
    const following = answerLater("GET");
    failNext("GET");
    try {
        await saveButton(page).click();
        const held = await following.request;
        const replacement = nextRequest(page, isRead);
        await speak(page, "tr");
        await (await replacement).response();
        await expect(
            page.getByLabel("Gönderim saati 1", { exact: true }),
        ).toHaveValue("07:00");
        const failure = await watchFor(page, [UNAVAILABLE, "Tekrar dene"]);
        const answered = answerTo(page, held);
        following.release();
        expect((await answered).status()).toBe(503);
        await expect(
            page.getByLabel("Gönderim saati 1", { exact: true }),
        ).toHaveValue("07:00");
        await expect(panel(page).getByRole("alert")).toHaveCount(0);
        const later = nextRequest(page, isRead);
        await speak(page, "en");
        await (await later).response();
        expect(await failure()).toEqual([]);
        await expect(time(page, 1)).toHaveValue("07:00");
        await expect(panel(page).getByRole("alert")).toHaveCount(0);
        await expect(retryButton(page)).toHaveCount(0);
    } finally {
        following.release();
    }
});

test("after a conflict Save returns only with the read that follows, and the next review carries its revision", async ({
    page,
}) => {
    const { answerLater } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await time(page, 1).fill("09:00");
    await review(page);
    await saveElsewhere(page, { ...SAVED, times: ["10:00"] });
    const following = answerLater("GET");
    try {
        const put = nextRequest(page, isWrite);
        await confirmButton(page).click();
        const refusal = (await (await put).response())!;
        expect([refusal.status(), (await refusal.json()).code]).toEqual([
            409,
            "reminder_schedule_changed",
        ]);
        await following.committed;
        await expect(line(page, CONFLICT)).toBeFocused();
        await expect(saveButton(page)).toHaveCount(0);
        await expect(typedFields(page)).toHaveCount(0);
        const answered = answerTo(page, await following.request);
        following.release();
        await answered;
    } finally {
        following.release();
    }
    await expect(saveButton(page)).toBeEnabled();
    await expect(time(page, 1)).toHaveValue("09:00");
    const { previewed } = await review(page);
    expect(previewed).toMatchObject({ expected_revision: 1, times: ["09:00"] });
});

for (const touched of [false, true]) {
    test(`a visible save from another tab shows its Copy in the saved view and nothing says everyone is hidden${touched ? ", with an edited time kept" : ""}`, async ({
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
        await expect(line(page, HIDDEN_TODAY)).toBeVisible();
        await expect(line(page, HIDDEN_HELPER)).toBeVisible();
        if (touched) await time(page, 1).fill("09:00");
        await saveElsewhere(page, {
            ...SAVED,
            address_roles: [{ address: OUTSIDE, role: "cc", memberships: [] }],
        });
        await refetch(page);
        await expect(listed(savedView(page))).toHaveText([
            "To",
            ADMIN,
            "Copy (Cc)",
            MEMBER,
            `${OUTSIDE}${OUTSIDE_MARK}`,
        ]);
        await expect(hideBox(page)).not.toBeChecked();
        await expect(line(page, HIDDEN_TODAY)).toHaveCount(0);
        await expect(line(page, HIDDEN_HELPER)).toHaveCount(0);
        await expect(
            page.getByText(/hidden copy|is hidden today|cannot see each other/),
        ).toHaveCount(0);
        await expect(radio(page, OUTSIDE, "Copy")).toBeChecked();
        await expect(time(page, 1)).toHaveValue(touched ? "09:00" : "08:00");
    });
}

test("a newer read stays in the saved view through a language switch whose read is held and fails, and the draft stays", async ({
    page,
}) => {
    await page.clock.install();
    const { answerLater, failNext } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await speaking(page);
    await openReminders(page);
    await time(page, 1).fill("09:00");
    await saveElsewhere(page, {
        ...SAVED,
        address_roles: [{ address: OUTSIDE, role: "cc", memberships: [] }],
    });
    await refetch(page);
    await expect(listed(savedView(page))).toContainText([
        `${OUTSIDE}${OUTSIDE_MARK}`,
    ]);
    const held = answerLater("GET");
    failNext("GET");
    try {
        await page.getByLabel("Language", { exact: true }).selectOption("tr");
        const request = await held.request;
        const turkish = page
            .getByRole("heading", { name: "Kim neyi görür", exact: true })
            .locator("xpath=..");
        await expect(listed(turkish)).toHaveText([
            "Kime",
            ADMIN,
            "Bilgi (Cc)",
            MEMBER,
            `${OUTSIDE}Aktif bir üyenin doğrulanmış birincil adresi değil`,
        ]);
        const failed = answerTo(page, request);
        held.release();
        expect((await failed).status()).toBe(503);
        await expect(
            page.getByRole("alert").filter({ hasText: /./ }),
        ).not.toHaveCount(0);
        await expect(listed(turkish)).toHaveText([
            "Kime",
            ADMIN,
            "Bilgi (Cc)",
            MEMBER,
            `${OUTSIDE}Aktif bir üyenin doğrulanmış birincil adresi değil`,
        ]);
        await expect(
            page.getByLabel("Gönderim saati 1", { exact: true }),
        ).toHaveValue("09:00");
    } finally {
        held.release();
    }
});

test("the review freezes the draft, a changed draft is reviewed again and the save sends exactly the reviewed body and digest", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    const previews = sent(page, isPreview);
    await openReminders(page);
    await review(page);
    const enabled = page.getByLabel("Enable forecast emails for this plant");
    await expect(enabled).toBeDisabled();
    await expect(time(page, 1)).toBeDisabled();
    await expect(radio(page, MEMBER, "Hidden")).toBeDisabled();
    await expect(
        page.getByRole("button", { name: "Saving…", exact: true }),
    ).toBeDisabled();
    await cancelButton(page).click();
    await expect(dialog(page)).toHaveCount(0);
    await expect(saveButton(page)).toBeFocused();
    await expect(line(page, SAVED_LINE)).toHaveCount(0);
    await enabled.check();
    const { previewed, answer, body } = await saveVisible(page);
    expect(previews).toHaveLength(2);
    expect(previews[0]).toMatchObject({ enabled: false });
    expect(previewed).toMatchObject({ enabled: true });
    expect(body).toEqual({ ...previewed, disclosure: answer.disclosure });
    await expect(enabled).toBeChecked();
});

test("a digest that differs at the save closes the review, keeps the draft and the next save reviews the new answer", async ({
    page,
}) => {
    const { persons } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: [
            ADMIN,
            MEMBER,
            { membership: 3, primary: OUTSIDE, verified: [] },
        ],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await time(page, 1).fill("09:00");
    await review(page);
    await expect(listed(dialog(page))).toHaveText([
        "To",
        ADMIN,
        "Copy (Cc)",
        MEMBER,
    ]);
    persons[2]!.verified = [OUTSIDE];
    const put = nextRequest(page, isWrite);
    const reading = nextRequest(page, isRead);
    const after = order(page);
    await confirmButton(page).click();
    const write = await put;
    const refused = (await write.response())!;
    expect([refused.status(), (await refused.json()).code]).toEqual([
        409,
        "reminder_disclosure_changed",
    ]);
    await expect(dialog(page)).toHaveCount(0);
    await expect(line(page, CONFLICT)).toBeFocused();
    const read = await reading;
    expect(after(write, isRead)[0]).toBe(read);
    await read.response();
    await expect(time(page, 1)).toHaveValue("09:00");
    await expect(listed(savedView(page))).toHaveText([
        "To",
        ADMIN,
        "Copy (Cc)",
        MEMBER,
        OUTSIDE,
    ]);
    await review(page);
    await expect(line(page, CONFLICT)).toHaveCount(0);
    await expect(listed(dialog(page))).toHaveText([
        "To",
        ADMIN,
        "Copy (Cc)",
        MEMBER,
        OUTSIDE,
    ]);
    await confirmButton(page).click();
    await expect(line(page, SAVED_LINE)).toBeFocused();
    await expect(saveButton(page)).toBeEnabled();
    await expect(time(page, 1)).toHaveValue("09:00");
});

test("a review whose answer carries no digest is refused at the save and ends on the conflict line with the draft kept", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        disclosure: false,
    });
    await openReminders(page);
    await time(page, 1).fill("09:00");
    const { answer } = await review(page);
    expect(answer).not.toHaveProperty("disclosure");
    const put = nextRequest(page, isWrite);
    const reading = nextRequest(page, isRead);
    const after = order(page);
    await confirmButton(page).click();
    const write = await put;
    expect(write.postDataJSON()).not.toHaveProperty("disclosure");
    const refused = (await write.response())!;
    expect([refused.status(), (await refused.json()).code]).toEqual([
        409,
        "reminder_disclosure_changed",
    ]);
    await expect(dialog(page)).toHaveCount(0);
    await expect(line(page, CONFLICT)).toBeFocused();
    const read = await reading;
    expect(after(write, isRead)[0]).toBe(read);
    await read.response();
    await expect(time(page, 1)).toHaveValue("09:00");
    const { previewed } = await review(page);
    expect(previewed).toMatchObject({ times: ["09:00"] });
});

for (const after of [true, false])
    test(`a save whose answer is ${after ? "lost after the write" : "a server failure before the write"} says the outcome is unknown, keeps the draft and reviews the next save on what is stored`, async ({
        page,
    }) => {
        const { answerLater, failNext } = await reminderApi(page, {
            recipients: [OUTSIDE],
            members: TEAM,
            includeMembers: true,
            hidden: false,
        });
        await openReminders(page);
        await radio(page, MEMBER, "To").check();
        await review(page);
        const write = answerLater("PUT");
        if (after) failNext("PUT", "after", "lost");
        else failNext("PUT");
        try {
            await confirmButton(page).click();
            const request = await write.request;
            const reading = nextRequest(page, isRead);
            if (after) {
                const lost = page.waitForEvent(
                    "requestfailed",
                    (failed) => failed === request,
                );
                write.release();
                await lost;
            } else {
                const answered = answerTo(page, request);
                write.release();
                expect((await answered).status()).toBe(503);
            }
            await expect(dialog(page)).toHaveCount(0);
            await expect(line(page, KEPT(UNKNOWN))).toBeFocused();
            await expect(confirmButton(page)).toHaveCount(0);
            await (await reading).response();
        } finally {
            write.release();
        }
        await expect(listed(savedView(page))).toHaveText(
            after ? ["To", ADMIN, MEMBER] : ["To", ADMIN, "Copy (Cc)", MEMBER],
        );
        await expect(radio(page, MEMBER, "To")).toBeChecked();
        await expect(line(page, KEPT(UNKNOWN))).toBeVisible();
        const { previewed } = await review(page);
        expect(previewed).toMatchObject({
            expected_revision: after ? 1 : 0,
            member_roles: [{ memberships: [2], address: MEMBER, role: "to" }],
        });
        await confirmButton(page).click();
        await expect(line(page, SAVED_LINE)).toBeFocused();
    });

test("a hidden result is saved with one PUT, without a preview or a review, and the reader's loading line stands in until the read that follows", async ({
    page,
}) => {
    const { answerLater } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    const previews = sent(page, isPreview);
    const writes = sent(page, isWrite);
    await openReminders(page);
    await hideBox(page).check();
    await expect(line(page, HIDDEN_HELPER)).toBeVisible();
    await expect(page.getByRole("radio")).toHaveCount(0);
    const following = answerLater("GET");
    let body: Record<string, unknown>;
    try {
        const request = nextRequest(page, isWrite);
        await saveButton(page).click();
        body = (await request).postDataJSON();
        await following.committed;
        await expect(line(page, SAVED_LINE)).toBeFocused();
        await expect(panel(page).getByRole("status")).toHaveText(LOADING);
        await expect(saveButton(page)).toHaveCount(0);
        await expect(heading(page)).toHaveCount(0);
        const answered = answerTo(page, await following.request);
        following.release();
        await answered;
    } finally {
        following.release();
    }
    await expect(saveButton(page)).toBeEnabled();
    await expect(line(page, SAVED_LINE)).toBeFocused();
    expect(body).toMatchObject({ hide_recipients: true });
    expect(body).not.toHaveProperty("disclosure");
    expect(previews).toEqual([]);
    expect(writes).toHaveLength(1);
    await expect(dialog(page)).toHaveCount(0);
    await expect(line(page, HIDDEN_TODAY)).toBeVisible();
});

test("a confirmed visible save moves focus to the outcome line after the dialog has closed", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await review(page);
    await confirmButton(page).click();
    await expect(line(page, SAVED_LINE)).toBeFocused();
    await expect(dialog(page)).toHaveCount(0);
    expect(
        await page.evaluate(() => ({
            inDialog: !!document.activeElement?.closest("dialog"),
            open: document.querySelectorAll("dialog[open]").length,
        })),
    ).toEqual({ inDialog: false, open: 0 });
});

test("a new typed address offers its role only once it is saved", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: [ADMIN],
        hidden: false,
    });
    await openReminders(page);
    await page
        .getByRole("button", { name: "Add address", exact: true })
        .click();
    await address(page, 2).fill("new@example.test");
    await expect(group(page, "new@example.test")).toHaveCount(0);
    await expect(line(page, ONCE_SAVED)).toBeVisible();
    await expect(address(page, 2)).toHaveAccessibleDescription(ONCE_SAVED);
    const { body } = await saveVisible(page);
    expect(body.recipients).toEqual([OUTSIDE, "new@example.test"]);
    expect(body).not.toHaveProperty("address_roles");
    await expect(radio(page, "new@example.test", "Default")).toBeChecked();
    await expect(line(page, ONCE_SAVED)).toHaveCount(0);
});

test("a choice for a member who leaves is removed with a line naming the choice and the address, and the rest of the draft stays", async ({
    page,
}) => {
    await page.clock.install();
    const { persons } = await reminderApi(page, {
        recipients: [],
        members: [ADMIN, MEMBER, THIRD],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await radio(page, MEMBER, "Hidden").check();
    await radio(page, THIRD, "To").check();
    await time(page, 1).fill("09:00");
    persons[1]!.active = false;
    await refetch(page);
    await expect(line(page, DROPPED("Hidden", MEMBER))).toBeVisible();
    await expect(radio(page, THIRD, "To")).toBeChecked();
    await expect(time(page, 1)).toHaveValue("09:00");
    const { body } = await saveVisible(page);
    expect(body.member_roles).toEqual([
        { memberships: [3], address: THIRD, role: "to" },
    ]);
    expect(body.times).toEqual(["09:00"]);
    await expect(line(page, DROPPED("Hidden", MEMBER))).toHaveCount(0);
});

test("a choice on a typed address whose row went elsewhere is dropped and named, never moved to the member who now holds the address", async ({
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
        hidden: false,
    });
    await openReminders(page);
    await radio(page, OUTSIDE, "Copy").check();
    await saveElsewhere(page, { ...SAVED, recipients: [] });
    persons[2]!.verified = [OUTSIDE];
    await refetch(page);
    await expect(line(page, DROPPED("Copy", OUTSIDE))).toBeVisible();
    await expect(typedFields(page)).toHaveValue("");
    await expect(group(page, OUTSIDE)).toHaveCount(1);
    await expect(radio(page, OUTSIDE, "Default")).toBeChecked();
    await expect(radio(page, OUTSIDE, "Copy")).not.toBeChecked();
    const { body } = await saveVisible(page);
    expect(body).not.toHaveProperty("address_roles");
    expect(body).not.toHaveProperty("member_roles");
});

for (const how of ["Use the defaults for everyone", "Copy, then Default"])
    test(`a Default chosen on a typed row whose stored To rests is sent as a clear in both lists (${how})`, async ({
        page,
    }) => {
        await reminderApi(page, {
            recipients: [MEMBER],
            members: [ADMIN, { membership: 2, primary: MEMBER }],
            typed: { [MEMBER]: { role: "to", members: [] } },
            includeMembers: true,
            hidden: false,
        });
        await openReminders(page);
        await expect(radio(page, MEMBER, "Default")).toBeChecked();
        await expect(
            page.getByRole("listitem").filter({ hasText: MEMBER }),
        ).toContainText("Set under Other addresses");
        if (how === "Copy, then Default") {
            await radio(page, MEMBER, "Copy").check();
            await radio(page, MEMBER, "Default").check();
        } else await useDefaults(page).click();
        const { body } = await saveVisible(page);
        expect(body.address_roles).toEqual([
            { address: MEMBER, role: null, memberships: [2] },
        ]);
        expect(body.member_roles).toEqual(
            how === "Copy, then Default"
                ? [{ memberships: [2], address: MEMBER, role: null }]
                : [
                      { memberships: [1], address: ADMIN, role: null },
                      { memberships: [2], address: MEMBER, role: null },
                  ],
        );
    });

test("an active member keeps their own role control beside a typed row of their address that adds no recipient", async ({
    page,
}) => {
    const SHARED = "shared@example.test";
    await page.clock.install();
    const { persons } = await reminderApi(page, {
        recipients: [SHARED],
        members: [
            ADMIN,
            { membership: 2, primary: SHARED, active: false },
            { membership: 3, primary: "later@example.test" },
        ],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    persons[2]!.primary = SHARED;
    persons[2]!.verified = [SHARED];
    await refetch(page);
    const memberRow = page.getByRole("listitem").filter({ hasText: SHARED });
    await expect(memberRow.getByRole("radio")).toHaveCount(4);
    await expect(memberRow).not.toContainText("Set under Other addresses");
    await expect(line(page, `${SHARED} adds no recipient.`)).toBeVisible();
    await expect(group(page, SHARED)).toHaveCount(1);
    await radio(page, SHARED, "Copy").check();
    const { body } = await saveVisible(page);
    expect(body.member_roles).toEqual([
        { memberships: [3], address: SHARED, role: "cc" },
    ]);
    expect(body).not.toHaveProperty("address_roles");
});

for (const cause of ["a departure hides everyone", "the address becomes typed"])
    test(`when ${cause}, focus on a removed member control moves to the panel heading`, async ({
        page,
    }) => {
        await page.clock.install();
        const { depart } = await reminderApi(page, {
            recipients: [],
            members: [ADMIN, MEMBER, THIRD],
            includeMembers: true,
            hidden: false,
        });
        await openReminders(page);
        await time(page, 1).fill("09:00");
        await radio(page, MEMBER, "Default").focus();
        if (cause === "a departure hides everyone") depart(3);
        else await saveElsewhere(page, { ...SAVED, recipients: [MEMBER] });
        await refetch(page);
        if (cause === "a departure hides everyone") {
            await expect(line(page, DEPARTURE)).toBeVisible();
            await expect(group(page, MEMBER)).toHaveCount(0);
        } else
            await expect(
                page.getByRole("listitem").filter({ hasText: MEMBER }),
            ).toContainText("Set under Other addresses");
        await expect(panelHeading(page)).toBeFocused();
        await expect(time(page, 1)).toHaveValue("09:00");
    });

for (const reviewing of [false, true])
    test(`a save answered after administration was lost and regained leaves the later draft${reviewing ? " and its closed review's draft, with focus on the panel heading" : ", the outcome line and focus"} alone and reads again`, async ({
        page,
    }) => {
        let revoked = false;
        await page.clock.install();
        const { answerLater } = await reminderApi(page, {
            recipients: [OUTSIDE],
            members: TEAM,
            includeMembers: true,
            hidden: false,
            viewer: (method) => revoked && method === "GET",
        });
        await asRole(page, () => "admin");
        await openReminders(page);
        const enabled = page.getByLabel(
            "Enable forecast emails for this plant",
        );
        const language = page.getByLabel("Language", { exact: true });
        await time(page, 1).fill("09:00");
        await review(page);
        const write = answerLater("PUT");
        try {
            await confirmButton(page).click();
            await write.committed;
            revoked = true;
            await refetch(page);
            await expect(line(page, COUNT_ONLY(3))).toBeVisible();
            await expect(typedFields(page)).toHaveCount(0);
            await saveElsewhere(page, { ...SAVED, times: ["10:00"] });
            revoked = false;
            await refetch(page);
            await expect(time(page, 1)).toHaveValue("10:00");
            await enabled.check();
            if (reviewing) await review(page);
            else await language.focus();
            const held = await write.request;
            const answered = answerTo(page, held);
            const reading = nextRequest(page, isRead);
            write.release();
            await answered;
            await (await reading).response();
        } finally {
            write.release();
        }
        await expect(saveButton(page)).toBeEnabled();
        await expect(enabled).toBeChecked();
        await expect(time(page, 1)).toHaveValue("10:00");
        await expect(line(page, SAVED_LINE)).toHaveCount(0);
        await expect(dialog(page)).toHaveCount(0);
        await expect(reviewing ? panelHeading(page) : language).toBeFocused();
    });

async function atOutcomeFocus(page: Page, needles: string[]) {
    await page.evaluate((needles) => {
        const seen: unknown[] = [];
        document.addEventListener("focusin", (event) => {
            const target = event.target as Element;
            const section = target.closest("section");
            if (target.tagName !== "P" || !target.closest("[aria-live]"))
                return;
            const shown = [
                document.body.textContent ?? "",
                ...[...document.querySelectorAll("input")].map(
                    (input) => input.value,
                ),
            ].join("\n");
            seen.push({
                addresses: needles.filter((needle) => shown.includes(needle)),
                saves: section?.querySelectorAll("button[type=submit]").length,
                dialogs: document.querySelectorAll("dialog").length,
                radios: section?.querySelectorAll("input[type=radio]").length,
            });
        });
        (window as unknown as { atFocus: unknown[] }).atFocus = seen;
    }, needles);
    return () =>
        page.evaluate(
            () => (window as unknown as { atFocus: unknown[] }).atFocus,
        );
}

for (const ending of ["refused", "saved", "changed", "unknown"] as const)
    test(`when a confirmed save ends ${ending}, the outcome line takes focus in a view without the old answer, its form or its review`, async ({
        page,
    }) => {
        const { failNext } = await reminderApi(page, {
            recipients: [OUTSIDE],
            members: TEAM,
            includeMembers: true,
            hidden: false,
            deliveries: [SENT_TO_TEAM],
        });
        await openReminders(page);
        await page.getByText("Recent deliveries", { exact: true }).click();
        await expect(page.getByText(`${MEMBER} Copy`)).toBeVisible();
        await time(page, 1).fill("09:00");
        await review(page);
        if (ending === "refused")
            failNext("PUT", "before", {
                status: 403,
                code: "workspace_admin_required",
            });
        if (ending === "changed")
            await saveElsewhere(page, { ...SAVED, times: ["10:00"] });
        if (ending === "unknown") failNext("PUT");
        const focused = await atOutcomeFocus(page, [MEMBER, OUTSIDE]);
        await confirmButton(page).click();
        await expect(
            line(
                page,
                {
                    refused: LOST,
                    saved: SAVED_LINE,
                    changed: CONFLICT,
                    unknown: KEPT(UNKNOWN),
                }[ending],
            ),
        ).toBeFocused();
        expect(await focused()).toEqual([
            { addresses: [], saves: 0, dialogs: 0, radios: 0 },
        ]);
    });

test("an administrator answer cached before administration moved is discarded when a viewer opens the reminders again and never returns", async ({
    page,
}) => {
    let role: "admin" | "viewer" = "admin";
    let unavailable = false;
    await page.clock.install();
    const { answerLater } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await page.route("**/reminders/", (route) =>
        unavailable && route.request().method() === "GET"
            ? route.fulfill({
                  status: 503,
                  json: errorBody("server_error", UNAVAILABLE),
              })
            : route.fallback(),
    );
    await asRole(page, () => role);
    await openReminders(page);
    await expect(typedFields(page)).toHaveValue(OUTSIDE);
    await page.getByRole("button", { name: "Location", exact: true }).click();
    await expect(typedFields(page)).toHaveCount(0);
    role = "viewer";
    await tenantsAgain(page);
    await saveElsewhere(page, { ...SAVED, recipients: [THIRD] });
    unavailable = true;
    const seen = await watchFor(page, [OUTSIDE]);
    await page
        .getByRole("button", { name: "Email reminders", exact: true })
        .click();
    await expect(panel(page).getByRole("alert")).toContainText(UNAVAILABLE);
    unavailable = false;
    role = "admin";
    const following = answerLater("GET");
    try {
        await tenantsAgain(page);
        const held = await following.request;
        await expect(typedFields(page)).toHaveCount(0);
        expect(await seen()).toEqual([]);
        const answered = answerTo(page, held);
        following.release();
        await answered;
        await expect(typedFields(page)).toHaveValue(THIRD);
        expect(await seen()).toEqual([]);
    } finally {
        following.release();
    }
});

test("a viewer whose reminders read still carries addresses discards it once and reads no more", async ({
    page,
}) => {
    let role: "admin" | "viewer" = "admin";
    await page.clock.install();
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await asRole(page, () => role);
    await speaking(page);
    const after = order(page);
    await openReminders(page);
    await expect(typedFields(page)).toHaveValue(OUTSIDE);
    await page.getByRole("button", { name: "Location", exact: true }).click();
    role = "viewer";
    await tenantsAgain(page);
    await page
        .getByRole("button", { name: "Email reminders", exact: true })
        .click();
    await expect(line(page, COUNT_ONLY(3))).toBeVisible();
    const switched = nextRequest(
        page,
        (method, url) =>
            method === "POST" && url.endsWith("/api/session/language/"),
    );
    const read = nextRequest(page, isRead);
    await speak(page, "tr");
    const post = await switched;
    await (await read).response();
    await expect(
        page.getByText(
            "En fazla 5 alıcıdan 3 alıcı. Adresleri bu uygulamada yalnızca çalışma alanı yöneticisi görür.",
            { exact: true },
        ),
    ).toBeVisible();
    expect(after(post, isRead)).toEqual([await read]);
});

for (const cause of [
    "a count-only read",
    "a refused read",
    "a tenant read saying viewer",
] as const)
    test(`the address in a dropped-choice line is never shown once ${cause} has removed the editor`, async ({
        page,
    }) => {
        let role: "admin" | "viewer" = "admin";
        let counting = false;
        await page.clock.install();
        const { persons, failNext } = await reminderApi(page, {
            recipients: [],
            members: [ADMIN, MEMBER, THIRD],
            includeMembers: true,
            hidden: false,
            viewer: (method) => counting && method === "GET",
        });
        await asRole(page, () => role);
        await openReminders(page);
        await radio(page, THIRD, "Hidden").check();
        persons[2]!.active = false;
        await refetch(page);
        await expect(line(page, DROPPED("Hidden", THIRD))).toBeVisible();
        await page.evaluate((needle) => {
            const alone: string[] = [];
            new MutationObserver(() => {
                if (
                    !document.querySelector('input[name="recipients"]') &&
                    (document.body.textContent ?? "").includes(needle)
                )
                    alone.push(needle);
            }).observe(document.body, {
                subtree: true,
                childList: true,
                characterData: true,
            });
            (window as unknown as { alone: string[] }).alone = alone;
        }, THIRD);
        if (cause === "a count-only read") counting = true;
        if (cause === "a refused read")
            failNext("GET", "before", {
                status: 403,
                code: "workspace_admin_required",
            });
        if (cause === "a tenant read saying viewer") role = "viewer";
        await tenantsAgain(page);
        await expect(line(page, DROPPED("Hidden", THIRD))).toHaveCount(0);
        expect(
            await page.evaluate(
                () => (window as unknown as { alone: string[] }).alone,
            ),
        ).toEqual([]);
    });

test("a member's choice whose address another save gives to a typed row is dropped and named, and the typed row's stored role applies", async ({
    page,
}) => {
    await page.clock.install();
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await radio(page, MEMBER, "Hidden").check();
    await saveElsewhere(page, {
        ...SAVED,
        recipients: [OUTSIDE, MEMBER],
        address_roles: [{ address: MEMBER, role: "cc", memberships: [2] }],
    });
    await refetch(page);
    await expect(line(page, DROPPED("Hidden", MEMBER))).toBeVisible();
    const memberRow = page.getByRole("listitem").filter({ hasText: MEMBER });
    await expect(memberRow).toContainText("Set under Other addresses");
    await expect(memberRow.getByRole("radio")).toHaveCount(0);
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    const { body } = await saveVisible(page);
    expect(body).not.toHaveProperty("member_roles");
    expect(body).not.toHaveProperty("address_roles");
    await expect(listed(savedView(page))).toHaveText([
        "To",
        ADMIN,
        "Copy (Cc)",
        MEMBER,
    ]);
});

test("a member beside a typed row of their address removed in the form has no role control, and their dropped choice does not come back", async ({
    page,
}) => {
    await page.clock.install();
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await radio(page, MEMBER, "Hidden").check();
    await saveElsewhere(page, {
        ...SAVED,
        recipients: [OUTSIDE, MEMBER],
        address_roles: [{ address: MEMBER, role: "cc", memberships: [2] }],
    });
    await refetch(page);
    await expect(address(page, 2)).toHaveValue(MEMBER);
    await page
        .getByRole("button", { name: "Remove email address 2", exact: true })
        .click();
    const memberRow = page.getByRole("listitem").filter({ hasText: MEMBER });
    await expect(memberRow.getByRole("radio")).toHaveCount(0);
    await expect(memberRow).toHaveText(`${MEMBER}Member`);
    await expect(group(page, MEMBER)).toHaveCount(0);
    await expect(line(page, DROPPED("Hidden", MEMBER))).toBeVisible();
    const { body } = await saveVisible(page);
    expect(body.recipients).toEqual([OUTSIDE]);
    expect(body).not.toHaveProperty("member_roles");
});

test("a typed choice whose row is removed and typed again applies again without a notice", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE, THIRD],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await radio(page, OUTSIDE, "Copy").check();
    await page
        .getByRole("button", { name: "Remove email address 1", exact: true })
        .click();
    await expect(group(page, OUTSIDE)).toHaveCount(0);
    await page
        .getByRole("button", { name: "Add address", exact: true })
        .click();
    await address(page, 2).fill(OUTSIDE);
    await expect(radio(page, OUTSIDE, "Copy")).toBeChecked();
    await expect(page.getByText(/was removed:/)).toHaveCount(0);
    const { body } = await saveVisible(page);
    expect(body.recipients).toEqual([THIRD, OUTSIDE]);
    expect(body.address_roles).toEqual([
        { address: OUTSIDE, role: "cc", memberships: [] },
    ]);
});

for (const variant of ["save", "preview", "refusal"] as const)
    test(`a ${variant === "preview" ? "review" : "save"} answered ${variant === "refusal" ? "403" : "409"} without a body ${variant === "refusal" ? "clears the draft and reads the tenants" : "keeps the draft and reviews the next save on the read that follows"}, with a line that names no cause`, async ({
        page,
    }) => {
        const { failNext } = await reminderApi(page, {
            recipients: [OUTSIDE],
            members: TEAM,
            includeMembers: true,
            hidden: false,
        });
        const after = order(page);
        await openReminders(page);
        await time(page, 1).fill("09:00");
        if (variant !== "preview") await review(page);
        await saveElsewhere(page, { ...SAVED, times: ["10:00"] });
        failNext(variant === "preview" ? "POST" : "PUT", "before", {
            status: variant === "refusal" ? 403 : 409,
        });
        const answered = nextRequest(
            page,
            variant === "preview" ? isPreview : isWrite,
        );
        const reading = nextRequest(page, isRead);
        const tenantsRead =
            variant === "refusal" ? nextRequest(page, isTenants) : undefined;
        await (
            variant === "preview" ? saveButton(page) : confirmButton(page)
        ).click();
        const request = await answered;
        expect((await request.response())!.status()).toBe(
            variant === "refusal" ? 403 : 409,
        );
        await expect(dialog(page)).toHaveCount(0);
        await expect(
            line(page, variant === "refusal" ? REFUSED : KEPT(REFUSED)),
        ).toBeFocused();
        const read = await reading;
        expect(after(request, isRead)[0]).toBe(read);
        await read.response();
        await expect(saveButton(page)).toBeEnabled();
        if (tenantsRead) {
            const tenants = await tenantsRead;
            expect(after(request, isTenants)).toContain(tenants);
            await tenants.response();
            await expect(time(page, 1)).toHaveValue("10:00");
            return;
        }
        await expect(time(page, 1)).toHaveValue("09:00");
        const { previewed } = await review(page);
        expect(previewed).toMatchObject({
            expected_revision: 1,
            times: ["09:00"],
        });
    });

for (const code of ["csrf_failed", "workspace_admin_required"] as const)
    test(`a save refused 403 ${code} while still administrator clears the draft, reads again and keeps a line that stays true beside the editor`, async ({
        page,
    }) => {
        const { failNext } = await reminderApi(page, {
            recipients: [OUTSIDE],
            members: TEAM,
            includeMembers: true,
            hidden: true,
        });
        const after = order(page);
        await openReminders(page);
        await time(page, 1).fill("09:00");
        failNext("PUT", "before", { status: 403, code });
        const put = nextRequest(page, isWrite);
        const reading = nextRequest(page, isRead);
        const tenantsRead = nextRequest(page, isTenants);
        await saveButton(page).click();
        const write = await put;
        const refusal = (await write.response())!;
        expect([refusal.status(), (await refusal.json()).code]).toEqual([
            403,
            code,
        ]);
        const shown = code === "csrf_failed" ? REFUSED : LOST;
        await expect(line(page, shown)).toBeFocused();
        const read = await reading;
        expect(after(write, isRead)[0]).toBe(read);
        const tenants = await tenantsRead;
        expect(after(write, isTenants)).toContain(tenants);
        await read.response();
        await tenants.response();
        await expect(saveButton(page)).toBeEnabled();
        await expect(time(page, 1)).toHaveValue("08:00");
        await expect(line(page, shown)).toBeVisible();
    });

test("a save whose outcome is unknown and whose following read carries no addresses no longer says that the edits are kept", async ({
    page,
}) => {
    let revoked = false;
    const { failNext } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        viewer: (method) => revoked && method === "GET",
    });
    await openReminders(page);
    await radio(page, MEMBER, "To").check();
    await review(page);
    failNext("PUT", "after", "lost");
    revoked = true;
    const reading = nextRequest(page, isRead);
    await confirmButton(page).click();
    await (await reading).response();
    await expect(line(page, COUNT_ONLY(3))).toBeVisible();
    await expect(line(page, UNKNOWN)).toBeFocused();
    await expect(line(page, KEPT(UNKNOWN))).toHaveCount(0);
});

test("a draft is saved against the read it was started on, so a save over a later change elsewhere is refused until the read that follows the refusal", async ({
    page,
}) => {
    await page.clock.install();
    const { answerLater } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: true,
    });
    const HIDDEN = { ...SAVED, hide_recipients: true };
    const writes = sent(page, isWrite);
    await openReminders(page);
    const include = page.getByLabel("Include workspace members", {
        exact: true,
    });
    await include.uncheck();
    await include.check();
    await saveElsewhere(page, { ...HIDDEN, include_workspace_members: false });
    await refetch(page);
    await expect(line(savedView(page), "1 / 5")).toBeVisible();
    await expect(include).toBeChecked();
    const following = answerLater("GET");
    try {
        const put = nextRequest(page, isWrite);
        await saveButton(page).click();
        const refusal = (await (await put).response())!;
        expect([refusal.status(), (await refusal.json()).code]).toEqual([
            409,
            "reminder_schedule_changed",
        ]);
        expect(writes.at(-1)).toMatchObject({
            include_workspace_members: true,
            expected_revision: 0,
        });
        await expect(line(page, CONFLICT)).toBeFocused();
        const answered = answerTo(page, await following.request);
        following.release();
        await answered;
    } finally {
        following.release();
    }
    await expect(include).toBeChecked();
    await saveElsewhere(page, {
        ...HIDDEN,
        include_workspace_members: false,
        times: ["10:00"],
    });
    await refetch(page);
    await expect(time(page, 1)).toHaveValue("10:00");
    const put = nextRequest(page, isWrite);
    await saveButton(page).click();
    const again = await put;
    expect(again.postDataJSON()).toMatchObject({
        include_workspace_members: true,
        expected_revision: 1,
    });
    const refusal = (await again.response())!;
    expect([refusal.status(), (await refusal.json()).code]).toEqual([
        409,
        "reminder_schedule_changed",
    ]);
    await expect(line(page, CONFLICT)).toBeFocused();
    await expect(include).toBeChecked();
});

test("an explicit Default kept over a newer stored choice is refused with the revision conflict", async ({
    page,
}) => {
    await page.clock.install();
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await radio(page, MEMBER, "Copy").check();
    await radio(page, MEMBER, "Default").check();
    await saveElsewhere(page, {
        ...SAVED,
        member_roles: [{ memberships: [2], address: MEMBER, role: "to" }],
    });
    await refetch(page);
    await expect(listed(savedView(page))).toHaveText(["To", ADMIN, MEMBER]);
    await expect(radio(page, MEMBER, "Default")).toBeChecked();
    const preview = nextRequest(page, isPreview);
    await saveButton(page).click();
    const request = await preview;
    expect(request.postDataJSON()).toMatchObject({
        expected_revision: 0,
        member_roles: [{ memberships: [2], address: MEMBER, role: null }],
    });
    const refusal = (await request.response())!;
    expect([refusal.status(), (await refusal.json()).code]).toEqual([
        409,
        "reminder_schedule_changed",
    ]);
    await expect(line(page, CONFLICT)).toBeFocused();
    await expect(dialog(page)).toHaveCount(0);
});

test("a newer administrator read with another placement during a held save, then a failed read and Try again, ends on the stored placement", async ({
    page,
}) => {
    await page.clock.install();
    const { persons, answerLater, failNext } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: [
            ADMIN,
            MEMBER,
            { membership: 3, primary: OUTSIDE, verified: [] },
        ],
        includeMembers: true,
        hidden: false,
    });
    const STORED = ["To", ADMIN, "Copy (Cc)", MEMBER, OUTSIDE];
    await openReminders(page);
    await review(page);
    const write = answerLater("PUT");
    try {
        await confirmButton(page).click();
        await write.committed;
        persons[2]!.verified = [OUTSIDE];
        await refetch(page);
        await expect(listed(savedView(page))).toHaveText(STORED);
        failNext("GET");
        const held = await write.request;
        const answered = answerTo(page, held);
        const reading = nextRequest(page, isRead);
        write.release();
        await answered;
        expect((await (await reading).response())!.status()).toBe(503);
        await expect(line(page, SAVED_LINE)).toBeFocused();
        await expect(panel(page).getByRole("alert")).toHaveText(
            `${UNAVAILABLE}Try again`,
        );
        await expect(retryButton(page)).toHaveCount(1);
        await expect(heading(page)).toHaveCount(0);
    } finally {
        write.release();
    }
    await retryButton(page).click();
    await expect(listed(savedView(page))).toHaveText(STORED);
    await expect(line(page, SAVED_LINE)).toBeVisible();
    await expect(retryButton(page)).toHaveCount(0);
});

test("focus on Recent deliveries goes to the panel heading when a loss discards the read", async ({
    page,
}) => {
    let revoked = false;
    await page.clock.install();
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        viewer: (method) => revoked && method === "GET",
    });
    await openReminders(page);
    const summary = page.getByText("Recent deliveries", { exact: true });
    await summary.focus();
    await expect(summary).toBeFocused();
    revoked = true;
    await refetch(page);
    await expect(line(page, COUNT_ONLY(3))).toBeVisible();
    await expect(panelHeading(page)).toBeFocused();
});

test("the review takes focus, confines the keyboard to its own controls, returns focus to Save on Escape and fits a narrow screen", async ({
    page,
}) => {
    const LONG =
        "a.very.long.recipient.address.outside.the.workspace@subdomain.example.test";
    await reminderApi(page, {
        recipients: [LONG],
        members: TEAM,
        includeMembers: true,
        hidden: false,
        typed: { [LONG]: "cc" },
    });
    await page.setViewportSize({ width: 320, height: 900 });
    await openReminders(page);
    await review(page);
    await expect(
        dialog(page).getByRole("heading", { name: TITLE }),
    ).toBeFocused();
    await expect(dialog(page)).toHaveAccessibleDescription(SENTENCE);
    await expect(listed(dialog(page))).toContainText([
        `${LONG}${OUTSIDE_MARK}`,
    ]);
    expect(await fits(page)).toBe(true);
    const box = await dialog(page).boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(320);
    const visited: string[] = [];
    for (let step = 0; step < 6; step++) {
        await page.keyboard.press("Tab");
        visited.push(
            await dialog(page).evaluate((element) => {
                const active = document.activeElement;
                if (!active || active === document.body) return "browser";
                return element.contains(active)
                    ? (active.textContent ?? "")
                    : `outside: ${active.outerHTML.slice(0, 60)}`;
            }),
        );
    }
    expect(visited).toEqual([
        "Save and show addresses",
        "Back to editing",
        "browser",
        "Save and show addresses",
        "Back to editing",
        "browser",
    ]);
    await page.setViewportSize({ width: 390, height: 900 });
    expect(await accessibilityViolations(page)).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(dialog(page)).toHaveCount(0);
    await expect(saveButton(page)).toBeFocused();
});

test("a visible review in which every recipient is hidden says that no address will appear in To or Copy", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    for (const item of [ADMIN, MEMBER, OUTSIDE])
        await radio(page, item, "Hidden").check();
    await review(page);
    await expect(listed(dialog(page))).toHaveCount(0);
    await expect(line(dialog(page), NO_ADDRESS_SHOWN)).toBeVisible();
    await expect(
        line(dialog(page), "Hidden (Bcc): 3 recipients"),
    ).toBeVisible();
    await cancelButton(page).click();
    await radio(page, ADMIN, "To").check();
    await review(page);
    await expect(line(dialog(page), NO_ADDRESS_SHOWN)).toHaveCount(0);
});

test("the review, the outside mark, a dropped choice, a review without To or Copy and every outcome read in Turkish", async ({
    page,
}) => {
    await page.clock.install();
    const { account, persons, failNext } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: [ADMIN, MEMBER, THIRD],
        includeMembers: true,
        hidden: false,
        typed: { [OUTSIDE]: "cc" },
    });
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { ...sessionIdentity(account.current), language: "tr" },
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
    const turkishRadio = (item: string, label: string) =>
        page
            .getByRole("group", {
                name: `${item} e-postayı nasıl alır`,
                exact: true,
            })
            .getByRole("radio", { name: label, exact: true });
    const saved = page
        .getByRole("heading", { name: "Kim neyi görür", exact: true })
        .locator("xpath=..");
    await expect(listed(saved)).toHaveText([
        "Kime",
        ADMIN,
        "Bilgi (Cc)",
        MEMBER,
        THIRD,
        `${OUTSIDE}Aktif bir üyenin doğrulanmış birincil adresi değil`,
    ]);
    await turkishRadio(THIRD, "Gizli").check();
    persons[2]!.active = false;
    await refetch(page);
    await expect(
        line(
            page,
            `${THIRD} için “Gizli” seçiminiz kaldırıldı: kayıtlı alıcılar veya bu çalışma alanındaki kişiler değişti.`,
        ),
    ).toBeVisible();
    for (const item of [ADMIN, MEMBER, OUTSIDE])
        await turkishRadio(item, "Gizli").check();
    const save = page.getByRole("button", {
        name: "Hatırlatıcıları kaydet",
        exact: true,
    });
    const turkishDialog = page.getByRole("dialog", {
        name: "Bu adresler her alıcıya gösterilsin mi?",
        exact: true,
    });
    await save.click();
    await expect(turkishDialog).toHaveAccessibleDescription(
        "Bu hatırlatıcıların her alıcısı aşağıdaki Kime ve Bilgi adreslerini görecek.",
    );
    await expect(
        line(
            turkishDialog,
            "Kime veya Bilgi satırında hiçbir adres görünmeyecek.",
        ),
    ).toBeVisible();
    await expect(line(turkishDialog, "Gizli (Bcc): 3 alıcı")).toBeVisible();
    await turkishDialog
        .getByRole("button", { name: "Düzenlemeye dön", exact: true })
        .click();
    await turkishRadio(ADMIN, "Varsayılan").check();
    await save.click();
    await expect(listed(turkishDialog)).toHaveText(["Kime", ADMIN]);
    await saveElsewhere(page, { ...SAVED, times: ["10:00"] });
    await turkishDialog
        .getByRole("button", { name: "Kaydet ve adresleri göster" })
        .click();
    await expect(
        line(
            page,
            "Bu kaydetme işlemindeki değişiklikler kaydedilmedi: hatırlatıcı ayarları veya bu çalışma alanındaki kişiler değişti. Düzenlemeleriniz duruyor; kontrol edip yeniden kaydedin.",
        ),
    ).toBeFocused();
    await expect(save).toBeEnabled();
    const confirmTurkish = async (outcome: string) => {
        await save.click();
        await turkishDialog
            .getByRole("button", { name: "Kaydet ve adresleri göster" })
            .click();
        await expect(line(page, outcome)).toBeFocused();
        await expect(save).toBeEnabled();
    };
    failNext("PUT", "before", { status: 403, code: "csrf_failed" });
    await confirmTurkish(
        "İstek reddedildi ve hiçbir şey kaydedilmedi. Sayfa kayıtlı ayarları yeniden okuyor.",
    );
    await turkishRadio(ADMIN, "Bilgi").check();
    failNext("PUT");
    await confirmTurkish(
        "Kaydetme doğrulanamadı. Sayfa kayıtlı ayarları yeniden okuyor. Düzenlemeleriniz duruyor.",
    );
    failNext("PUT", "before", {
        status: 403,
        code: "workspace_admin_required",
    });
    await confirmTurkish(
        "Hiçbir şey kaydedilmedi: artık bu çalışma alanının yöneticisi değildiniz.",
    );
});

test("Use the defaults for everyone sends Default for every control shown, including a choice equal to the stored role", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: [ADMIN, { membership: 2, primary: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await radio(page, ADMIN, "Copy").check();
    await radio(page, ADMIN, "Default").check();
    await radio(page, OUTSIDE, "To").check();
    await useDefaults(page).click();
    await expect(useDefaults(page)).toBeFocused();
    for (const item of [ADMIN, MEMBER, OUTSIDE])
        await expect(radio(page, item, "Default")).toBeChecked();
    const { body } = await saveVisible(page);
    expect(body.address_roles).toEqual([
        { address: OUTSIDE, role: null, memberships: [] },
    ]);
    expect(body.member_roles).toEqual([
        { memberships: [1], address: ADMIN, role: null },
        { memberships: [2], address: MEMBER, role: null },
    ]);
});

test("the mock answers each address field of the preview and the save with the status, code and field the server gives", async ({
    page,
}) => {
    const ACCEPTED = ["o.p_s+x-y@sub.example-1.com", "ops@xn--bcher-kva.de"];
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: [
            ADMIN,
            { membership: 2, primary: ACCEPTED[0]! },
            { membership: 3, primary: ACCEPTED[1]! },
        ],
        hidden: true,
        dailyLimit: 2,
    });
    await page.goto("/app/");
    const call = caller(page);
    const local65 = `${"o".repeat(65)}@example.com`;
    const long255 = `${"o".repeat(64)}@${"d".repeat(63)}.${"d".repeat(63)}.${"d".repeat(58)}.com`;
    expect(long255).toHaveLength(255);
    const childRefused = [
        ".ops@example.com",
        "ops.@example.com",
        "o..ps@example.com",
        "ops@-example.com",
        "ops@example.123",
        long255,
    ];
    const plainRefused = [
        "o!ps@example.com",
        '"ops"@example.com',
        "ops@bücher.de",
        local65,
    ];
    for (const item of ACCEPTED)
        expect([item, plain(item)]).toEqual([item, true]);
    for (const item of [...childRefused, ...plainRefused])
        expect([item, plain(item)]).toEqual([item, false]);
    const hidden = { ...SAVED, hide_recipients: true };
    const fields = {
        recipients: (item: string) => ({ recipients: [item] }),
        address_roles: (item: string, accepted: boolean) => ({
            recipients: [accepted ? item : OUTSIDE],
            address_roles: [
                {
                    address: item,
                    role: "cc",
                    memberships: accepted ? [ACCEPTED.indexOf(item) + 2] : [],
                },
            ],
        }),
        member_roles: (item: string, accepted: boolean) => ({
            member_roles: [
                {
                    memberships: [accepted ? ACCEPTED.indexOf(item) + 2 : 2],
                    address: item,
                    role: "cc",
                },
            ],
        }),
    };
    for (const path of ["preview/", ""]) {
        const method = path ? "POST" : "PUT";
        for (const [field, body] of Object.entries(fields)) {
            for (const item of ACCEPTED) {
                const revision = (await call("GET")).json.revision;
                const answer = await call(
                    method,
                    {
                        ...hidden,
                        ...body(item, true),
                        expected_revision: revision,
                    },
                    path,
                );
                expect([method, field, item, answer.status]).toEqual([
                    method,
                    field,
                    item,
                    200,
                ]);
            }
            const revision = (await call("GET")).json.revision;
            for (const item of [...childRefused, ...plainRefused]) {
                const answer = await call(
                    method,
                    {
                        ...hidden,
                        ...body(item, false),
                        expected_revision: revision,
                    },
                    path,
                );
                const expected =
                    field !== "recipients"
                        ? `${field}.0.address`
                        : childRefused.includes(item)
                          ? "recipients.0"
                          : "recipients";
                expect([
                    method,
                    item,
                    answer.status,
                    answer.json.code,
                    Object.keys(answer.json.fields ?? {}),
                ]).toEqual([method, item, 400, "invalid_request", [expected]]);
            }
        }
        const revision = (await call("GET")).json.revision;
        const twice = await call(
            method,
            {
                ...hidden,
                recipients: ["ops@example.com", "OPS@Example.COM"],
                expected_revision: revision,
            },
            path,
        );
        expect([twice.status, twice.json.code]).toEqual([
            400,
            "duplicate_recipients",
        ]);
        expect(Object.keys(twice.json.fields)).toEqual(["recipients"]);
        const times = await call(
            method,
            {
                ...hidden,
                times: ["07:00", "08:00", "09:00"],
                expected_revision: revision,
            },
            path,
        );
        expect([times.status, times.json.code]).toEqual([
            400,
            "too_many_reminder_times",
        ]);
        expect(Object.keys(times.json.fields)).toEqual(["times"]);
    }
});

test("the mock refuses a preview and a save by a member who is not the administrator before it reads the body", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        hidden: true,
        viewer: (method) => method !== "GET",
    });
    await page.goto("/app/");
    const call = caller(page);
    const before = (await call("GET")).json;
    for (const path of ["preview/", ""])
        for (const body of [
            { ...SAVED, hide_recipients: true },
            { ...SAVED, recipients: [".ops@example.com"] },
        ]) {
            const answer = await call(path ? "POST" : "PUT", body, path);
            expect([answer.status, answer.json]).toEqual([
                403,
                {
                    code: "workspace_admin_required",
                    message: "Workspace administrator required.",
                },
            ]);
        }
    expect((await call("GET")).json).toEqual(before);
});

test("a plant without a schedule opens unticked with Default for every row and the language of the request", async ({
    page,
}) => {
    await page.setExtraHTTPHeaders({ "Accept-Language": "tr" });
    await reminderApi(page, { unsaved: true, members: TEAM });
    await openReminders(page);
    await expect(hideBox(page)).not.toBeChecked();
    await expect(
        page.getByLabel("Include workspace members", { exact: true }),
    ).not.toBeChecked();
    await expect(address(page, 1)).toHaveValue("");
    await expect(
        page.getByLabel("Email language", { exact: true }),
    ).toHaveValue("tr");
    await page.getByLabel("Include workspace members", { exact: true }).check();
    for (const item of TEAM)
        await expect(radio(page, item, "Default")).toBeChecked();
    await expect(
        page.getByRole("listitem").filter({ hasText: ADMIN }),
    ).toContainText("Workspace administrator");
    await expect(
        line(page, "New members are added with the default role."),
    ).toBeVisible();
    await expect(
        line(savedView(page), "Nobody receives this email yet."),
    ).toBeVisible();
    await page.getByText("Recent deliveries", { exact: true }).click();
    await expect(
        page.getByText("No deliveries yet", { exact: true }),
    ).toBeVisible();
});

test("a member whose address is typed is chosen once on the typed row and its choice is sent in both lists", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [MEMBER],
        members: [ADMIN, { membership: 2, primary: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    const memberRow = page.getByRole("listitem").filter({ hasText: MEMBER });
    await expect(memberRow).toContainText("Set under Other addresses");
    await expect(memberRow.getByRole("radio")).toHaveCount(0);
    await expect(group(page, MEMBER)).toHaveCount(1);
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await radio(page, MEMBER, "Copy").check();
    const { body } = await saveVisible(page);
    expect(body).toMatchObject({
        address_roles: [{ address: MEMBER, role: "cc", memberships: [2] }],
        member_roles: [{ memberships: [2], address: MEMBER, role: "cc" }],
    });
    await expect(radio(page, MEMBER, "Copy")).toBeChecked();
    await expect(listed(savedView(page))).toHaveText([
        "To",
        ADMIN,
        "Copy (Cc)",
        MEMBER,
    ]);
    await radio(page, MEMBER, "Default").check();
    const cleared = await saveVisible(page);
    expect(cleared.body).toMatchObject({
        address_roles: [{ address: MEMBER, role: null, memberships: [2] }],
        member_roles: [{ memberships: [2], address: MEMBER, role: null }],
    });
});

test("a saved Copy with nobody in To shows the warning in the saved view and the review", async ({
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
    await radio(page, MEMBER, "Copy").check();
    await review(page);
    await expect(listed(dialog(page))).toHaveText(["Copy (Cc)", ADMIN, MEMBER]);
    await expect(
        line(dialog(page), "Nobody is in To: the email's To line is empty."),
    ).toBeVisible();
    await confirmButton(page).click();
    await expect(saveButton(page)).toBeEnabled();
    await expect(
        line(savedView(page), "Nobody is in To: the email's To line is empty."),
    ).toBeVisible();
    await expect(radio(page, ADMIN, "Copy")).toBeChecked();
});

test("a schedule hidden by a departure says so in the saved view, keeps the choices and is reviewed on its next visible save", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [],
        members: [ADMIN, { membership: 2, primary: MEMBER, choice: "bcc" }],
        includeMembers: true,
        hiddenByDeparture: true,
    });
    await openReminders(page);
    await expect(line(savedView(page), DEPARTURE)).toBeVisible();
    await expect(hideBox(page)).toBeChecked();
    await expect(page.getByRole("radio")).toHaveCount(0);
    await hideBox(page).uncheck();
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await expect(line(savedView(page), DEPARTURE)).toBeVisible();
    const { body } = await saveVisible(page);
    expect(body).toMatchObject({ hide_recipients: false });
    expect(body).not.toHaveProperty("member_roles");
    await expect(line(savedView(page), DEPARTURE)).toHaveCount(0);
    await expect(listed(savedView(page))).toHaveText(["To", ADMIN]);
});

test("the history shows the roles recorded for sent and uncertain deliveries to the administrator", async ({
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
        await expect(line(page, COUNT_ONLY(2))).toBeVisible();
        await expect(line(page, mode)).toBeVisible();
        await expect(page.getByRole("radio")).toHaveCount(0);
        await expect(heading(page)).toHaveCount(0);
        await page.getByText("Recent deliveries", { exact: true }).click();
        await expect(
            page.getByRole("cell", { name: "2 recipients", exact: true }),
        ).toBeVisible();
        await expect(page.getByText(MEMBER)).toHaveCount(0);
        await expect(page.getByText("Copy", { exact: true })).toHaveCount(0);
    });
}

test("keyboard users reach each role group at its checked radio and change it with the arrows", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: [ADMIN, { membership: 2, primary: MEMBER, choice: "cc" }],
        includeMembers: true,
        hidden: false,
    });
    await openReminders(page);
    await page.getByLabel("Include workspace members", { exact: true }).focus();
    await page.keyboard.press("Tab");
    await expect(radio(page, ADMIN, "Default")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(radio(page, MEMBER, "Copy")).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(radio(page, MEMBER, "Hidden")).toBeFocused();
    await expect(radio(page, MEMBER, "Hidden")).toBeChecked();
    await page.keyboard.press("Tab");
    await expect(address(page, 1)).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(radio(page, OUTSIDE, "Default")).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(radio(page, OUTSIDE, "Hidden")).toBeChecked();
    const { body } = await saveVisible(page);
    expect(body).toMatchObject({
        address_roles: [{ address: OUTSIDE, role: "bcc", memberships: [] }],
        member_roles: [{ memberships: [2], address: MEMBER, role: "bcc" }],
    });
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
        typed: { [LONG]: "cc" },
    });
    await page.setViewportSize({ width: 320, height: 900 });
    await openReminders(page);
    await expect(listed(savedView(page))).toContainText([
        `${LONG}${OUTSIDE_MARK}`,
    ]);
    expect(await fits(page)).toBe(true);
    const outside = await page
        .getByRole("region", { name: "Email reminders" })
        .locator("input:visible, button:visible, p:visible, dd:visible")
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

test("the mock binds a visible save to the preview's digest, writes nothing for a preview or a refusal and ignores the digest of a hidden save", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: true,
    });
    await page.goto("/app/");
    const call = caller(page);
    const before = (await call("GET")).json;
    const visible = { ...SAVED, hide_recipients: false };
    const preview = await call("POST", visible, "preview/");
    expect(preview.status).toBe(200);
    expect(typeof preview.json.disclosure).toBe("string");
    expect((await call("GET")).json).toEqual(before);
    for (const body of [visible, { ...visible, disclosure: "other" }]) {
        const answer = await call("PUT", body);
        expect([answer.status, answer.json.code]).toEqual([
            409,
            "reminder_disclosure_changed",
        ]);
        expect((await call("GET")).json).toEqual(before);
    }
    const hidden = await call("PUT", {
        ...SAVED,
        hide_recipients: true,
        times: ["09:00"],
        disclosure: "other",
    });
    expect(hidden.status).toBe(200);
    expect(hidden.json.revision).toBe(1);
    const shown = await call("PUT", {
        ...visible,
        expected_revision: 1,
        disclosure: (
            await call("POST", { ...visible, expected_revision: 1 }, "preview/")
        ).json.disclosure,
    });
    expect(shown.status).toBe(200);
    expect(shown.json.hide_recipients).toBe(false);
});

test("the mock refuses before it changes anything, keeps the revision of an unchanged save and keeps recorded history", async ({
    page,
}) => {
    let viewing = false;
    await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: true,
        viewer: () => viewing,
    });
    await page.goto("/app/");
    const call = caller(page);
    const before = (await call("GET")).json;
    const unchanged = { ...SAVED, hide_recipients: true };
    expect((await call("PUT", unchanged)).json.revision).toBe(0);
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
        const answer = await call("PUT", { ...unchanged, ...refused });
        expect([answer.status, answer.json.code]).toEqual([status, code]);
        expect(Object.keys(answer.json.fields ?? {})).toEqual(
            field ? [field] : [],
        );
    }
    viewing = true;
    const viewer = await call("PUT", {
        ...unchanged,
        include_workspace_members: false,
    });
    expect([viewer.status, viewer.json.code]).toEqual([
        403,
        "workspace_admin_required",
    ]);
    viewing = false;
    expect((await call("GET")).json).toEqual(before);
    const changed = await call("PUT", {
        ...unchanged,
        member_roles: [{ memberships: [2], address: MEMBER, role: "bcc" }],
    });
    expect(changed.json.revision).toBe(1);
    expect(changed.json.addresses.members[1].role).toBe("bcc");
    expect(changed.json.deliveries).toEqual(before.deliveries);
    const preview = await call(
        "POST",
        {
            ...unchanged,
            expected_revision: 1,
            include_workspace_members: false,
        },
        "preview/",
    );
    expect(preview.json.include_workspace_members).toBe(false);
    expect((await call("GET")).json).toEqual(changed.json);
});

test("the mock combines the choices of several members of one address and keeps the ties of a hidden typed row when its member moves", async ({
    page,
}) => {
    const ALICE = "alice@example.test";
    const MOVED = "alice.new@example.test";
    const SHARED = "Shared@example.test";
    const { persons } = await reminderApi(page, {
        recipients: [],
        members: [
            ADMIN,
            { membership: 2, primary: SHARED, choice: "to" },
            { membership: 3, primary: "shared@example.test", choice: "cc" },
            { membership: 4, primary: ALICE },
        ],
        includeMembers: true,
        hidden: false,
        limit: 10,
    });
    await page.goto("/app/");
    const call = caller(page);
    type Answer = {
        addresses: {
            members: {
                address: string;
                memberships: number[];
                role: string | null;
                placement: string;
            }[];
        };
    };
    const placed = (answer: Answer) =>
        answer.addresses.members.map((row) => [
            row.address,
            row.memberships,
            row.role,
            row.placement,
        ]);
    expect(placed((await call("GET")).json)).toEqual([
        [ADMIN, [1], null, "to"],
        [SHARED, [2, 3], "bcc", "bcc"],
        [ALICE, [4], null, "cc"],
    ]);
    const hidden = await call("PUT", {
        ...SAVED,
        hide_recipients: true,
        recipients: [ALICE],
        address_roles: [{ address: ALICE, role: "bcc", memberships: [4] }],
    });
    expect(hidden.status).toBe(200);
    persons[3]!.primary = MOVED;
    persons[3]!.verified = [MOVED];
    const preview = await call(
        "POST",
        {
            ...SAVED,
            recipients: [ALICE],
            expected_revision: hidden.json.revision,
        },
        "preview/",
    );
    expect(placed(preview.json)).toEqual([
        [ADMIN, [1], null, "to"],
        [SHARED, [2, 3], "bcc", "bcc"],
        [MOVED, [4], null, "bcc"],
    ]);
});

for (const schedule of ["hidden", "visible", "unsaved"] as const)
    test(`the mock's departure of a member from a ${schedule === "unsaved" ? "plant without a schedule changes only the person" : `${schedule} schedule removes the member's typed rows${schedule === "visible" ? ", hides every recipient" : ""} and advances the revision once`}`, async ({
        page,
    }) => {
        const ALIAS = "alias@example.test";
        const { depart, persons } = await reminderApi(page, {
            recipients: [OUTSIDE, MEMBER, ALIAS],
            members: TEAM,
            hidden: schedule === "hidden",
            unsaved: schedule === "unsaved",
        });
        await page.goto("/app/");
        const call = caller(page);
        persons[1]!.verified = [MEMBER, ALIAS];
        const before = (await call("GET")).json;
        depart(2);
        const after = (await call("GET")).json;
        if (schedule === "unsaved") {
            expect(after).toEqual({
                ...before,
                workspace_member_count: 1,
                addresses: {
                    ...before.addresses,
                    workspace_recipients: [ADMIN],
                    members: [before.addresses.members[0]],
                },
            });
            return;
        }
        expect(before.addresses.recipients).toEqual([OUTSIDE, MEMBER, ALIAS]);
        expect([
            after.addresses.recipients,
            after.revision,
            after.hide_recipients,
            after.hidden_by_departure,
        ]).toEqual([[OUTSIDE], 1, true, schedule === "visible"]);
    });

test("the mock refuses each malformed request with the status, code and member the server names", async ({
    page,
}) => {
    await reminderApi(page, {
        recipients: [OUTSIDE, "two@example.test", "three@example.test"],
        members: TEAM,
        hidden: true,
        limit: 1,
    });
    await page.goto("/app/");
    const call = caller(page);
    const hidden = { ...SAVED, hide_recipients: true };
    const withoutRecipients = Object.fromEntries(
        Object.entries(hidden).filter(([name]) => name !== "recipients"),
    );
    for (const [body, status, code, field] of [
        [{ ...hidden, times: ["25:00"] }, 400, "invalid_request", "times.0"],
        [withoutRecipients, 400, "invalid_request", "recipients"],
        [{ ...hidden, language: "de" }, 400, "invalid_request", "language"],
        [{ ...hidden, disclosure: 1 }, 400, "invalid_request", "disclosure"],
        [
            {
                ...hidden,
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
                ...hidden,
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
        ...hidden,
        include_workspace_members: false,
        recipients: [OUTSIDE, "two@example.test", "three@example.test"],
        member_roles: [{ memberships: [2, 2], address: MEMBER, role: "bcc" }],
    });
    expect(repeated.status).toBe(200);
    expect(repeated.json.addresses.members[1].role).toBe("bcc");
});
