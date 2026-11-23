import type { Locator } from "@playwright/test";
import { expect, test, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
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
const CONFLICT =
    "Nothing was saved: the reminder settings or the people in this workspace changed. Check them and save again.";
const SAVED_LOADING = "Saved. Loading the saved recipients…";
const SAVED_FAILED = "Saved, but the saved recipients could not be loaded.";
const HIDDEN_TODAY =
    "Every recipient is hidden today. You can address the administrator directly and keep members in copy.";
const HIDDEN_HELPER =
    "When ticked, everyone gets the reminder in hidden copy (Bcc) and no recipient sees another address.";
const ONCE_SAVED = "You can choose a role once this address is saved.";
const DEPARTURE =
    "Every recipient was hidden again because a member left or an account was deleted. The remaining choices are kept.";
const OUTSIDE_MARK = "Outside the workspace";
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
    savedView(page).getByRole("button", { name: "Try again", exact: true });
const typedFields = (page: Page) => page.locator('input[name="recipients"]');
const hideBox = (page: Page) =>
    page.getByLabel("Hide recipients from each other", { exact: true });

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

const response = (
    page: Page,
    matches: (method: string, url: string) => boolean,
    status?: number,
) =>
    page.waitForResponse(
        (answer) =>
            matches(answer.request().method(), answer.url()) &&
            (status === undefined || answer.status() === status),
    );

async function review(page: Page) {
    const request = page.waitForRequest((request) =>
        isPreview(request.method(), request.url()),
    );
    const answered = response(page, isPreview, 200);
    await saveButton(page).click();
    const previewed = (await request).postDataJSON();
    const answer = await (await answered).json();
    await expect(dialog(page)).toBeVisible();
    return { previewed, answer };
}

async function saveVisible(page: Page) {
    const { previewed, answer } = await review(page);
    const request = page.waitForRequest((request) =>
        isWrite(request.method(), request.url()),
    );
    await confirmButton(page).click();
    const body = (await request).postDataJSON();
    await expect(saveButton(page)).toBeEnabled();
    return { previewed, answer, body };
}

async function refetch(page: Page) {
    const read = response(page, isRead);
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
    const preview = await call("POST", body, "preview/");
    expect(preview.status).toBe(200);
    const stored = await call("PUT", {
        ...body,
        disclosure: preview.json.disclosure,
    });
    expect(stored.status).toBe(200);
    return stored.json;
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
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([tenantRow({ id: 1, name: "Sunfield Energy", role })]),
        }),
    );
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
        const late = response(page, isRead);
        const fresh = response(page, isRead);
        turkish.release();
        english.release();
        await late;
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
    const tenants = page.waitForResponse((answer) =>
        answer.url().endsWith("/api/tenants/"),
    );
    await refetch(page);
    await tenants;
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
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([tenantRow({ id: 1, name: "Sunfield Energy", role })]),
        }),
    );
    const writes = sent(page, isWrite);
    await openReminders(page);
    await radio(page, OUTSIDE, "Copy").check();
    await time(page, 1).fill("09:00");
    await review(page);
    role = "viewer";
    const tenants = page.waitForResponse((answer) =>
        answer.url().endsWith("/api/tenants/"),
    );
    await page.clock.fastForward("00:10");
    await page.evaluate(() =>
        window.dispatchEvent(new Event("visibilitychange")),
    );
    await tenants;
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
        const failed = response(page, isRead, 503);
        await page.getByLabel("Language", { exact: true }).selectOption("tr");
        await held.committed;
        const turkish = page
            .getByRole("heading", { name: "Kim neyi görür", exact: true })
            .locator("xpath=..");
        await expect(listed(turkish)).toHaveText([
            "Kime",
            ADMIN,
            "Bilgi (Cc)",
            MEMBER,
            `${OUTSIDE}Çalışma alanı dışında`,
        ]);
        held.release();
        await failed;
        await expect(
            page.getByRole("alert").filter({ hasText: /./ }),
        ).not.toHaveCount(0);
        await expect(listed(turkish)).toHaveText([
            "Kime",
            ADMIN,
            "Bilgi (Cc)",
            MEMBER,
            `${OUTSIDE}Çalışma alanı dışında`,
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
    await expect(
        page.getByText("Saved successfully.", { exact: true }),
    ).toHaveCount(0);
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
    const refused = response(page, isWrite, 409);
    const read = response(page, isRead, 200);
    await confirmButton(page).click();
    expect((await (await refused).json()).code).toBe(
        "reminder_disclosure_changed",
    );
    await expect(dialog(page)).toHaveCount(0);
    const alert = page.locator(".notice[role=alert]");
    await expect(alert).toContainText(CONFLICT);
    await expect(alert).toBeFocused();
    await read;
    await expect(time(page, 1)).toHaveValue("09:00");
    await expect(listed(savedView(page))).toHaveText([
        "To",
        ADMIN,
        "Copy (Cc)",
        MEMBER,
        OUTSIDE,
    ]);
    await review(page);
    await expect(listed(dialog(page))).toHaveText([
        "To",
        ADMIN,
        "Copy (Cc)",
        MEMBER,
        OUTSIDE,
    ]);
    const write = response(page, isWrite, 200);
    await confirmButton(page).click();
    await write;
    await expect(saveButton(page)).toBeEnabled();
    await expect(time(page, 1)).toHaveValue("09:00");
});

test("a save whose following read fails keeps the fields read-only with Retry, and Retry opens the read for editing", async ({
    page,
}) => {
    const { failNext } = await reminderApi(page);
    await openReminders(page);
    await page.getByRole("button", { name: "Add time", exact: true }).click();
    await time(page, 2).fill("07:00");
    failNext("GET");
    const failed = response(page, isRead, 503);
    await saveButton(page).click();
    await failed;
    await expect(line(page, SAVED_FAILED)).toBeVisible();
    await expect(line(page, SAVED_FAILED)).toBeFocused();
    await expect(retryButton(page)).toBeVisible();
    await expect(saveButton(page)).toHaveCount(0);
    await expect(time(page, 1)).toHaveValue("08:00");
    await expect(time(page, 2)).toHaveValue("07:00");
    for (const field of [time(page, 1), address(page, 1)])
        await expect(field).toBeDisabled();
    await expect(
        page.getByRole("button", { name: "Add time", exact: true }),
    ).toBeHidden();
    const read = response(page, isRead, 200);
    await retryButton(page).click();
    await read;
    await expect(time(page, 1)).toHaveValue("07:00");
    await expect(time(page, 2)).toHaveValue("08:00");
    await expect(time(page, 1)).toBeEnabled();
    await expect(saveButton(page)).toBeEnabled();
    await expect(heading(page)).toBeFocused();
    await expect(line(page, SAVED_FAILED)).toHaveCount(0);
});

test("a held unchanged save ends on the read Retry loads, with no line about a change elsewhere", async ({
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
    await openReminders(page);
    await review(page);
    const write = answerLater("PUT");
    try {
        await confirmButton(page).click();
        await write.committed;
        persons[2]!.verified = [OUTSIDE];
        await refetch(page);
        await expect(listed(savedView(page))).toContainText([OUTSIDE]);
        failNext("GET");
        const failed = response(page, isRead, 503);
        write.release();
        await failed;
        await expect(line(page, SAVED_FAILED)).toBeVisible();
    } finally {
        write.release();
    }
    await expect(page.getByText(/changed elsewhere/)).toHaveCount(0);
    await retryButton(page).click();
    await expect(listed(savedView(page))).toHaveText([
        "To",
        ADMIN,
        "Copy (Cc)",
        MEMBER,
        OUTSIDE,
    ]);
    await expect(page.getByText(/changed elsewhere/)).toHaveCount(0);
});

test("a read that removes the member whose role group holds focus moves focus to the members heading and keeps an edited time", async ({
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
    await time(page, 1).fill("09:00");
    await radio(page, MEMBER, "Default").focus();
    persons[1]!.active = false;
    await refetch(page);
    await expect(group(page, MEMBER)).toHaveCount(0);
    await expect(
        page.getByRole("heading", { name: "Workspace members (2)" }),
    ).toBeFocused();
    await expect(time(page, 1)).toHaveValue("09:00");
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

test("a hidden result is saved with one PUT, without a preview or a review", async ({
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
    const read = answerLater("GET");
    let body: Record<string, unknown>;
    try {
        const request = page.waitForRequest((request) =>
            isWrite(request.method(), request.url()),
        );
        await saveButton(page).click();
        body = (await request).postDataJSON();
        await read.committed;
        await expect(line(page, SAVED_LOADING)).toBeFocused();
        await expect(saveButton(page)).toHaveCount(0);
        await expect(listed(savedView(page))).toHaveCount(0);
        const answered = response(page, isRead, 200);
        read.release();
        await answered;
    } finally {
        read.release();
    }
    await expect(saveButton(page)).toBeEnabled();
    await expect(heading(page)).toBeFocused();
    expect(body).toMatchObject({ hide_recipients: true });
    expect(body).not.toHaveProperty("disclosure");
    expect(previews).toEqual([]);
    expect(writes).toHaveLength(1);
    await expect(dialog(page)).toHaveCount(0);
    await expect(line(page, HIDDEN_TODAY)).toBeVisible();
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

test("a choice for a member who leaves is removed with a line naming the address and the rest of the draft stays", async ({
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
    await expect(
        line(
            page,
            `Your choice for ${MEMBER} was removed: it is no longer on the list.`,
        ),
    ).toBeVisible();
    await expect(radio(page, THIRD, "To")).toBeChecked();
    await expect(time(page, 1)).toHaveValue("09:00");
    const { body } = await saveVisible(page);
    expect(body.member_roles).toEqual([
        { memberships: [3], address: THIRD, role: "to" },
    ]);
    expect(body.times).toEqual(["09:00"]);
});

test("the review dialog takes focus, keeps Tab off the page behind it, returns to Save on Escape and passes axe at 390px without sideways scroll at 320px", async ({
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

test("the review, the conflict and the lines after a save read in Turkish", async ({
    page,
}) => {
    const { account, answerLater, failNext } = await reminderApi(page, {
        recipients: [OUTSIDE],
        members: TEAM,
        includeMembers: true,
        hidden: false,
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
    await expect(listed(turkishDialog)).toHaveText([
        "Kime",
        ADMIN,
        "Bilgi (Cc)",
        MEMBER,
    ]);
    await expect(line(turkishDialog, "Gizli (Bcc): 1 alıcı")).toBeVisible();
    await expect(
        turkishDialog.getByRole("button", { name: "Düzenlemeye dön" }),
    ).toBeVisible();
    await saveElsewhere(page, { ...SAVED, times: ["10:00"] });
    await turkishDialog
        .getByRole("button", { name: "Kaydet ve adresleri göster" })
        .click();
    const alert = page.locator(".notice[role=alert]");
    await expect(alert).toContainText(
        "Hiçbir şey kaydedilmedi: hatırlatıcı ayarları veya bu çalışma alanındaki kişiler değişti. Kontrol edip yeniden kaydedin.",
    );
    await expect(alert).toBeFocused();
    await expect(save).toBeEnabled();
    await page
        .getByLabel("Alıcıları birbirinden gizle", { exact: true })
        .check();
    const held = answerLater("GET");
    try {
        await save.click();
        await held.committed;
        await expect(
            line(page, "Kaydedildi. Kaydedilen alıcılar yükleniyor…"),
        ).toBeFocused();
        failNext("GET");
        held.release();
    } finally {
        held.release();
    }
    await expect(
        line(page, "Kaydedildi. Kaydedilen alıcılar yükleniyor…"),
    ).toHaveCount(0);
    await expect(save).toBeEnabled();
    await page.getByLabel("Gönderim saati 1", { exact: true }).fill("11:00");
    failNext("GET");
    await save.click();
    await expect(
        line(page, "Kaydedildi, ancak kaydedilen alıcılar yüklenemedi."),
    ).toBeFocused();
    await expect(
        page
            .getByRole("heading", { name: "Kim neyi görür", exact: true })
            .locator("xpath=..")
            .getByRole("button", { name: "Tekrar dene" }),
    ).toBeVisible();
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

test("a choice back to the stored role and Use the defaults for everyone send only what differs", async ({
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
    const useDefaults = page.getByRole("button", {
        name: "Use the defaults for everyone",
        exact: true,
    });
    await useDefaults.click();
    await expect(useDefaults).toBeFocused();
    for (const item of [ADMIN, MEMBER, OUTSIDE])
        await expect(radio(page, item, "Default")).toBeChecked();
    const { body } = await saveVisible(page);
    expect(body).not.toHaveProperty("address_roles");
    expect(body.member_roles).toEqual([
        { memberships: [2], address: MEMBER, role: null },
    ]);
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
