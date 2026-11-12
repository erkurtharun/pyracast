import AxeBuilder from "@axe-core/playwright";
import { test, expect } from "./support/test";
import { mockApi, user } from "./support/workspace";

test("creates a company account with CSRF and opens its plant workspace", async ({
    page,
}) => {
    await mockApi(page);
    let signedIn = false;
    await page.route("**/api/session/", async (route) =>
        route.fulfill({
            json: {
                user: signedIn ? user : null,
                csrf_token: signedIn ? "signed-token" : "anonymous-token",
            },
        }),
    );
    await page.route("**/api/session/register/", async (route) => {
        expect(route.request().headers()["x-csrftoken"]).toBe(
            "anonymous-token",
        );
        expect(route.request().postDataJSON()).toEqual({
            company_name: "Smart Solar Energy",
            username: "solar.owner",
            email: "owner@example.test",
            password: "Test-only-cobalt-79!",
        });
        signedIn = true;
        await route.fulfill({
            status: 201,
            json: { user, csrf_token: "signed-token" },
        });
    });
    await page.goto("/");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await page.getByLabel("Company name").fill("Smart Solar Energy");
    await page.getByLabel("Username", { exact: true }).fill("solar.owner");
    await page.getByLabel("Work email").fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await expect(page.getByLabel("Password", { exact: true })).toHaveAttribute(
        "autocomplete",
        "new-password",
    );
    await page
        .getByRole("button", { name: "Show password", exact: true })
        .click();
    await expect(page.getByLabel("Password", { exact: true })).toHaveAttribute(
        "type",
        "text",
    );
    await page
        .getByRole("button", { name: "Hide password", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Create company account", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});

test("registration errors identify the field and move keyboard focus to the summary", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/session/register/", async (route) =>
        route.fulfill({
            status: 400,
            json: {
                code: "http_400",
                detail: { username: ["This username is already in use."] },
            },
        }),
    );
    await page.goto("/");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await page.getByLabel("Company name").fill("Solar Company");
    await page.getByLabel("Username", { exact: true }).fill("taken");
    await page.getByLabel("Work email").fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Create company account", exact: true })
        .click();
    await expect(page.locator("form").getByRole("alert")).toBeFocused();
    await expect(page.getByLabel("Username", { exact: true })).toHaveAttribute(
        "aria-invalid",
        "true",
    );
    await page
        .getByRole("button", { name: "This username is already in use." })
        .click();
    await expect(page.getByLabel("Username", { exact: true })).toBeFocused();
    await expect(page.getByLabel("Company name")).toHaveValue("Solar Company");
});

test("account access reflows on a narrow screen without accessibility violations", async ({
    page,
}) => {
    await page.setViewportSize({ width: 320, height: 740 });
    await mockApi(page, { anonymous: true });
    await page.goto("/");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Create your company account" }),
    ).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});
