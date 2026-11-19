import { expect, test } from "./support/test";
import type { Page } from "@playwright/test";
import { mockApi, paged, region, user, tenantRow } from "./support/workspace";
import { errorBody } from "./support/errors";

test("shared reminder recipients and email language survive administrator transfer", async ({
    page,
}, testInfo) => {
    await reminderApi(page, [user.email], 5, true);
    await page.setViewportSize({ width: 320, height: 900 });
    let currentUser = user;
    let role = "admin";
    let saves = 0;
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: currentUser,
                language: "en",
                csrf_token: "signed-token",
            },
        }),
    );
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([{ id: 1, name: "Sunfield Energy", role }]),
        }),
    );
    await page.route("**/reminders/", (route) => {
        if (route.request().method() === "PUT") saves++;
        return route.fallback();
    });
    await openReminders(page);
    await page.getByLabel("Email language", { exact: true }).selectOption("tr");
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON()).toMatchObject({
        enabled: true,
        recipients: [user.email],
        times: ["08:00"],
        language: "tr",
    });
    await expect(
        page.getByLabel("Email language", { exact: true }),
    ).toHaveValue("tr");
    currentUser = { ...user, id: 2, email: "next-admin@example.test" };
    await openReminders(page);
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue(user.email);
    await expect(
        page.getByLabel("Email language", { exact: true }),
    ).toHaveValue("tr");
    await expect(
        page.getByLabel("Delivery time 1", { exact: true }),
    ).toHaveValue("08:00");
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    await page.screenshot({
        path: testInfo.outputPath("shared-reminders-320.png"),
        fullPage: true,
    });
    role = "viewer";
    await openReminders(page);
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue(user.email);
    await expect(
        page.getByLabel("Email language", { exact: true }),
    ).toHaveValue("tr");
    await expect(
        page.getByLabel("Email language", { exact: true }),
    ).toBeDisabled();
    await expect(
        page.getByRole("button", { name: "Save reminders" }),
    ).toHaveCount(0);
    expect(saves).toBe(1);
});

test("a failed reload after saving keeps the saved reminders and the history until a retry loads them", async ({
    page,
}) => {
    await reminderApi(page, [user.email], 5, true);
    let unavailable = false;
    await page.route("**/reminders/", (route) => {
        if (route.request().method() === "PUT") unavailable = true;
        else if (unavailable)
            return route.fulfill({
                status: 503,
                json: errorBody("busy", "Reminder service is busy."),
            });
        return route.fallback();
    });
    const read = (status: number) =>
        page.waitForResponse(
            (response) =>
                response.request().method() === "GET" &&
                response.url().endsWith("/reminders/") &&
                response.status() === status,
        );
    await openReminders(page);
    await page.getByText("Recent deliveries", { exact: true }).click();
    const history = page.getByRole("table", {
        name: "Recent forecast email deliveries",
    });
    await expect(history).toBeVisible();
    await page.getByRole("button", { name: /Add time/ }).click();
    await page.getByLabel("Delivery time 2", { exact: true }).fill("07:00");
    const failed = read(503);
    await page.getByRole("button", { name: "Save reminders" }).click();
    await failed;
    const notice = page
        .getByRole("alert")
        .filter({ hasText: "Could not refresh" });
    await expect(notice).toBeVisible();
    await expect(
        page.getByLabel("Delivery time 1", { exact: true }),
    ).toHaveValue("08:00");
    await expect(
        page.getByLabel("Delivery time 2", { exact: true }),
    ).toHaveValue("07:00");
    await expect(history).toBeVisible();

    unavailable = false;
    const reloaded = read(200);
    await notice
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await reloaded;
    await expect(notice).toHaveCount(0);
    await expect(
        page.getByLabel("Delivery time 1", { exact: true }),
    ).toHaveValue("07:00");
    await expect(
        page.getByLabel("Delivery time 2", { exact: true }),
    ).toHaveValue("08:00");
    await expect(history).toBeVisible();
});

async function openReminders(page: Page) {
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
        .getByRole("button", { name: "Email reminders", exact: true })
        .click();
}

async function reminderApi(
    page: Page,
    recipients: string[] = [user.email],
    limit = 5,
    enabled = false,
    delivery: Record<string, unknown> = {},
    dailyLimit = 10,
    members: string[] = [user.email],
    includeMembers = false,
) {
    await mockApi(page);
    let saved = {
        enabled,
        recipients,
        times: ["08:00"],
        language: "en",
        include_workspace_members: includeMembers,
    };
    await page.route("**/reminders/", async (route) => {
        if (route.request().method() === "PUT") {
            const payload = route.request().postDataJSON();
            expect(["en", "tr"]).toContain(payload.language);
            saved = {
                ...payload,
                recipients:
                    payload.recipients.length ||
                    payload.include_workspace_members
                        ? payload.recipients
                        : [user.email],
            };
        }
        const effective = [
            ...new Map(
                [
                    ...saved.recipients,
                    ...(saved.include_workspace_members ? members : []),
                ].map((email) => [email.toLowerCase(), email]),
            ).values(),
        ];
        await route.fulfill({
            json: {
                ...saved,
                workspace_recipients: members,
                effective_recipients: effective,
                recipient_limit: limit,
                recipient_count: effective.length,
                recipient_slots_remaining: Math.max(
                    0,
                    limit - effective.length,
                ),
                recipient_limit_exceeded: effective.length > limit,
                timezone: region.timezone,
                quota: {
                    daily_reminder_limit: dailyLimit,
                    queries_per_email: 0,
                },
                deliveries: [
                    {
                        id: 1,
                        recipients: effective,
                        scheduled_for: "2026-01-01T08:00:00Z",
                        collected_at: null,
                        source_age_seconds: null,
                        stale: false,
                        sent_at: "2026-01-01T08:00:00Z",
                        status: "sent",
                        late: false,
                        last_error: "",
                        forecast_status: null,
                        forecast_complete: null,
                        ...delivery,
                    },
                ],
            },
        });
    });
}

test("workspace toggle counts unique addresses live and keeps manual recipients separate at 320px", async ({
    page,
}, testInfo) => {
    await page.setViewportSize({ width: 320, height: 900 });
    await reminderApi(page, ["MEMBER@example.test"], 5, true, {}, 10, [
        "member@example.test",
        "second@example.test",
    ]);
    await openReminders(page);
    const toggle = page.getByLabel("Include workspace members", {
        exact: true,
    });
    await expect(page.getByText("1 / 5", { exact: true })).toBeVisible();
    await toggle.check();
    await expect(page.getByText("2 / 5", { exact: true })).toBeVisible();
    await page.getByText("Workspace members (2)", { exact: true }).click();
    await expect(
        page.getByRole("listitem").filter({ hasText: "second@example.test" }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Add address", exact: true })
        .click();
    await page
        .getByLabel("Email address 2", { exact: true })
        .fill("extra@example.test");
    await expect(page.getByText("3 / 5", { exact: true })).toBeVisible();
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON()).toMatchObject({
        include_workspace_members: true,
        recipients: ["MEMBER@example.test", "extra@example.test"],
    });
    await expect(toggle).toBeChecked();
    await expect(page.getByText("3 / 5", { exact: true })).toBeVisible();
    await toggle.uncheck();
    await expect(page.getByText("2 / 5", { exact: true })).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    await page.screenshot({
        path: testInfo.outputPath("workspace-recipients-320.png"),
        fullPage: true,
    });
});

test("automatic-only recipients persist without capturing the saving administrator", async ({
    page,
}) => {
    await reminderApi(page, [], 5, true, {}, 10, ["member@example.test"], true);
    await openReminders(page);
    const address = page.getByLabel("Email address 1", { exact: true });
    await expect(address).toHaveValue("");
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON()).toMatchObject({
        include_workspace_members: true,
        recipients: [],
    });
    await expect(address).toHaveValue("");
    await expect(page.getByText("1 / 5", { exact: true })).toBeVisible();
    await page.getByText("Recent deliveries", { exact: true }).click();
    await expect(
        page.getByRole("cell", { name: "member@example.test", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("cell", { name: user.email, exact: true }),
    ).toHaveCount(0);
});

test("workspace recipients stay readable while viewer controls are disabled", async ({
    page,
}) => {
    await page.setViewportSize({ width: 320, height: 900 });
    await reminderApi(page, [], 5, true, {}, 10, ["member@example.test"], true);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "viewer" }),
            ]),
        }),
    );
    await openReminders(page);
    await expect(
        page.getByLabel("Include workspace members", { exact: true }),
    ).toBeDisabled();
    await page.getByText("Workspace members (1)", { exact: true }).click();
    await expect(
        page.getByRole("listitem").filter({ hasText: "member@example.test" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Save reminders", exact: true }),
    ).toHaveCount(0);
});

test("an oversized automatic group preserves every member and can be turned off", async ({
    page,
}) => {
    const members = Array.from(
        { length: 6 },
        (_, index) => `member${index}@example.test`,
    );
    await reminderApi(
        page,
        ["extra@example.test"],
        5,
        true,
        {},
        10,
        members,
        true,
    );
    await openReminders(page);
    await expect(page.getByText("7 / 5", { exact: true })).toBeVisible();
    await expect(
        page.getByText("Sending is paused", { exact: false }),
    ).toBeVisible();
    await page.getByText("Workspace members (6)", { exact: true }).click();
    for (const member of members) {
        await expect(
            page.getByRole("listitem").filter({ hasText: member }),
        ).toBeVisible();
    }
    await page
        .getByLabel("Include workspace members", { exact: true })
        .uncheck();
    await expect(page.getByText("1 / 5", { exact: true })).toBeVisible();
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON()).toMatchObject({
        include_workspace_members: false,
        recipients: ["extra@example.test"],
    });
});

test("turning on a new oversized automatic group shows the limit before saving", async ({
    page,
}) => {
    const members = Array.from(
        { length: 6 },
        (_, index) => `member${index}@example.test`,
    );
    await reminderApi(page, [], 5, true, {}, 10, members);
    let saves = 0;
    await page.route("**/reminders/", (route) => {
        if (route.request().method() === "PUT") saves++;
        return route.fallback();
    });
    await openReminders(page);
    await page.getByLabel("Include workspace members", { exact: true }).check();
    await expect(page.getByText("6 / 5", { exact: true })).toBeVisible();
    await expect(
        page.getByText("Use at most 5 email addresses.", { exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(page.locator(".notice[role=alert]")).toContainText(
        "Use at most 5 email addresses.",
    );
    expect(saves).toBe(0);
});

test("Turkish automatic recipient labels preserve readable characters", async ({
    page,
}) => {
    await reminderApi(page);
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
        page.getByLabel("Çalışma alanı üyelerini ekle", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("Diğer adresler", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText(
            "Her gönderim saatinde tek e-posta. Alıcılar birbirini göremez (BCC).",
            { exact: true },
        ),
    ).toBeVisible();
});

for (const [field, add, remove] of [
    ["Email address", "Add address", "Remove email address"],
    ["Delivery time", "Add time", "Remove delivery time"],
]) {
    test(`${field} add and remove keep keyboard focus in the edited fields`, async ({
        page,
    }) => {
        await reminderApi(page);
        await openReminders(page);
        await expect(
            page.getByRole("button", { name: "Email reminders", exact: true }),
        ).toBeFocused();
        const addButton = page.getByRole("button", { name: add, exact: true });
        await addButton.focus();
        await page.keyboard.press("Enter");
        await expect(
            page.getByLabel(`${field} 2`, { exact: true }),
        ).toBeFocused();
        await addButton.focus();
        await page.keyboard.press("Enter");
        await expect(
            page.getByLabel(`${field} 3`, { exact: true }),
        ).toBeFocused();
        await page
            .getByRole("button", { name: `${remove} 2`, exact: true })
            .focus();
        await page.keyboard.press("Enter");
        await expect(
            page.getByLabel(`${field} 2`, { exact: true }),
        ).toBeFocused();
        await page
            .getByRole("button", { name: `${remove} 2`, exact: true })
            .focus();
        await page.keyboard.press("Enter");
        await expect(
            page.getByLabel(`${field} 1`, { exact: true }),
        ).toBeFocused();
        await expect(
            page.getByRole("button", { name: `${remove} 1`, exact: true }),
        ).toHaveCount(0);
    });
}

for (const [kind, label] of [
    ["saved_complete", "Saved forecast"],
    ["saved_partial", "Partial forecast"],
    ["unavailable", "No forecast available"],
] as const) {
    test(`delivered ${kind} is distinguished from mail acceptance`, async ({
        page,
    }) => {
        await reminderApi(page, [], 5, true, { forecast_status: kind });
        await openReminders(page);
        await page.getByText("Recent deliveries", { exact: true }).click();
        await expect(
            page.getByText("Accepted by mail service", { exact: true }),
        ).toBeVisible();
        await expect(page.getByText(label, { exact: true })).toBeVisible();
    });
}

test("uncertain acceptance explains the duplicate risk without exposing internal codes", async ({
    page,
}) => {
    await reminderApi(page, [], 5, true, {
        status: "uncertain",
        last_error: "mail_outcome_uncertain",
        sent_at: null,
    });
    await openReminders(page);
    await page.getByText("Recent deliveries", { exact: true }).click();
    await expect(
        page.getByText("Acceptance unconfirmed", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText(
            "Check the mail service before resending to avoid a duplicate.",
            { exact: true },
        ),
    ).toBeVisible();
    await expect(
        page.getByText("mail_outcome_uncertain", { exact: true }),
    ).toHaveCount(0);
});

for (const [code, detail] of [
    [
        "mail_rate_limited",
        "The mail service rate limit was reached. Retries are bounded by the sending window.",
    ],
    [
        "mail_preparation_failed",
        "Transmission permission could not be confirmed. Retries are bounded by the sending window.",
    ],
] as const) {
    test(`${code} explains the retry reason without exposing internal codes`, async ({
        page,
    }) => {
        await reminderApi(page, [], 5, true, {
            status: "retry",
            last_error: code,
            sent_at: null,
        });
        await openReminders(page);
        await page.getByText("Recent deliveries", { exact: true }).click();
        await expect(page.getByText(detail, { exact: true })).toBeVisible();
        await expect(page.getByText(code, { exact: true })).toHaveCount(0);
    });
}

test("five recipients can be added, removed, saved and reloaded at 320px", async ({
    page,
}) => {
    await page.setViewportSize({ width: 320, height: 900 });
    await reminderApi(page);
    await openReminders(page);
    for (let index = 1; index <= 5; index++) {
        if (index > 1)
            await page.getByRole("button", { name: "Add address" }).click();
        await page
            .getByLabel(`Email address ${index}`, { exact: true })
            .fill(`person${index}@example.test`);
    }
    await expect(page.getByText("5 / 5", { exact: true })).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Add address" }),
    ).toBeDisabled();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    await page
        .getByRole("button", { name: "Remove email address 2", exact: true })
        .click();
    await expect(
        page.getByLabel("Email address 2", { exact: true }),
    ).toHaveValue("person3@example.test");
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON().recipients).toEqual([
        "person1@example.test",
        "person3@example.test",
        "person4@example.test",
        "person5@example.test",
    ]);
    await openReminders(page);
    await expect(
        page.getByLabel("Email address 4", { exact: true }),
    ).toHaveValue("person5@example.test");
    await page.getByText("Recent deliveries", { exact: true }).click();
    await expect(
        page
            .getByRole("cell")
            .getByText("person1@example.test", { exact: true }),
    ).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
});

test("dynamic recipient limit and server error retain entered values", async ({
    page,
}) => {
    await reminderApi(page, [], 2);
    await openReminders(page);
    await page
        .getByLabel("Email address 1", { exact: true })
        .fill("first@example.test");
    await page.getByRole("button", { name: "Add address" }).click();
    await page
        .getByLabel("Email address 2", { exact: true })
        .fill("second@example.test");
    await expect(
        page.getByRole("button", { name: "Add address" }),
    ).toBeDisabled();
    await expect(page.getByText("2 / 2", { exact: true })).toBeVisible();
    await page.route("**/reminders/", (route) =>
        route.fulfill({
            status: 400,
            json: errorBody(
                "invalid_request",
                "Check the highlighted values and try again.",
                {
                    fields: {
                        recipients:
                            "The recipient limit changed. Use at most 1 email address.",
                    },
                },
            ),
        }),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(page.locator(".notice[role=alert]")).toContainText(
        "recipient limit changed",
    );
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue("first@example.test");
    await expect(
        page.getByLabel("Email address 2", { exact: true }),
    ).toHaveValue("second@example.test");
});

test("saved recipient remains explicit when saving unchanged settings", async ({
    page,
}) => {
    await reminderApi(page);
    await openReminders(page);
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue(user.email);
    await expect(
        page.getByRole("button", {
            name: "Remove email address 1",
            exact: true,
        }),
    ).toHaveCount(0);
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON()).toMatchObject({
        recipients: [user.email],
        language: "en",
    });
});

test("duplicate recipient addresses are rejected", async ({ page }) => {
    await reminderApi(page);
    await openReminders(page);
    await page
        .getByLabel("Email address 1", { exact: true })
        .fill("First@example.test");
    await page.getByRole("button", { name: "Add address" }).click();
    await page
        .getByLabel("Email address 2", { exact: true })
        .fill("first@example.test");
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(page.locator(".notice[role=alert]")).toContainText(
        "Enter each email address only once.",
    );
});

test("saved reminder fields stay unavailable until the refreshed settings arrive", async ({
    page,
}) => {
    await reminderApi(page);
    const refresh = Promise.withResolvers<void>();
    let saving = false;
    await page.route("**/reminders/", async (route) => {
        if (route.request().method() === "PUT") saving = true;
        else if (saving) await refresh.promise;
        await route.fallback();
    });
    await openReminders(page);
    const address = page.getByLabel("Email address 1", { exact: true });
    await address.fill("custom@example.test");
    await page.getByRole("button", { name: "Save reminders" }).click();
    try {
        await expect(address).toHaveValue("custom@example.test");
        await expect(address).toBeDisabled();
        await expect(
            page.getByRole("button", { name: "Saving…" }),
        ).toBeDisabled();
    } finally {
        refresh.resolve();
    }
    await expect(address).toBeEnabled();
    await expect(address).toHaveValue("custom@example.test");
});

for (const width of [320, 390]) {
    test(`stored and custom recipients match the visible fields at ${width}px`, async ({
        page,
    }) => {
        await page.setViewportSize({ width, height: 900 });
        await reminderApi(page);
        await openReminders(page);
        const first = page.getByLabel("Email address 1", { exact: true });
        await expect(first).toHaveValue(user.email);
        await page.getByRole("button", { name: "Add address" }).focus();
        await page.keyboard.press("Enter");
        await page
            .getByLabel("Email address 2", { exact: true })
            .fill("custom@example.test");
        const both = page.waitForRequest(
            (request) =>
                request.method() === "PUT" &&
                request.url().endsWith("/reminders/"),
        );
        await page.getByRole("button", { name: "Save reminders" }).click();
        expect((await both).postDataJSON().recipients).toEqual([
            user.email,
            "custom@example.test",
        ]);
        await expect(first).toHaveValue(user.email);
        await first.fill("");
        const custom = page.waitForRequest(
            (request) =>
                request.method() === "PUT" &&
                request.url().endsWith("/reminders/"),
        );
        await page.getByRole("button", { name: "Save reminders" }).click();
        expect((await custom).postDataJSON().recipients).toEqual([
            "custom@example.test",
        ]);
        await expect(first).toHaveValue("custom@example.test");
        await expect(page.locator('input[name="recipients"]')).toHaveCount(1);
        await expect(
            page.getByRole("button", {
                name: "Remove email address 1",
                exact: true,
            }),
        ).toHaveCount(0);
        await page
            .getByRole("button", { name: "Add time", exact: true })
            .focus();
        await page.keyboard.press("Enter");
        await page.getByLabel("Delivery time 2", { exact: true }).fill("17:30");
        await expect(
            page.getByLabel("Delivery time 2", { exact: true }),
        ).toHaveAttribute("type", "time");
        expect(
            await page.evaluate(
                () => document.documentElement.scrollWidth <= window.innerWidth,
            ),
        ).toBe(true);
        await page
            .getByRole("button", {
                name: "Remove delivery time 1",
                exact: true,
            })
            .click();
        await expect(
            page.getByLabel("Delivery time 1", { exact: true }),
        ).toHaveValue("17:30");
        await expect(
            page.getByRole("button", {
                name: "Remove delivery time 1",
                exact: true,
            }),
        ).toHaveCount(0);
    });
}

test("empty recipient input snapshots the saving account email even for a paused schedule", async ({
    page,
}) => {
    await reminderApi(page, [user.email]);
    await openReminders(page);
    const explicit = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await explicit).postDataJSON().recipients).toEqual([user.email]);
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue(user.email);
    await page.getByLabel("Email address 1", { exact: true }).fill("");
    await page.getByLabel("Delivery time 1", { exact: true }).fill("");
    const cleared = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await cleared).postDataJSON()).toEqual({
        enabled: false,
        include_workspace_members: false,
        recipients: [],
        times: [],
        language: "en",
    });
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue(user.email);
});

test("native delivery time controls require a schedule only when enabled", async ({
    page,
}) => {
    await reminderApi(page);
    await openReminders(page);
    const first = page.getByLabel("Delivery time 1", { exact: true });
    await expect(first).toHaveAttribute("type", "time");
    await first.fill("");
    await page.getByLabel("Enable forecast emails for this plant").check();
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(page.locator(".notice[role=alert]")).toContainText(
        "Add at least one delivery time.",
    );
    await page.getByLabel("Enable forecast emails for this plant").uncheck();
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON().times).toEqual([]);
    await expect(first).toHaveValue("");
});

test("delivery time controls stop at the current daily limit", async ({
    page,
}) => {
    await reminderApi(page, [], 5, false, {}, 2);
    await openReminders(page);
    await page.getByRole("button", { name: "Add time", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Add time", exact: true }),
    ).toBeDisabled();
    await page
        .getByRole("button", { name: "Remove delivery time 2", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Add time", exact: true }),
    ).toBeEnabled();
});

test("a reduced limit preserves all saved addresses until repaired", async ({
    page,
}) => {
    await reminderApi(
        page,
        ["one@example.test", "two@example.test", "three@example.test"],
        2,
    );
    await openReminders(page);
    await expect(
        page.getByText("Sending is paused", { exact: false }),
    ).toBeVisible();
    await expect(
        page.getByLabel("Email address 3", { exact: true }),
    ).toHaveValue("three@example.test");
    await page.getByLabel("Enable forecast emails for this plant").check();
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(page.locator(".notice[role=alert]")).toContainText(
        "Use at most 2 email addresses.",
    );
    await page
        .getByRole("button", { name: "Remove email address 2", exact: true })
        .click();
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(
        page.getByText("Sending is paused", { exact: false }),
    ).toHaveCount(0);
    await openReminders(page);
    await expect(
        page.getByLabel("Email address 2", { exact: true }),
    ).toHaveValue("three@example.test");
});

test("reminder access errors do not reveal recipient controls", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/reminders/", (route) =>
        route.fulfill({
            status: 403,
            json: errorBody(
                "membership_required",
                "You do not have permission to access this plant.",
            ),
        }),
    );
    await openReminders(page);
    await expect(page.locator(".notice[role=alert]")).toContainText(
        "permission",
    );
    await expect(
        page.getByRole("button", { name: "Save reminders" }),
    ).toHaveCount(0);
});

test("shared plant capacity rejection retains entered addresses", async ({
    page,
}) => {
    await reminderApi(page, ["mine@example.test"], 5);
    await openReminders(page);
    await expect(page.locator('input[name="recipients"]')).toHaveCount(1);
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue("mine@example.test");
    await page.getByRole("button", { name: "Add address" }).click();
    await page
        .getByLabel("Email address 2", { exact: true })
        .fill("second@example.test");
    await page.route("**/reminders/", (route) =>
        route.fulfill({
            status: 400,
            json: errorBody(
                "invalid_request",
                "Check the highlighted values and try again.",
                {
                    fields: {
                        recipients:
                            "This plant has no room for another address. Remove an address or ask an administrator to raise the limit.",
                    },
                },
            ),
        }),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(page.locator(".notice[role=alert]")).toContainText(
        "Remove an address",
    );
    await expect(
        page.getByLabel("Email address 2", { exact: true }),
    ).toHaveValue("second@example.test");
});

test("an oversized list can be reduced in stages while sending remains paused", async ({
    page,
}) => {
    await reminderApi(
        page,
        Array.from({ length: 7 }, (_, index) => `person${index}@example.test`),
    );
    await openReminders(page);
    await page
        .getByRole("button", { name: "Remove email address 7", exact: true })
        .click();
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON().recipients).toHaveLength(6);
    await openReminders(page);
    await expect(
        page.getByLabel("Email address 6", { exact: true }),
    ).toHaveValue("person5@example.test");
    await expect(
        page.getByText("Sending is paused", { exact: false }),
    ).toBeVisible();
});

test("an enabled oversized recipient list can be paused without removing addresses", async ({
    page,
}) => {
    const recipients = Array.from(
        { length: 6 },
        (_, index) => `person${index}@example.test`,
    );
    await reminderApi(page, recipients, 5, true);
    await openReminders(page);
    await page.getByLabel("Enable forecast emails for this plant").uncheck();
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
        { timeout: 3000 },
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON()).toMatchObject({
        enabled: false,
        recipients,
    });
    await openReminders(page);
    await expect(
        page.getByLabel("Enable forecast emails for this plant"),
    ).not.toBeChecked();
    await expect(page.locator('input[name="recipients"]')).toHaveCount(6);
});
