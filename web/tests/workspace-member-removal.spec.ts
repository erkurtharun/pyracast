import { test, expect, type Page } from "./support/test";
import { mockApi, paged, user } from "./support/workspace";
import { errorBody } from "./support/errors";

const viewer = {
    id: 2,
    email: "viewer.with.a.long.address@example.test",
    role: "viewer",
};
const owner = { id: 3, email: "owner@example.test", role: "admin" };
const self = { id: user.id, email: user.email, role: "viewer" };

async function openMembers(page: Page) {
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    await expect(page.getByText(viewer.email, { exact: true })).toBeVisible();
}

async function mockMembers(page: Page) {
    await mockApi(page);
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({ json: { members: [owner, self, viewer] } }),
    );
    await page.route("**/api/tenants/*/invitations/", (route) =>
        route.fulfill({ json: { invitations: [] } }),
    );
}

for (const width of [320, 390]) {
    test(`administrator confirms member removal at ${width}px without deleting an account`, async ({
        page,
    }, testInfo) => {
        await mockMembers(page);
        await page.setViewportSize({ width, height: 844 });
        let removed = false;
        let deletes = 0;
        let release: () => void = () => {};
        const pending = new Promise<void>((resolve) => {
            release = resolve;
        });
        await page.route("**/api/tenants/1/members/", (route) =>
            route.fulfill({
                json: {
                    members: removed ? [owner, self] : [owner, self, viewer],
                },
            }),
        );
        await page.route("**/api/tenants/1/members/2/", async (route) => {
            expect(route.request().method()).toBe("DELETE");
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            deletes++;
            await pending;
            removed = true;
            await route.fulfill({ status: 204 });
        });
        await openMembers(page);
        const action = page.getByRole("button", {
            name: `Remove from workspace: ${viewer.email}`,
            exact: true,
        });
        await expect(
            page.getByRole("button", { name: /^Remove from workspace: / }),
        ).toHaveCount(1);
        await action.click();
        const confirmation = page.getByRole("region", {
            name: "Confirm member removal",
        });
        await expect(confirmation).toContainText(viewer.email);
        await expect(confirmation).toContainText(
            "Their account and other workspaces will stay available.",
        );
        await confirmation
            .getByRole("button", { name: "Cancel", exact: true })
            .click();
        expect(deletes).toBe(0);
        await action.click();
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.screenshot({
            path: testInfo.outputPath(`remove-member-${width}.png`),
            fullPage: true,
        });
        expect(
            await page.evaluate(
                () => document.documentElement.scrollWidth <= innerWidth,
            ),
        ).toBe(true);
        await confirmation
            .getByRole("button", { name: "Remove from workspace", exact: true })
            .click();
        await expect(
            confirmation.getByRole("button", {
                name: "Removing…",
                exact: true,
            }),
        ).toBeDisabled();
        await expect(action).toBeDisabled();
        release();
        await expect(page.getByRole("status")).toContainText(
            `${viewer.email} was removed from this workspace.`,
        );
        await expect(
            page
                .locator(".member-list")
                .first()
                .getByText(viewer.email, { exact: true }),
        ).toHaveCount(0);
        await expect(
            page.getByText(owner.email, { exact: true }),
        ).toBeVisible();
        await expect(page.getByText(user.email, { exact: true })).toBeVisible();
        expect(deletes).toBe(1);
    });
}

test("failed removal refreshes current membership and permits a fresh confirmation", async ({
    page,
}) => {
    await mockMembers(page);
    let reads = 0;
    await page.route("**/api/tenants/1/members/", (route) => {
        reads++;
        return route.fulfill({ json: { members: [owner, viewer] } });
    });
    await page.route("**/api/tenants/1/members/2/", (route) =>
        route.fulfill({
            status: 400,
            json: errorBody(
                "invalid_request",
                "Unable to remove this member. Try again.",
            ),
        }),
    );
    await openMembers(page);
    const initialReads = reads;
    await page
        .getByRole("button", { name: `Remove from workspace: ${viewer.email}` })
        .click();
    await page
        .getByRole("region", { name: "Confirm member removal" })
        .getByRole("button", { name: "Remove from workspace", exact: true })
        .click();
    await expect(page.locator(".notice[role=alert]")).toContainText(
        "Unable to remove this member. Try again.",
    );
    await expect.poll(() => reads).toBeGreaterThan(initialReads);
    await expect(page.getByText(viewer.email, { exact: true })).toBeVisible();
    await page
        .getByRole("button", { name: `Remove from workspace: ${viewer.email}` })
        .click();
    await expect(
        page.getByRole("region", { name: "Confirm member removal" }),
    ).toBeVisible();
});

test("lost administrator access refreshes workspace role and removes write controls", async ({
    page,
}) => {
    await mockMembers(page);
    let role = "admin";
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([{ id: 1, name: "Sunfield Energy", role }]),
        }),
    );
    await page.route("**/api/tenants/1/members/2/", (route) => {
        role = "viewer";
        return route.fulfill({
            status: 403,
            json: errorBody(
                "workspace_admin_required",
                "Workspace administrator access is required.",
            ),
        });
    });
    await openMembers(page);
    await page
        .getByRole("button", { name: `Remove from workspace: ${viewer.email}` })
        .click();
    await page
        .getByRole("region", { name: "Confirm member removal" })
        .getByRole("button", { name: "Remove from workspace", exact: true })
        .click();
    await expect(
        page.getByText("Member · changes require approval", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Members", exact: true }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("button", {
            name: "Remove from workspace",
            exact: true,
        }),
    ).toHaveCount(0);
});

test("switching to a viewer workspace on Members shows Plants as current", async ({
    page,
}) => {
    await mockMembers(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                { id: 1, name: "Sunfield Energy", role: "admin" },
                { id: 9, name: "Viewer workspace", role: "viewer" },
            ]),
        }),
    );
    await page.route(/\/api\/tenants\/9\/(regions|sites)\/$/, (route) =>
        route.fulfill({ json: paged([]) }),
    );
    await openMembers(page);
    await page
        .getByRole("combobox", { name: "Workspace", exact: true })
        .selectOption("9");
    const overview = page
        .getByRole("navigation")
        .getByRole("button", { name: "Plants", exact: true });
    await expect(overview).toHaveAttribute("aria-current", "page");
    await expect(page.locator(".topbar strong")).toHaveText("Plants");
});

test("switching workspace discards a pending confirmation and uses the new tenant URL", async ({
    page,
}) => {
    await mockMembers(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                { id: 1, name: "Sunfield Energy", role: "admin" },
                { id: 9, name: "Other workspace", role: "admin" },
            ]),
        }),
    );
    await page.route("**/api/tenants/9/members/", (route) =>
        route.fulfill({ json: { members: [viewer] } }),
    );
    let requested = "";
    await page.route("**/api/tenants/*/members/2/", (route) => {
        requested = new URL(route.request().url()).pathname;
        return route.fulfill({ status: 204 });
    });
    await openMembers(page);
    await page
        .getByRole("button", { name: `Remove from workspace: ${viewer.email}` })
        .click();
    await page
        .getByRole("combobox", { name: "Workspace", exact: true })
        .selectOption("9");
    await expect(
        page.getByRole("region", { name: "Confirm member removal" }),
    ).toHaveCount(0);
    expect(requested).toBe("");
    await page
        .getByRole("button", { name: `Remove from workspace: ${viewer.email}` })
        .click();
    await page
        .getByRole("region", { name: "Confirm member removal" })
        .getByRole("button", { name: "Remove from workspace", exact: true })
        .click();
    await expect.poll(() => requested).toBe("/api/tenants/9/members/2/");
});
