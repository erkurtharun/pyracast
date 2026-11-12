import AxeBuilder from "@axe-core/playwright";
import { test, expect } from "./support/test";
import { mockApi, user } from "./support/workspace";

test("requires email verification after registration", async ({ page }) => {
    await mockApi(page);
    const signedIn = false;
    await page.route("**/api/session/", async (route) =>
        route.fulfill({
            json: {
                user: signedIn ? user : null,
                csrf_token: signedIn ? "signed-token" : "anonymous-token",
            },
        }),
    );
    await page.route("**/api/auth/browser/v1/auth/signup", async (route) => {
        expect(route.request().headers()["x-csrftoken"]).toBe(
            "anonymous-token",
        );
        expect(route.request().postDataJSON()).toEqual({
            company_name: "Smart Solar Energy",
            email: "owner@example.test",
            password: "Test-only-cobalt-79!",
        });
        await route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "verify_email", is_pending: true }] },
                meta: { is_authenticated: false },
            },
        });
    });
    await page.goto("/");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await page.getByLabel("Company name").fill("Smart Solar Energy");
    await page.getByLabel("Email").fill("owner@example.test");
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
        page.getByRole("heading", { name: "Verify your email", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).not.toBeVisible();
});

test("registration errors identify the field and move keyboard focus to the summary", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/signup", async (route) =>
        route.fulfill({
            status: 400,
            json: {
                status: 400,
                errors: [
                    {
                        param: "email",
                        message: "This email is already in use.",
                    },
                ],
            },
        }),
    );
    await page.goto("/");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await page.getByLabel("Company name").fill("Solar Company");
    await page.getByLabel("Email").fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Create company account", exact: true })
        .click();
    await expect(page.locator("form").getByRole("alert")).toBeFocused();
    await expect(page.getByLabel("Email", { exact: true })).toHaveAttribute(
        "aria-invalid",
        "true",
    );
    await page
        .getByRole("button", { name: "This email is already in use." })
        .click();
    await expect(page.getByLabel("Email", { exact: true })).toBeFocused();
    await expect(page.getByLabel("Company name")).toHaveValue("Solar Company");
});

test("a non-JSON authentication failure shows a safe error", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/login", (route) =>
        route.fulfill({
            status: 403,
            contentType: "text/html",
            body: "<html>private gateway details</html>",
        }),
    );
    await page.goto("/");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.locator("form").getByRole("alert")).toContainText(
        "Request failed (403). Please try again.",
    );
    await expect(page.locator("form").getByRole("alert")).not.toContainText(
        "private gateway details",
    );
});

test("pending verification requires the code before completing", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "verify_email", is_pending: true }] },
                meta: { is_authenticated: false },
            },
        }),
    );
    let posts = 0;
    await page.route(
        "**/api/auth/browser/v1/auth/email/verify",
        async (route) => {
            posts += 1;
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "anonymous-token",
            );
            expect(route.request().postDataJSON()).toEqual({
                key: "123456",
            });
            await route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    data: { flows: [{ id: "login" }] },
                    meta: { is_authenticated: false },
                },
            });
        },
    );
    await page.goto("/");
    await expect(
        page.getByRole("heading", { name: "Verify your email" }),
    ).toBeVisible();
    expect(posts).toBe(0);
    await page.getByLabel("Email code").fill("123456");
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await expect(
        page.getByText("Email verified. You can sign in now."),
    ).toBeVisible();
    expect(posts).toBe(1);
});

test("requires the second factor before opening the workspace", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    let signedIn = false;
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: signedIn ? user : null,
                csrf_token: signedIn ? "signed-token" : "anonymous-token",
            },
        }),
    );
    await page.route("**/api/auth/browser/v1/auth/login", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "mfa_authenticate", is_pending: true }] },
                meta: { is_authenticated: false },
            },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/auth/2fa/authenticate",
        (route) => {
            expect(route.request().postDataJSON()).toEqual({ code: "123456" });
            signedIn = true;
            return route.fulfill({
                json: { status: 200, meta: { is_authenticated: true } },
            });
        },
    );
    await page.goto("/");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Sign in", exact: true })
        .last()
        .click();
    await expect(
        page.getByRole("heading", { name: "Two-factor authentication" }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).not.toBeVisible();
    await page.getByLabel("Authenticator or recovery code").fill("123456");
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});

test("sign in only follows the permitted admin return path", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/admin/", (route) =>
        route.fulfill({ contentType: "text/html", body: "<h1>Admin</h1>" }),
    );
    await page.goto("/?next=%2Fadmin%2F");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Admin" })).toBeVisible();
});

test("sign in ignores an external return path", async ({ page }) => {
    await mockApi(page, { anonymous: true });
    await page.goto("/?next=https%3A%2F%2Fevil.example%2F");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    expect(new URL(page.url()).hostname).toBe("127.0.0.1");
});

test("a completed password reset returns to sign in", async ({ page }) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/password/request", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: {
                    flows: [{ id: "password_reset_by_code", is_pending: true }],
                },
                meta: { is_authenticated: false },
            },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/auth/password/reset",
        async (route) => {
            expect(route.request().postDataJSON()).toEqual({
                key: "123456",
                password: "Test-new-password-123!",
            });
            await route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    meta: { is_authenticated: false },
                    data: { flows: [{ id: "login" }] },
                },
            });
        },
    );
    await page.goto("/");
    await page.getByRole("button", { name: "Forgot password?" }).click();
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page.getByRole("button", { name: "Send reset code" }).click();
    await expect(
        page.getByRole("heading", { name: "Choose a new password" }),
    ).toBeVisible();
    await page.getByLabel("Email code").fill("123456");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-new-password-123!");
    await page
        .getByRole("button", { name: "Reset password", exact: true })
        .click();
    await expect(
        page.getByText("Password reset. Sign in with your new password."),
    ).toBeVisible();
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
