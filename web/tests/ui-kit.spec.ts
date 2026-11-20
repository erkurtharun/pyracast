import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "./support/test";
import { mockApi, user } from "./support/workspace";

async function scan(page: Page, selector: string) {
    const { violations } = await new AxeBuilder({ page })
        .include(selector)
        .analyze();
    expect(violations).toEqual([]);
}

test("view navigation names its group and marks the current view", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    const sections = page.getByRole("navigation", { name: "Plant sections" });
    await expect(
        sections.getByRole("button", { name: "Forecast", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await expect(sections.locator("[aria-current]")).toHaveCount(1);
    const views = page.getByRole("navigation", { name: "Forecast views" });
    await expect(
        views.getByRole("button", { name: "Generation", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await views
        .getByRole("button", { name: "Irradiance", exact: true })
        .click();
    await expect(
        views.getByRole("button", { name: "Irradiance", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await expect(views.locator("[aria-current]")).toHaveCount(1);
    for (const button of await page
        .locator(".tabs button, .subnav button")
        .all())
        await expect(button).toHaveAttribute("type", "button");
    await scan(page, ".tabs");
    await scan(page, ".subnav");
});

test("labelled controls are reachable by their visible labels", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(
        page.getByLabel("Forecast date", { exact: true }),
    ).toHaveAttribute("type", "date");
    await expect(page.getByLabel("Resolution")).toHaveValue("60");
    await scan(page, ".forecast-controls");
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await expect(page.getByLabel("Day", { exact: true })).toHaveAttribute(
        "type",
        "date",
    );
    const fullDay = page.getByRole("checkbox", {
        name: "Show full day",
        exact: true,
    });
    await expect(fullDay).not.toBeChecked();
    await fullDay.check();
    await expect(fullDay).toBeChecked();
    await scan(page, ".production-controls");
});

test("a confirmation names its question, focuses the safe choice and returns focus", async ({
    page,
}) => {
    const viewer = { id: 2, email: "viewer@example.test", role: "viewer" };
    await mockApi(page);
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [
                    { id: user.id, email: user.email, role: "admin" },
                    viewer,
                ],
            },
        }),
    );
    await page.route("**/api/tenants/*/invitations/", (route) =>
        route.fulfill({ json: { invitations: [] } }),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    await page
        .getByRole("button", {
            name: `Remove from workspace: ${viewer.email}`,
            exact: true,
        })
        .click();
    const confirmation = page.getByRole("region", {
        name: "Confirm member removal",
    });
    await expect(confirmation).toContainText(viewer.email);
    await expect(
        confirmation.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeFocused();
    await scan(page, ".member-list + section");
    await confirmation
        .getByRole("button", { name: "Cancel", exact: true })
        .click();
    await expect(confirmation).toHaveCount(0);
    await expect(
        page.getByRole("heading", { name: "Members", level: 2 }),
    ).toBeFocused();
});

test("a confirmation moved to another member focuses its safe choice again", async ({
    page,
}) => {
    const first = { id: 2, email: "first@example.test", role: "viewer" };
    const second = { id: 3, email: "second@example.test", role: "viewer" };
    await mockApi(page);
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [
                    { id: user.id, email: user.email, role: "admin" },
                    first,
                    second,
                ],
            },
        }),
    );
    await page.route("**/api/tenants/*/invitations/", (route) =>
        route.fulfill({ json: { invitations: [] } }),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    for (const viewer of [first, second])
        await page
            .getByRole("button", {
                name: `Remove from workspace: ${viewer.email}`,
                exact: true,
            })
            .click();
    const confirmation = page.getByRole("region", {
        name: "Confirm member removal",
    });
    await expect(confirmation).toContainText(second.email);
    await expect(
        confirmation.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeFocused();
});
