import { expect, test } from "./support/test";
import type { Page } from "@playwright/test";
import { mockApi, paged, plant, region, user } from "./support/workspace";

const STATES = [
    ["sending", "On", "Forecast email: Not sent"],
    ["paused_by_user", "Off", "Forecast email off"],
    [
        "waiting_for_approval",
        "Waiting for approval by Pyracast",
        "Forecast email: Waiting for approval by Pyracast",
    ],
    ["provider_attention", "Paused", "Forecast email: Paused"],
    ["no_recipients", "No recipients", "Forecast email: No recipients"],
    ["invalid_recipient", "Invalid address", "Forecast email: Invalid address"],
    [
        "over_recipient_limit",
        "Too many recipients",
        "Forecast email: Too many recipients",
    ],
] as const;

const LAST = {
    scheduled_for: "2026-01-01T05:00:00Z",
    status: "failed",
    last_error: "mail_rejected",
} as const;

async function plantList(page: Page, reminder: unknown) {
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({ json: paged([{ ...region, plant, reminder }]) }),
    );
}

async function reminders(page: Page, state: string) {
    await page.route("**/reminders/", (route) =>
        route.fulfill({
            json: {
                enabled: state !== "paused_by_user",
                state,
                revision: 3,
                language: "en",
                include_workspace_members: false,
                hide_recipients: true,
                addresses: {
                    recipients: [user.email],
                    workspace_recipients: [],
                    members: [],
                    added: [
                        {
                            address: user.email,
                            role: null,
                            eligible: true,
                            memberships: [],
                        },
                    ],
                },
                recipient_limit: 5,
                recipient_count: 1,
                workspace_member_count: 0,
                recipient_limit_exceeded: false,
                times: ["08:00"],
                daily_reminder_limit: 10,
                deliveries: [
                    {
                        id: 1,
                        ...LAST,
                        recipient_count: 1,
                        recipients: [user.email],
                        collected_at: null,
                        source_age_seconds: null,
                        stale: false,
                        sent_at: null,
                        late: null,
                        forecast_status: null,
                        forecast_complete: null,
                    },
                ],
            },
        }),
    );
}

for (const [state, label, line] of STATES) {
    test(`the plant list and the reminders screen name the ${state} state with the latest outcome`, async ({
        page,
    }) => {
        await mockApi(page);
        await plantList(page, { state, last_delivery: LAST });
        await reminders(page, state);
        await page.goto("/app/");
        const card = page.getByRole("button", { name: /East array/ });
        await expect(card.locator(".fine")).toHaveText([line]);
        await card.click();
        await page
            .getByRole("button", { name: "Irradiance", exact: true })
            .click();
        await page
            .getByRole("button", { name: "Settings", exact: true })
            .click();
        await page
            .getByRole("button", { name: "Email reminders", exact: true })
            .click();
        const panel = page.getByRole("region", { name: "Email reminders" });
        await expect(panel.getByText(label, { exact: true })).toBeVisible();
        await expect(panel.getByRole("status")).toContainText(
            "Last email: Not sent",
        );
        await expect(panel.getByRole("status")).toContainText(
            "The email service rejected the email.",
        );
    });
}

test("a plant whose forecast email was never saved shows no forecast email line", async ({
    page,
}) => {
    await mockApi(page);
    await plantList(page, null);
    await page.goto("/app/");
    const card = page.getByRole("button", { name: /East array/ });
    await expect(card).toBeVisible();
    await expect(card).not.toContainText("Forecast email");
});

for (const [status, outcome] of [
    ["sent", "was last sent"],
    [null, "has not been sent yet"],
] as const)
    test(`a plant whose forecast email is on and ${outcome} shows no forecast email line`, async ({
        page,
    }) => {
        await mockApi(page);
        await plantList(page, {
            state: "sending",
            last_delivery: status && { ...LAST, status, last_error: "" },
        });
        await page.goto("/app/");
        const card = page.getByRole("button", { name: /East array/ });
        await expect(card).toContainText("Open plant");
        await expect(card).not.toContainText("Forecast email");
    });

test("a plant whose forecast email is on and not known to have been sent says so", async ({
    page,
}) => {
    await mockApi(page);
    await plantList(page, {
        state: "sending",
        last_delivery: { ...LAST, status: "uncertain", last_error: "" },
    });
    await page.goto("/app/");
    const card = page.getByRole("button", { name: /East array/ });
    await expect(card).toContainText("Forecast email: Not known whether sent");
});
