import { expect, test } from "./support/test";
import type { Page } from "@playwright/test";
import { mockApi, paged, plant, region, user } from "./support/workspace";

const STATES = [
    ["sending", "Sending"],
    ["paused_by_user", "Paused"],
    ["waiting_for_approval", "Waiting for approval"],
    ["provider_attention", "Provider account needs attention"],
    ["no_recipients", "No recipients"],
    ["invalid_recipient", "Invalid recipient"],
    ["over_recipient_limit", "Over the recipient limit"],
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

for (const [state, label] of STATES) {
    test(`the plant list and the reminders screen name the ${state} state with the latest outcome`, async ({
        page,
    }) => {
        await mockApi(page);
        await plantList(page, { state, last_delivery: LAST });
        await reminders(page, state);
        await page.goto("/app/");
        const card = page.getByRole("button", { name: /East array/ });
        await expect(card).toContainText(`Daily email: ${label}`);
        await expect(card).toContainText("Last email: Not sent");
        await card.click();
        await page
            .getByRole("button", { name: "Irradiance forecast", exact: true })
            .click();
        await page
            .getByRole("button", { name: "Settings", exact: true })
            .click();
        await page
            .getByRole("button", { name: "Forecast email", exact: true })
            .click();
        const panel = page.getByRole("region", { name: "Forecast email" });
        await expect(panel.getByText(label, { exact: true })).toBeVisible();
        await expect(panel.getByRole("status")).toContainText(
            "Last email: Not sent",
        );
        await expect(panel.getByRole("status")).toContainText(
            "The mail service rejected the message.",
        );
    });
}

test("a plant whose reminders were never saved shows no reminder line", async ({
    page,
}) => {
    await mockApi(page);
    await plantList(page, null);
    await page.goto("/app/");
    const card = page.getByRole("button", { name: /East array/ });
    await expect(card).toBeVisible();
    await expect(card).not.toContainText("Daily email");
});

test("a plant whose reminders never ran names its state without an outcome", async ({
    page,
}) => {
    await mockApi(page);
    await plantList(page, { state: "sending", last_delivery: null });
    await page.goto("/app/");
    const card = page.getByRole("button", { name: /East array/ });
    await expect(card).toContainText("Daily email: Sending");
    await expect(card).not.toContainText("Last email");
});
