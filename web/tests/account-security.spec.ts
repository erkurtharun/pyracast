import { expect, test } from "@playwright/test";
import { mockApi } from "./support/workspace";

test("account security enables TOTP and displays recovery codes only on request", async ({
    page,
}) => {
    await mockApi(page);
    await page.route(
        "**/api/auth/browser/v1/account/authenticators/totp",
        async (route) => {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            if (route.request().method() === "GET") {
                await route.fulfill({
                    status: 404,
                    json: { status: 404, meta: { secret: "TESTSECRET" } },
                });
            } else {
                expect(route.request().postDataJSON()).toEqual({
                    code: "123456",
                });
                await route.fulfill({ json: { status: 200 } });
            }
        },
    );
    await page.route(
        "**/api/auth/browser/v1/account/authenticators/recovery-codes",
        (route) =>
            route.fulfill({
                json: {
                    status: 200,
                    data: { unused_codes: ["code-one", "code-two"] },
                },
            }),
    );
    await page.goto("/");
    await page.getByRole("button", { name: "Account settings" }).click();
    await expect(page.getByText("code-one")).not.toBeVisible();
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await expect(page.getByText("TESTSECRET")).toBeVisible();
    await page.getByLabel("Authenticator code", { exact: true }).fill("123456");
    await page.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(page.getByText("TESTSECRET")).not.toBeVisible();
    await page.getByRole("button", { name: "Show recovery codes" }).click();
    await expect(page.getByText("code-one")).toBeVisible();
});

test("an expired account session returns to sign in instead of asking for reauthentication", async ({
    page,
}) => {
    await mockApi(page);
    let expired = false;
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: expired
                    ? null
                    : { id: 1, username: "user", email: "user@example.test" },
                csrf_token: "token",
            },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/account/authenticators/totp",
        (route) => {
            expired = true;
            return route.fulfill({
                status: 401,
                json: { status: 401, meta: { is_authenticated: false } },
            });
        },
    );
    await page.goto("/");
    await page.getByRole("button", { name: "Account settings" }).click();
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
});

test("email confirmation requiring reauthentication does not report success", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                meta: { is_authenticated: true },
                data: { flows: [{ id: "reauthenticate", is_pending: true }] },
            },
        }),
    );
    await page.goto("/");
    await page.getByRole("button", { name: "Account settings" }).click();
    await page.getByLabel("Email verification code").fill("ABCD-EFGH");
    await page.getByRole("button", { name: "Verify new email" }).click();
    await expect(
        page.getByText("Please confirm your identity and try again."),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Account settings" }),
    ).toBeVisible();
});

test("administrator recovery codes request a fresh authenticator confirmation", async ({
    page,
}) => {
    await mockApi(page);
    await page.route(
        "**/api/auth/browser/v1/account/authenticators/recovery-codes",
        (route) =>
            route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    meta: { is_authenticated: true },
                    errors: [
                        {
                            code: "mfa_reauthentication_required",
                            message:
                                "Verify an authenticator before managing administrator recovery codes.",
                        },
                    ],
                },
            }),
    );
    await page.goto("/");
    await page.getByRole("button", { name: "Account settings" }).click();
    await page.getByRole("button", { name: "Show recovery codes" }).click();
    await expect(
        page.getByText(
            "Verify an authenticator before managing administrator recovery codes.",
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Confirm authenticator" }),
    ).toBeVisible();
});
