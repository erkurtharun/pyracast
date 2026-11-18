import { expect, test, type Page } from "./support/test";
import {
    mockApi,
    paged,
    plant,
    region,
    tenantRow,
    user,
} from "./support/workspace";

async function mockMembers(page: Page) {
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [{ id: 1, email: user.email, role: "admin" }],
            },
        }),
    );
    await page.route("**/api/tenants/1/invitations/", (route) =>
        route.fulfill({ json: { invitations: [] } }),
    );
}

test("refresh and Back keep the workspace, the view and the forecast date", async ({
    page,
}) => {
    await mockApi(page);
    await mockMembers(page);
    await page.goto("/app/");
    await expect(page).toHaveURL("/app/w/1/");
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/forecast/");
    await page.getByLabel("Forecast date").fill("2026-11-25");
    await expect(page).toHaveURL(
        "/app/w/1/plants/11/forecast/?date=2026-11-25",
    );
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    await expect(page).toHaveURL(
        "/app/w/1/plants/11/forecast/?date=2026-11-25&view=irradiance",
    );
    await page.reload();
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-25");
    await expect(
        page.getByRole("button", { name: "Irradiance", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/production/");
    await page.goBack();
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-25");
    await expect(
        page.getByRole("button", { name: "Irradiance", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await page.getByRole("button", { name: /All plants/ }).click();
    await expect(page).toHaveURL("/app/w/1/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    await expect(page).toHaveURL("/app/w/1/members/");
    await page.reload();
    await expect(
        page.getByRole("heading", { name: "Members", exact: true }),
    ).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL("/app/w/1/");
    await expect(page.getByRole("heading", { name: /Plants/ })).toBeVisible();
});

test("the workspace lives in the address and survives a reload", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
                tenantRow({ id: 2, name: "Second Energy", role: "viewer" }),
            ]),
        }),
    );
    await page.route("**/api/tenants/2/sites/", (route) =>
        route.fulfill({
            json: paged([
                {
                    ...region,
                    id: 12,
                    name: "Konya field",
                    plant: { ...plant, id: 22, name: "West array" },
                },
            ]),
        }),
    );
    await page.goto("/app/w/2/");
    await expect(
        page.getByRole("button", { name: /West array/ }),
    ).toBeVisible();
    await expect(page.getByLabel("Workspace")).toHaveValue("2");
    await page.getByLabel("Workspace").selectOption("1");
    await expect(page).toHaveURL("/app/w/1/");
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
    await page.reload();
    await expect(page.getByLabel("Workspace")).toHaveValue("1");
    await page.goBack();
    await expect(page).toHaveURL("/app/w/2/");
    await expect(
        page.getByRole("button", { name: /West array/ }),
    ).toBeVisible();
});

test("an unknown workspace in the address shows no other workspace's plants", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/w/9/");
    await expect(
        page.getByText("This workspace is not available to your account."),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /East array/ })).toHaveCount(
        0,
    );
});

test("account settings have their own address", async ({ page }) => {
    await mockApi(page);
    await page.goto("/app/account/");
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: /Plants/ }).click();
    await expect(page).toHaveURL("/app/w/1/");
});

test("no legacy entry path remains", async ({ page }) => {
    await mockApi(page);
    const accept = await page.goto("/invitations/accept/#token=invite-secret");
    expect(accept?.status()).toBe(404);
    const home = await page.goto("/?account=security");
    expect(home?.status()).toBe(200);
    await expect(page).toHaveURL("/?account=security");
    for (const legacy of [
        "/app/#token=invite-secret",
        "/app/?token=invite-secret",
        "/app/?account=security",
        "/app/#plant-11/settings",
    ]) {
        await page.goto(legacy);
        await expect(page, legacy).toHaveURL("/app/w/1/");
        await expect(
            page.getByRole("heading", { name: /Plants/ }),
            legacy,
        ).toBeVisible();
        await expect(
            page.getByRole("heading", { name: "Join workspace" }),
        ).toHaveCount(0);
    }
});

test("an invitation link opens the acceptance screen and drops the token from the address", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(
        page.getByRole("heading", { name: "Join workspace" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/invitation/");
});

test("a sign-out or another account never shows the previous account's cached plants", async ({
    page,
}) => {
    await mockApi(page);
    let account = 1;
    let secondSignedIn = false;
    let releaseSites!: () => void;
    const sitesHeld = new Promise<void>((resolve) => {
        releaseSites = resolve;
    });
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        secondSignedIn = account === 2;
        return route.fallback();
    });
    await page.route("**/api/session/", (route) =>
        secondSignedIn
            ? route.fulfill({
                  json: {
                      user: { ...user, id: 2, email: "other@example.test" },
                      csrf_token: "signed-token",
                  },
              })
            : route.fallback(),
    );
    await page.route("**/api/tenants/1/sites/", async (route) => {
        if (!secondSignedIn) return route.fallback();
        await sitesHeld;
        await route.fulfill({
            json: paged([
                {
                    ...region,
                    id: 13,
                    plant: { ...plant, id: 23, name: "Second account array" },
                },
            ]),
        });
    });
    await page.goto("/app/");
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(
        page.locator("form").getByRole("button", { name: "Sign in" }),
    ).toBeVisible();
    account = 2;
    await page.getByLabel("Email", { exact: true }).fill("other@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Loading plants")).toBeVisible();
    await expect(page.getByRole("button", { name: /East array/ })).toHaveCount(
        0,
    );
    releaseSites();
    await expect(
        page.getByRole("button", { name: /Second account array/ }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /East array/ })).toHaveCount(
        0,
    );
});
