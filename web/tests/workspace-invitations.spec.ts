import { test, expect } from "./support/test";
import { invitationPreview, mockApi } from "./support/workspace";
import { errorBody } from "./support/errors";

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
        return route.fulfill({ json: { tenant_id: 1, role: "viewer" } });
    });
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(page).toHaveURL("/app/invitation/");
    const invitation = page.getByRole("region", { name: "Invitation" });
    await expect(invitation).toContainText(
        "You are invited to join Sunfield Energy.",
    );
    await expect(invitation).toContainText("Role: Member");
    await expect(invitation).toContainText("invited by owner@example.test");
    await expect(invitation).toContainText(
        "Sign in or create an account with solar@example.test to accept it.",
    );
    await page.getByLabel("Email", { exact: true }).fill("solar@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("heading", { name: "Join workspace" }),
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
                    name: "Cancel invitation",
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
