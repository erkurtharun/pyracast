import { test, expect } from "./support/test";
import { answer, invitationPreview, mockApi } from "./support/workspace";
import { errorBody } from "./support/errors";
import type { components } from "../src/lib/schema";
import enWorkspace from "../messages/en/workspace.json";
import trWorkspace from "../messages/tr/workspace.json";

type Invitation = components["schemas"]["InvitationOutput"];

test("an invitee sees the workspace, role and inviter before signing in and then accepts", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    page.on("request", (request) => {
        expect(request.url()).not.toContain("invite-secret");
    });
    const previewed: string[] = [];
    await page.route("**/api/invitations/preview/", (route) => {
        previewed.push(JSON.stringify(route.request().postDataJSON()));
        return route.fulfill({ json: invitationPreview });
    });
    let accepted = 0;
    await page.route("**/api/invitations/accept/", (route) => {
        expect(route.request().postDataJSON()).toEqual({
            token: "invite-secret",
        });
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        accepted++;
        return route.fulfill({
            json: answer("InvitationAcceptanceOutput", {
                tenant_id: 1,
                role: "viewer",
            }),
        });
    });
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(page).toHaveURL("/app/invitation/");
    const invitation = page.getByRole("region", { name: "Invitation" });
    await expect(invitation).toContainText(
        "You are invited to join Sunfield Energy.",
    );
    await expect(invitation).toContainText("Role: Member");
    await expect(invitation).toContainText("Invited by: owner@example.test");
    await expect(invitation).toContainText(
        "Sign in or create an account with solar@example.test to accept it.",
    );
    await page.getByLabel("Email", { exact: true }).fill("solar@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("heading", { name: "Invitation", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("You are invited to join Sunfield Energy."),
    ).toBeVisible();
    await expect(
        page.getByText("Signed in as solar@example.test"),
    ).toBeVisible();
    await page.getByRole("button", { name: "Accept invitation" }).click();
    await expect(page).toHaveURL("/app/w/1/");
    expect(accepted).toBe(1);
    expect(new Set(previewed)).toEqual(
        new Set([JSON.stringify({ token: "invite-secret" })]),
    );
    expect(
        await page.evaluate(() =>
            sessionStorage.getItem("pyracast.workspaceInvitation"),
        ),
    ).toBeNull();
});

for (const anonymous of [true, false])
    test(`an unusable invitation link is explained ${anonymous ? "before sign-in" : "to a signed-in account"} and can be dismissed`, async ({
        page,
    }) => {
        await mockApi(page, { anonymous });
        let acceptances = 0;
        await page.route("**/api/invitations/preview/", (route) =>
            route.fulfill({
                status: 400,
                json: errorBody(
                    "invitation_unavailable",
                    "The invitation is invalid or expired.",
                ),
            }),
        );
        await page.route("**/api/invitations/accept/", (route) => {
            acceptances++;
            return route.fallback();
        });
        await page.goto("/app/invitation/#token=closed-secret");
        await expect(
            page.getByText(
                "This invitation link can no longer be used. Ask the workspace administrator to send it again.",
            ),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Accept invitation" }),
        ).toHaveCount(0);
        if (anonymous) {
            await page
                .getByRole("button", {
                    name: "Withdraw invitation",
                    exact: true,
                })
                .click();
            await expect(page).toHaveURL("/app/w/");
        } else {
            await page
                .getByRole("button", { name: "Cancel", exact: true })
                .click();
            await expect(page).toHaveURL("/app/w/1/");
        }
        expect(acceptances).toBe(0);
        expect(
            await page.evaluate(() =>
                sessionStorage.getItem("pyracast.workspaceInvitation"),
            ),
        ).toBeNull();
    });

test("a preview that cannot be loaded still lets the invitee accept", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/invitations/preview/", (route) =>
        route.fulfill({
            status: 503,
            headers: { "Retry-After": "1" },
            json: errorBody(
                "busy",
                "The workspace is busy. Try again in a moment.",
                { retry_after: 1 },
            ),
        }),
    );
    await page.route("**/api/invitations/accept/", (route) =>
        route.fulfill({
            json: answer("InvitationAcceptanceOutput", {
                tenant_id: 1,
                role: "viewer",
            }),
        }),
    );
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(
        page.getByText("The workspace is busy. Try again in a moment."),
    ).toBeVisible();
    await page.getByRole("button", { name: "Accept invitation" }).click();
    await expect(page).toHaveURL("/app/w/1/");
});

for (const anonymous of [true, false])
    for (const link of ["/app/invitation/#token=", "/app/invitation/"])
        test(`an invitation page ${link.endsWith("=") ? "with an empty token" : "without a token"} says the link cannot be used ${anonymous ? "before sign-in" : "to a signed-in account"}`, async ({
            page,
        }) => {
            await mockApi(page, { anonymous });
            const previews: unknown[] = [];
            await page.route("**/api/invitations/preview/", (route) => {
                previews.push(route.request().postDataJSON());
                return route.fallback();
            });
            await page.goto(link);
            await expect(
                page.getByText(
                    "This invitation link can no longer be used. Ask the workspace administrator to send it again.",
                ),
            ).toBeVisible();
            await expect(
                page.getByRole("button", { name: "Accept invitation" }),
            ).toHaveCount(0);
            await expect(page).toHaveURL("/app/invitation/");
            expect(previews).toEqual([]);
        });

test("each invitation row button is named by its visible label first in both languages", () => {
    for (const { WorkspaceMembers: labels } of [enWorkspace, trWorkspace]) {
        expect(labels.resendNamed).toBe(`${labels.resend}: {email}`);
        expect(labels.cancelNamed).toBe(`${labels.cancel}: {email}`);
    }
});

for (const first of ["resend", "cancel"] as const)
    test(`while ${first === "resend" ? "an invitation is sent again" : "an invitation is cancelled"} no other invitation action can start`, async ({
        page,
    }) => {
        await mockApi(page);
        const invitation = (id: number): Invitation => ({
            id,
            email: "late@example.test",
            role: "viewer",
            state: "pending",
            created_at: "2026-11-20T09:00:00Z",
            expires_at: "2026-11-27T09:00:00Z",
            delivery_status: "sent",
        });
        let rows = [invitation(3)];
        const writes: string[] = [];
        let release = () => {};
        const held = new Promise<void>((resolve) => {
            release = resolve;
        });
        await page.route("**/api/tenants/1/members/", (route) =>
            route.fulfill({
                json: answer("WorkspaceMembersOutput", {
                    members: [
                        { id: 1, email: "owner@example.test", role: "admin" },
                    ],
                    plants_at_recipient_limit: 0,
                }),
            }),
        );
        await page.route("**/api/tenants/1/invitations/", (route) =>
            route.fulfill({
                json: answer("WorkspaceInvitationsOutput", {
                    invitations: rows,
                }),
            }),
        );
        await page.route(
            "**/api/tenants/1/invitations/3/resend/",
            async (route) => {
                writes.push("resend");
                await held;
                rows = [invitation(4)];
                await route.fulfill({ status: 201, json: rows[0] });
            },
        );
        await page.route("**/api/tenants/1/invitations/3/", async (route) => {
            expect(route.request().method()).toBe("DELETE");
            writes.push("cancel");
            await held;
            rows = [];
            await route.fulfill({ status: 204 });
        });
        await page.goto("/app/");
        await page
            .getByRole("button", { name: "Members", exact: true })
            .click();
        const resend = page.getByRole("button", {
            name: "Send again: late@example.test",
            exact: true,
        });
        const cancel = page.getByRole("button", {
            name: "Withdraw invitation: late@example.test",
            exact: true,
        });
        const [started, other] =
            first === "resend" ? [resend, cancel] : [cancel, resend];
        await started.click();
        await expect.poll(() => writes).toEqual([first]);
        await expect(other).toBeDisabled();
        await other.click({ force: true });
        release();
        const region = page.locator("[data-live-region]");
        if (first === "resend") {
            await expect(region).toHaveText(
                "A new invitation is being sent to late@example.test.",
            );
            await expect(
                page
                    .locator(".workspace-member", {
                        hasText: "late@example.test",
                    })
                    .getByText("Pending", { exact: true }),
            ).toBeVisible();
            await expect(cancel).toBeEnabled();
            expect(rows.map((row) => [row.id, row.state])).toEqual([
                [4, "pending"],
            ]);
        } else {
            await expect(region).toHaveText("Invitation withdrawn.");
            await expect(page.getByText("No open invitations.")).toBeVisible();
            expect(rows).toEqual([]);
        }
        expect(writes).toEqual([first]);
    });
