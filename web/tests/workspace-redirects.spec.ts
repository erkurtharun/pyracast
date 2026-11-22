import { expect, test, type Page } from "./support/test";
import { mockApi, paged, tenantRow } from "./support/workspace";

async function hold(page: Page, matches: (url: URL) => boolean) {
    const gate = Promise.withResolvers<void>();
    await page.route(matches, async (route) => {
        if (route.request().resourceType() !== "document") await gate.promise;
        await route.fallback();
    });
    return gate.resolve;
}

const workspaceSelect = (page: Page) =>
    page.getByRole("combobox", { name: "Workspace", exact: true });

test("a click on Account before the workspaces answer opens the account settings", async ({
    page,
}) => {
    await mockApi(page);
    const releaseTenants = await hold(
        page,
        (url) => url.pathname === "/api/tenants/",
    );
    const releaseAccount = await hold(page, (url) =>
        url.pathname.startsWith("/app/account/"),
    );
    await page.goto("/app/w/");
    await expect(page.getByText("Loading your workspaces…")).toBeVisible();
    await page.getByRole("button", { name: "Account", exact: true }).click();
    releaseTenants();
    await expect(workspaceSelect(page)).toHaveValue("1");
    releaseAccount();
    await expect(page).toHaveURL("/app/account/");
    await expect(
        page.getByRole("button", { name: "Account", exact: true }),
    ).toHaveAttribute("aria-current", "page");
});

test("a click on Inbox while a role change withdraws the members page opens the inbox", async ({
    page,
}) => {
    await page.clock.install({ time: new Date("2026-11-10T12:00:00Z") });
    await mockApi(page);
    let role: "admin" | "viewer" = "admin";
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([tenantRow({ id: 1, name: "Sunfield Energy", role })]),
        }),
    );
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({ json: { members: [] } }),
    );
    await page.route("**/api/tenants/1/invitations/", (route) =>
        route.fulfill({ json: { invitations: [] } }),
    );
    const releaseInbox = await hold(page, (url) =>
        url.pathname.startsWith("/app/w/1/inbox/"),
    );
    await page.goto("/app/w/1/members/");
    await expect(
        page.getByRole("heading", { name: "Members", level: 1, exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    role = "viewer";
    await page.clock.runFor(120_001);
    await expect(
        page.getByText("Member · changes require approval", { exact: true }),
    ).toBeVisible();
    releaseInbox();
    await expect(page).toHaveURL("/app/w/1/inbox/");
    await expect(
        page.getByRole("button", { name: "Inbox", exact: true }),
    ).toHaveAttribute("aria-current", "page");
});

test("the workspace index opens the portfolio once the workspaces answer late", async ({
    page,
}) => {
    await mockApi(page);
    const releaseTenants = await hold(
        page,
        (url) => url.pathname === "/api/tenants/",
    );
    await page.goto("/app/w/");
    await expect(page.getByText("Loading your workspaces…")).toBeVisible();
    await expect(page).toHaveURL("/app/w/");
    releaseTenants();
    await expect(page).toHaveURL("/app/w/1/");
});
