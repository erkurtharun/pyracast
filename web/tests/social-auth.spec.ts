import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "./support/test";
import { mockApi, user } from "./support/workspace";
import { allauthErrorBody } from "./support/errors";

test("enabled providers submit a CSRF protected native redirect form on mobile", async ({
    page,
    baseURL,
}) => {
    await mockApi(page, { anonymous: true });
    await page.setViewportSize({ width: 390, height: 812 });
    await page.route("**/api/auth/browser/v1/config", (route) =>
        route.fulfill({
            json: {
                status: 200,
                data: {
                    socialaccount: {
                        providers: [
                            { id: "google" },
                            { id: "microsoft" },
                            { id: "unknown" },
                        ],
                    },
                },
            },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/auth/provider/redirect",
        async (route) => {
            expect(route.request().method()).toBe("POST");
            const form = new URLSearchParams(route.request().postData()!);
            expect(Object.fromEntries(form)).toEqual({
                provider: "google",
                process: "login",
                csrfmiddlewaretoken: "anonymous-token",
                callback_url: new URL("/app/?social_callback=1", baseURL).href,
            });
            await route.fulfill({
                contentType: "text/html",
                body: "<h1>Provider sign-in</h1>",
            });
        },
    );
    await page.goto("/app/");
    const google = page.getByRole("button", { name: "Continue with Google" });
    await expect(google).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Sign in with Microsoft" }),
    ).toBeVisible();
    expect((await google.boundingBox())!.width).toBeGreaterThan(250);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await page.screenshot({
        path: test.info().outputPath("login-mobile.png"),
        fullPage: true,
    });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({
        path: test.info().outputPath("login-desktop.png"),
        fullPage: true,
    });
    await google.click();
    await expect(
        page.getByRole("heading", { name: "Provider sign-in" }),
    ).toBeVisible();
});

test("unconfigured providers stay hidden", async ({ page }) => {
    await mockApi(page, { anonymous: true });
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Continue with Google" }),
    ).toHaveCount(0);
});

test("social signup restores invitation and does not request a password", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.addInitScript(() =>
        sessionStorage.setItem("pyracast.workspaceInvitation", "invite-secret"),
    );
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "provider_signup", is_pending: true }] },
                meta: { is_authenticated: false },
            },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/auth/provider/signup",
        async (route) => {
            if (route.request().method() === "GET")
                return route.fulfill({
                    json: {
                        status: 200,
                        data: { email: [{ email: "new@example.test" }] },
                    },
                });
            expect(route.request().postDataJSON()).toEqual({
                email: "new@example.test",
                invitation_token: "invite-secret",
            });
            return route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    data: { flows: [{ id: "verify_email", is_pending: true }] },
                    meta: { is_authenticated: false },
                },
            });
        },
    );
    await page.goto("/app/?social_callback=1");
    await expect(
        page.getByRole("heading", { name: "Complete your account" }),
    ).toBeVisible();
    await expect(page.getByLabel("Email", { exact: true })).toHaveValue(
        "new@example.test",
    );
    await expect(page.getByLabel("Password", { exact: true })).toHaveCount(0);
    await expect(page.getByLabel("Company name")).toHaveCount(0);
    await page
        .getByRole("button", { name: "Complete your account", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Verify your email" }),
    ).toBeVisible();
});

test("social-only accounts confirm email before disconnecting the selected provider", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: { ...user, has_usable_password: false },
                csrf_token: "signed-token",
            },
        }),
    );
    let verified = false;
    let disconnected = false;
    await page.route(
        "**/api/auth/browser/v1/account/providers",
        async (route) => {
            if (route.request().method() === "GET")
                return route.fulfill({
                    json: {
                        status: 200,
                        data: disconnected
                            ? []
                            : [
                                  {
                                      provider: {
                                          id: "google",
                                          name: "Google",
                                      },
                                      uid: "account-42",
                                      display: "solar@example.test",
                                  },
                              ],
                    },
                });
            expect(route.request().postDataJSON()).toEqual({
                provider: "google",
                account: "account-42",
            });
            if (!verified)
                return route.fulfill({
                    status: 401,
                    json: { status: 401, meta: { is_authenticated: true } },
                });
            disconnected = true;
            return route.fulfill({ json: { status: 200, data: [] } });
        },
    );
    await page.route(
        "**/api/auth/browser/v1/auth/reauthenticate/code/request",
        (route) => {
            expect(route.request().postDataJSON()).toEqual({
                email: user.email,
            });
            return route.fulfill({ json: { status: 200 } });
        },
    );
    await page.route(
        "**/api/auth/browser/v1/auth/reauthenticate/code/confirm",
        (route) => {
            expect(route.request().postDataJSON()).toEqual({ code: "123456" });
            verified = true;
            return route.fulfill({ json: { status: 200 } });
        },
    );
    await page.goto("/app/account/");
    await page
        .getByRole("button", { name: "Disconnect solar@example.test" })
        .click();
    await page.screenshot({
        path: test.info().outputPath("linked-reauth-desktop.png"),
        fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
        path: test.info().outputPath("linked-reauth-mobile.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: "Send verification code" }).click();
    await page
        .getByRole("region", { name: "Connected accounts" })
        .getByLabel("Email verification code", { exact: true })
        .fill("123456");
    await page
        .getByRole("button", { name: "Confirm code", exact: true })
        .click();
    await expect(
        page.getByText("Account disconnected.", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Disconnect solar@example.test" }),
    ).toHaveCount(0);
});

for (const hasPassword of [true, false]) {
    test(`accounts with an authenticator ${hasPassword ? "and" : "but no"} password confirm a connection with the authenticator`, async ({
        page,
    }) => {
        await mockApi(page);
        await page.route("**/api/session/", (route) =>
            route.fulfill({
                json: {
                    user: { ...user, has_usable_password: hasPassword },
                    csrf_token: "signed-token",
                },
            }),
        );
        await page.route("**/api/auth/browser/v1/config", (route) =>
            route.fulfill({
                json: {
                    status: 200,
                    data: { socialaccount: { providers: [{ id: "google" }] } },
                },
            }),
        );
        await page.route(
            "**/api/auth/browser/v1/account/authenticators",
            (route) =>
                route.fulfill({
                    json: { status: 200, data: [{ type: "totp" }] },
                }),
        );
        let confirmed = false;
        await page.route(
            "**/api/auth/browser/v1/auth/2fa/reauthenticate",
            (route) => {
                expect(route.request().postDataJSON()).toEqual({
                    code: "123456",
                });
                confirmed = true;
                return route.fulfill({ json: { status: 200 } });
            },
        );
        await page.goto("/app/?account=security");
        const accounts = page.getByRole("region", {
            name: "Connected accounts",
        });
        await accounts
            .getByRole("button", { name: "Connect an account" })
            .click();
        await expect(
            accounts.getByLabel("Current password", { exact: true }),
        ).toHaveCount(0);
        await accounts
            .getByLabel("Authenticator or recovery code", { exact: true })
            .fill("123456");
        await accounts
            .getByRole("button", { name: "Confirm authenticator" })
            .click();
        await expect(
            accounts.getByRole("button", { name: "Connect Google" }),
        ).toBeVisible();
        expect(confirmed).toBe(true);
    });
}

test("provider errors explain how to link an existing email safely", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.goto(
        "/app/?social_callback=1&error=unknown&error_process=login",
    );
    await expect(page.locator(".notice.error")).toContainText(
        "sign in with your existing method",
    );
    await expect(page).not.toHaveURL(/error=/);
});

test("a social-only user can set the first password and follows the required sign-in", async ({
    page,
}) => {
    await mockApi(page);
    let saved = false;
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: saved ? null : { ...user, has_usable_password: false },
                csrf_token: saved ? "anonymous-token" : "signed-token",
            },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/account/password/change",
        (route) => {
            expect(route.request().postDataJSON()).toEqual({
                new_password: "Test-only-cobalt-79!",
            });
            saved = true;
            return route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    data: { flows: [{ id: "login" }] },
                    meta: { is_authenticated: false, password_changed: true },
                },
            });
        },
    );
    await page.goto("/app/account/");
    await page
        .getByRole("heading", { name: "Set a password", exact: true })
        .click();
    await expect(
        page.getByLabel("Current password", { exact: true }),
    ).toHaveCount(0);
    await page
        .getByLabel("New password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Set a password", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(
        page.getByText("Password saved. Sign in with your new password."),
    ).toBeVisible();
});

test("social-only MFA setup requests email confirmation rather than an unavailable password", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: { ...user, has_usable_password: false },
                csrf_token: "signed-token",
            },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/account/authenticators/totp",
        (route) =>
            route.fulfill({
                status: 401,
                json: { status: 401, meta: { is_authenticated: true } },
            }),
    );
    await page.goto("/app/account/");
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await expect(
        page.getByRole("heading", { name: "Confirm with email" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Send verification code" }),
    ).toBeVisible();
    await expect(
        page.getByLabel("Current password", { exact: true }),
    ).toHaveCount(0);
});

test("expired initial-password sessions never report that the password was saved", async ({
    page,
}) => {
    await mockApi(page);
    let expired = false;
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: expired ? null : { ...user, has_usable_password: false },
                csrf_token: "token",
            },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/account/password/change",
        (route) => {
            expired = true;
            return route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    data: { flows: [{ id: "login" }] },
                    meta: { is_authenticated: false },
                },
            });
        },
    );
    await page.goto("/app/account/");
    await page
        .getByRole("heading", { name: "Set a password", exact: true })
        .click();
    await page
        .getByLabel("New password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Set a password", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(
        page.getByText("Password saved. Sign in with your new password."),
    ).toHaveCount(0);
});

test("Turkish provider buttons fit a 320px screen", async ({
    page,
    context,
    baseURL,
}) => {
    await context.addCookies([
        {
            name: "pyracast_language",
            value: "tr",
            url: baseURL!,
        },
    ]);
    await mockApi(page, { anonymous: true });
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { user: null, csrf_token: "token", language: "tr" },
        }),
    );
    await page.route("**/api/auth/browser/v1/config", (route) =>
        route.fulfill({
            json: {
                status: 200,
                data: {
                    socialaccount: {
                        providers: [{ id: "google" }, { id: "microsoft" }],
                    },
                },
            },
        }),
    );
    await page.setViewportSize({ width: 320, height: 812 });
    await page.goto("/app/");
    await expect(
        page.getByRole("button", { name: "Google ile devam et" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Microsoft ile oturum a\u00e7" }),
    ).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await page.screenshot({
        path: test.info().outputPath("login-turkish-320.png"),
        fullPage: true,
    });
});

test("a rejected social signup email keeps an explicit sign-in and link path", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "provider_signup", is_pending: true }] },
                meta: { is_authenticated: false },
            },
        }),
    );
    await page.route("**/api/auth/browser/v1/auth/provider/signup", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  json: {
                      status: 200,
                      data: { email: [{ email: user.email }] },
                  },
              })
            : route.fulfill({
                  status: 400,
                  json: allauthErrorBody(400, "invalid_request", [
                      {
                          param: "email",
                          code: "email_taken",
                          message: "This email is already in use.",
                      },
                  ]),
              }),
    );
    await page.goto("/app/?social_callback=1");
    await page.getByLabel("Company name").fill("Sunfield Energy");
    await page
        .getByRole("button", { name: "Complete your account", exact: true })
        .click();
    await expect(page.locator("form").getByRole("alert")).toContainText(
        "This email is already in use.",
    );
    await expect(
        page.getByText(/sign in first and connect this provider/),
    ).toBeVisible();
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
});

test("connecting returns to account settings and consumes an expired-confirmation error once", async ({
    page,
}) => {
    await mockApi(page);
    await page.addInitScript(() => {
        if (
            location.pathname === "/app/" &&
            location.search.includes("social_callback")
        )
            sessionStorage.setItem("pyracast.socialConnect", "1");
    });
    await page.goto(
        "/app/?social_callback=1&error=reauthentication_required&error_process=connect",
    );
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
    await expect(
        page
            .getByRole("region", { name: "Connected accounts" })
            .getByRole("alert"),
    ).toContainText("Try again");
    await expect(page).not.toHaveURL(/error=/);
    await page.reload();
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("region", { name: "Connected accounts" }),
    ).toHaveCount(0);
});
