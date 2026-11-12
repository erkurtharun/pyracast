import { expect, test } from "./support/test";
import type { Page } from "@playwright/test";
import { mockApi, region, user } from "./support/workspace";

async function openReminders(page: Page) {
    await page.goto("/");
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
    recipients: string[] = [],
    limit = 5,
    otherRecipientCount = 0,
    enabled = false,
    delivery: Record<string, unknown> = {},
) {
    await mockApi(page);
    let saved = { enabled, recipients, times: ["08:00"] };
    await page.route("**/reminders/", async (route) => {
        if (route.request().method() === "PUT")
            saved = route.request().postDataJSON();
        await route.fulfill({
            json: {
                ...saved,
                effective_recipients: saved.recipients.length
                    ? saved.recipients
                    : [user.email],
                recipient_limit: limit,
                recipient_count:
                    Math.max(1, saved.recipients.length) + otherRecipientCount,
                recipient_slots_remaining: Math.max(
                    0,
                    limit -
                        Math.max(1, saved.recipients.length) -
                        otherRecipientCount,
                ),
                recipient_limit_exceeded:
                    Math.max(1, saved.recipients.length) + otherRecipientCount >
                    limit,
                timezone: region.timezone,
                quota: { daily_reminder_limit: 10, queries_per_email: 0 },
                deliveries: [
                    {
                        id: 1,
                        recipient: saved.recipients[0] ?? user.email,
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

for (const [kind, label] of [
    ["saved_complete", "Saved forecast"],
    ["saved_partial", "Partial forecast"],
    ["unavailable", "No forecast available"],
]) {
    test(`delivered ${kind} is distinguished from mail acceptance`, async ({
        page,
    }) => {
        await reminderApi(page, [], 5, 0, true, { forecast_status: kind });
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
    await reminderApi(page, [], 5, 0, true, {
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
    await expect(
        page.getByText("5 of 5 addresses.", { exact: false }),
    ).toBeVisible();
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
        page.getByRole("cell", { name: "person1@example.test", exact: true }),
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
    await expect(
        page.getByText("2 of 2 addresses.", { exact: false }),
    ).toBeVisible();
    await page.route("**/reminders/", (route) =>
        route.fulfill({
            status: 400,
            json: {
                recipients: [
                    "The recipient limit changed. Use at most 1 email address.",
                ],
            },
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

test("empty recipients use account email and duplicate addresses are rejected", async ({
    page,
}) => {
    await reminderApi(page);
    await openReminders(page);
    await expect(
        page.getByText(`(${user.email})`, { exact: false }),
    ).toBeVisible();
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON().recipients).toEqual([]);
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
            json: {
                detail: "You do not have permission to access this plant.",
            },
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

test("shared plant capacity is shown without other members' email addresses", async ({
    page,
}) => {
    await reminderApi(page, ["mine@example.test"], 5, 3);
    await openReminders(page);
    await expect(
        page.getByText("Saved plant recipients: 4 of 5.", { exact: false }),
    ).toContainText("1 new address slots remaining.");
    await expect(
        page.getByText("This limit is shared by all plant members", {
            exact: false,
        }),
    ).toBeVisible();
    await expect(page.locator('input[name="recipients"]')).toHaveCount(1);
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue("mine@example.test");
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
    await reminderApi(page, recipients, 5, 0, true);
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
