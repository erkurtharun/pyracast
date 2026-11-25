import { expect, test, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import {
    accountWith,
    firstAccount,
    mockApi,
    sessionIdentity,
    user,
} from "./support/workspace";
import { allauthErrorBody, errorBody } from "./support/errors";

const socialOnly = accountWith({ has_usable_password: false });

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
    const google = page.getByRole("button", { name: "Sign in with Google" });
    await expect(google).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Sign in with Microsoft" }),
    ).toBeVisible();
    expect((await google.boundingBox())!.width).toBeGreaterThan(250);
    expect(await accessibilityViolations(page)).toEqual([]);
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
        page.getByRole("button", { name: "Sign in with Google" }),
    ).toHaveCount(0);
});

for (const [language, tab, signIn, signUp] of [
    [
        "en",
        "Create account",
        ["Sign in with Google", "Sign in with Microsoft"],
        ["Sign up with Google", "Sign up with Microsoft"],
    ],
    [
        "tr",
        "Hesap oluştur",
        ["Google ile giriş yap", "Microsoft ile giriş yap"],
        ["Google ile kaydol", "Microsoft ile kaydol"],
    ],
] as const) {
    test(`the registration screen offers sign-up and the sign-in screen sign-in with each provider in ${language}`, async ({
        page,
    }) => {
        const account = await mockApi(page, { anonymous: true });
        await page.route("**/api/session/", (route) =>
            route.fulfill({
                json: { ...sessionIdentity(account.current), language },
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
        await page.goto("/app/");
        const providers = page.locator(".social-provider");
        await expect(providers).toHaveText([...signIn]);
        await page.getByRole("button", { name: tab, exact: true }).click();
        await expect(providers).toHaveText([...signUp]);
        expect(
            await page
                .locator('.social-providers input[name="process"]')
                .evaluateAll((inputs) =>
                    inputs.map((input) => (input as HTMLInputElement).value),
                ),
        ).toEqual(["login", "login"]);
    });
}

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
    await page.route("**/api/auth/browser/v1/auth/provider/complete", (route) =>
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
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Verify your email" }),
    ).toBeVisible();
});

test("a provider return whose sign-in screen has gone asks for no sign-in state", async ({
    context,
    page,
}) => {
    const account = await mockApi(page, { anonymous: true });
    const reached = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    await page.route(
        "**/api/auth/browser/v1/auth/provider/complete",
        async (route) => {
            reached.resolve();
            await release.promise;
            await route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    data: {
                        flows: [{ id: "provider_signup", is_pending: true }],
                    },
                    meta: { is_authenticated: false },
                },
            });
        },
    );
    const reads: string[] = [];
    page.on("request", (request) => {
        if (
            request.method() === "GET" &&
            new URL(request.url()).pathname ===
                "/api/auth/browser/v1/auth/session"
        )
            reads.push(request.url());
    });
    await page.goto("/app/?social_callback=1");
    await reached.promise;
    // Another tab observes a signed-in session: this tab hides the sign-in screen until its read,
    // queued behind the completion, adopts that session.
    account.current = { ...firstAccount, tag: "tag-b" };
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                ...sessionIdentity(account.current),
                language: "en",
                session_tag: account.current?.tag,
            },
        }),
    );
    const other = await context.newPage();
    await other.route("**/blank", (route) =>
        route.fulfill({ contentType: "text/html", body: "<html></html>" }),
    );
    await other.goto("/blank");
    await other.evaluate(() =>
        window.localStorage.setItem("pyracast.sessionTag", "tag-b"),
    );
    await expect(
        page.getByRole("progressbar", { name: "Loading…" }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toHaveCount(0);
    release.resolve();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await page.evaluate(
        () =>
            new Promise<void>((resolve) =>
                requestAnimationFrame(() =>
                    requestAnimationFrame(() => resolve()),
                ),
            ),
    );
    expect(reads).toEqual([]);
});

test("social-only accounts confirm email before disconnecting the selected provider", async ({
    page,
}) => {
    await mockApi(page, { account: { current: socialOnly } });
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
                                      can_disconnect: true,
                                      sign_in_available: true,
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
                    json: {
                        status: 401,
                        meta: { is_authenticated: true },
                        data: { flows: [{ id: "reauthenticate_by_code" }] },
                    },
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
    const accounts = page.getByRole("region", { name: "Connected accounts" });
    await expect(
        accounts.getByText(`Pyracast sends the email code to ${user.email}.`, {
            exact: true,
        }),
    ).toHaveCount(1);
    await page.getByRole("button", { name: "Send email code" }).click();
    await expect(
        accounts.getByText("Check your email for the code.", { exact: true }),
    ).toHaveCount(1);
    await expect(
        accounts.getByText(/Pyracast sends the email code/),
    ).toHaveCount(0);
    await accounts.getByLabel("Email code", { exact: true }).fill("123456");
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
        await mockApi(page, {
            account: {
                current: accountWith({ has_usable_password: hasPassword }),
            },
        });
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
        await page.goto("/app/account/");
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
            .getByLabel("Authenticator code or recovery code", { exact: true })
            .fill("123456");
        await accounts.getByRole("button", { name: "Confirm code" }).click();
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
        "sign in another way",
    );
    await expect(page).not.toHaveURL(/error=/);
});

test("a social-only user can set the first password and follows the required sign-in", async ({
    page,
}) => {
    const account = await mockApi(page, { account: { current: socialOnly } });
    await page.route(
        "**/api/auth/browser/v1/account/password/change",
        (route) => {
            expect(route.request().postDataJSON()).toEqual({
                new_password: "Test-only-cobalt-79!",
            });
            expect(account.current?.user.has_usable_password).toBe(false);
            account.current = null;
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
    await mockApi(page, { account: { current: socialOnly } });
    await page.route(
        "**/api/auth/browser/v1/account/authenticators/totp",
        (route) =>
            route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    meta: { is_authenticated: true },
                    data: { flows: [{ id: "reauthenticate_by_code" }] },
                },
            }),
    );
    await page.goto("/app/account/");
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await expect(
        page.getByRole("heading", { name: "Confirm with email" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Send email code" }),
    ).toBeVisible();
    await expect(
        page.getByLabel("Current password", { exact: true }),
    ).toHaveCount(0);
});

test("expired initial-password sessions never report that the password was saved", async ({
    page,
}) => {
    const account = await mockApi(page, { account: { current: socialOnly } });
    await page.route(
        "**/api/auth/browser/v1/account/password/change",
        (route) => {
            expect(account.current?.user.has_usable_password).toBe(false);
            account.current = null;
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
    const account = await mockApi(page, { anonymous: true });
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { ...sessionIdentity(account.current), language: "tr" },
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
        page.getByRole("button", { name: "Google ile giriş yap" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Microsoft ile giriş yap" }),
    ).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    expect(await accessibilityViolations(page)).toEqual([]);
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
    await page.route("**/api/auth/browser/v1/auth/provider/complete", (route) =>
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
        .getByRole("button", { name: "Create workspace", exact: true })
        .click();
    await expect(page.locator("form").getByRole("alert")).toContainText(
        "This email is already in use.",
    );
    await expect(
        page.getByText(/sign in and connect this one under Account/),
    ).toBeVisible();
    await page
        .getByRole("button", {
            name: "Use another sign-in method",
            exact: true,
        })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
});

test("connecting returns to account settings and consumes an expired-confirmation error once", async ({
    page,
}) => {
    await mockApi(page);
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

test("a provider sign-in completes on return and opens the workspace", async ({
    page,
}) => {
    const account = await mockApi(page, { anonymous: true });
    await page.route(
        "**/api/auth/browser/v1/auth/provider/complete",
        (route) => {
            expect(route.request().method()).toBe("POST");
            account.current = firstAccount;
            return route.fulfill({
                json: { status: 200, meta: { is_authenticated: true } },
            });
        },
    );
    await page.goto("/app/?social_callback=1");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    expect(account.current?.user).toBe(user);
    await expect(page).not.toHaveURL(/social_callback/);
});

test("a provider sign-in that needs the second factor asks for it", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    const pending = {
        status: 401,
        json: {
            status: 401,
            data: { flows: [{ id: "mfa_authenticate", is_pending: true }] },
            meta: { is_authenticated: false },
        },
    };
    await page.route("**/api/auth/browser/v1/auth/provider/complete", (route) =>
        route.fulfill(pending),
    );
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.fulfill(pending),
    );
    await page.goto("/app/?social_callback=1");
    await expect(
        page.getByRole("heading", { name: "Two-step verification" }),
    ).toBeVisible();
    await expect(page.locator(".notice.error")).toHaveCount(0);
});

const connectedAccount = {
    provider: { id: "google", name: "Google" },
    uid: "account-42",
    display: "solar@example.test",
    can_disconnect: true,
    sign_in_available: true,
};

async function connectingAccount(
    page: Parameters<typeof mockApi>[0],
    completion: { status: number; json: unknown },
) {
    await mockApi(page);
    await page.route("**/api/auth/browser/v1/config", (route) =>
        route.fulfill({
            json: {
                status: 200,
                data: { socialaccount: { providers: [{ id: "google" }] } },
            },
        }),
    );
    let connected = false;
    await page.route("**/api/auth/browser/v1/account/providers", (route) =>
        route.fulfill({
            json: { status: 200, data: connected ? [connectedAccount] : [] },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/auth/provider/complete",
        (route) => {
            connected = completion.status === 200;
            return route.fulfill(completion);
        },
    );
    await page.goto("/app/?social_callback=1");
    return page.getByRole("region", { name: "Connected accounts" });
}

test("a provider connection completes on return to account settings", async ({
    page,
}) => {
    const accounts = await connectingAccount(page, {
        status: 200,
        json: { status: 200, data: [connectedAccount] },
    });
    await expect(
        accounts.getByText("Account connected.", { exact: true }),
    ).toBeVisible();
    await expect(
        accounts.getByRole("button", { name: "Disconnect solar@example.test" }),
    ).toBeVisible();
    await expect(page).not.toHaveURL(/social_callback/);
});

test("a provider connection that needs a confirmation asks for the factor it names", async ({
    page,
}) => {
    const accounts = await connectingAccount(page, {
        status: 401,
        json: {
            status: 401,
            data: { flows: [{ id: "mfa_reauthenticate" }] },
            meta: { is_authenticated: true },
            errors: [
                {
                    code: "mfa_reauthentication_required",
                    message: "Confirm with your authenticator to continue.",
                },
            ],
        },
    });
    await expect(
        accounts.getByLabel("Authenticator code or recovery code", {
            exact: true,
        }),
    ).toBeVisible();
    await expect(
        accounts.getByLabel("Current password", { exact: true }),
    ).toHaveCount(0);
});

test("a provider connection to another account's identity says so", async ({
    page,
}) => {
    const message =
        "The social account is already connected to a different account.";
    const accounts = await connectingAccount(page, {
        status: 400,
        json: allauthErrorBody(400, "invalid_request", [
            { code: "connected_other", message },
        ]),
    });
    await expect(accounts.getByRole("alert")).toContainText(message);
});

for (const [name, refusal] of [
    ["finds no stored login", { status: 409, json: { status: 409 } }],
    [
        "leaves no pending step",
        {
            status: 401,
            json: {
                status: 401,
                data: { flows: [] },
                meta: { is_authenticated: false },
            },
        },
    ],
] as const) {
    test(`a provider sign-in whose completion ${name} shows the callback error`, async ({
        page,
    }) => {
        await mockApi(page, { anonymous: true });
        await page.route(
            "**/api/auth/browser/v1/auth/provider/complete",
            (route) => route.fulfill(refusal),
        );
        await page.goto("/app/?social_callback=1");
        await expect(page.locator(".notice.error")).toContainText(
            "sign in another way",
        );
        await expect(page).not.toHaveURL(/social_callback/);
    });
}

test("a refused provider connection shows the callback error", async ({
    page,
}) => {
    const accounts = await connectingAccount(page, {
        status: 403,
        json: { status: 403 },
    });
    await expect(accounts.getByRole("alert")).toHaveText(
        "This account could not be connected. Try again.",
    );
    await expect(
        accounts.getByText("Account connected.", { exact: true }),
    ).toHaveCount(0);
});

test("a connected account says when it cannot be disconnected or used to sign in", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/auth/browser/v1/account/providers", (route) =>
        route.fulfill({
            json: {
                status: 200,
                data: [
                    {
                        ...connectedAccount,
                        can_disconnect: false,
                        sign_in_available: false,
                    },
                ],
            },
        }),
    );
    await page.goto("/app/account/");
    const accounts = page.getByRole("region", { name: "Connected accounts" });
    await expect(
        accounts.getByRole("button", { name: "Disconnect solar@example.test" }),
    ).toBeDisabled();
    await expect(
        accounts.getByText("Cannot be used to sign in", { exact: true }),
    ).toBeVisible();
    await expect(
        accounts.getByText(
            "To disconnect it, set a password or connect another account first.",
            { exact: true },
        ),
    ).toBeVisible();
    await expect(accounts.getByText(/Your only way to sign in/)).toHaveCount(0);
});

test("a disconnection answered with a refusal that is not allauth's shows an error and keeps the account", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/auth/browser/v1/account/providers", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  json: { status: 200, data: [connectedAccount] },
              })
            : route.fulfill({ status: 401, json: { detail: "Unauthorized" } }),
    );
    await page.goto("/app/account/");
    const accounts = page.getByRole("region", { name: "Connected accounts" });
    await accounts
        .getByRole("button", { name: "Disconnect solar@example.test" })
        .click();
    await expect(accounts.getByRole("alert")).toContainText(
        "Request failed (401). Please try again.",
    );
    await expect(
        accounts.getByText("Account disconnected.", { exact: true }),
    ).toHaveCount(0);
    await expect(
        accounts.getByRole("button", { name: "Disconnect solar@example.test" }),
    ).toBeVisible();
});

const datedForecast = "/app/w/1/plants/11/forecast/?date=2026-11-25";
const returnKey = "pyracast.providerReturn";

async function offerGoogleSignIn(page: Page) {
    await page.clock.setFixedTime(new Date("2026-11-19T12:00:00Z"));
    const account = await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/config", (route) =>
        route.fulfill({
            json: {
                status: 200,
                data: { socialaccount: { providers: [{ id: "google" }] } },
            },
        }),
    );
    await page.route("**/api/auth/browser/v1/auth/provider/redirect", (route) =>
        route.fulfill({
            status: 302,
            headers: { location: "/app/?social_callback=1" },
        }),
    );
    await page.route(
        "**/api/auth/browser/v1/auth/provider/complete",
        (route) => {
            account.current = firstAccount;
            return route.fulfill({
                json: { status: 200, meta: { is_authenticated: true } },
            });
        },
    );
    return account;
}

const continueWithGoogle = (page: Page) =>
    page.getByRole("button", { name: "Sign in with Google" }).click();

async function keepReturn(page: Page, address: string) {
    await page.goto("/app/");
    await expect(
        page.getByRole("button", { name: "Sign in with Google" }),
    ).toBeVisible();
    await page.evaluate(
        ({ key, value }) => window.sessionStorage.setItem(key, value),
        { key: returnKey, value: address },
    );
}

const storedReturn = (page: Page) =>
    page.evaluate((key) => window.sessionStorage.getItem(key), returnKey);

function sentNavigations(page: Page) {
    const paths: string[] = [];
    page.on("request", (request) => {
        const { pathname } = new URL(request.url());
        if (request.isNavigationRequest()) paths.push(`document ${pathname}`);
        else if (request.headers().rsc === "1") paths.push(`RSC ${pathname}`);
    });
    return paths;
}

const portfolio = (page: Page) =>
    page.getByRole("heading", { name: "Plants", exact: true });

test("a provider sign-in started at a dated forecast opens that forecast through one navigation", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    await page.goto(datedForecast);
    const navigations = sentNavigations(page);
    await continueWithGoogle(page);
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-25");
    await expect(page).toHaveURL(datedForecast);
    expect(navigations).toEqual([
        "document /api/auth/browser/v1/auth/provider/redirect",
        "document /app/",
        "document /app/w/1/plants/11/forecast/",
    ]);
    expect(await storedReturn(page)).toBeNull();
});

test("a provider sign-in with an invitation kept in the tab opens the invitation", async ({
    page,
}) => {
    await page.addInitScript(() =>
        window.sessionStorage.setItem(
            "pyracast.workspaceInvitation",
            "invite-secret",
        ),
    );
    await offerGoogleSignIn(page);
    await page.goto(datedForecast);
    await continueWithGoogle(page);
    await expect(
        page.getByRole("heading", { name: "Join workspace" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/invitation/");
});

test("a provider sign-in started at a workspace the reader does not belong to opens that address", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    await page.goto("/app/w/7/plants/11/forecast/?date=2026-11-25");
    await continueWithGoogle(page);
    await expect(
        page.getByText("This workspace is not available to your account."),
    ).toBeVisible();
    await expect(page).toHaveURL(
        "/app/w/7/plants/11/forecast/?date=2026-11-25",
    );
});

test("a provider sign-in that needs the second factor opens the forecast it started at", async ({
    page,
}) => {
    const account = await offerGoogleSignIn(page);
    const pending = {
        status: 401,
        json: {
            status: 401,
            data: { flows: [{ id: "mfa_authenticate", is_pending: true }] },
            meta: { is_authenticated: false },
        },
    };
    let returned = false;
    await page.route(
        "**/api/auth/browser/v1/auth/provider/complete",
        (route) => {
            returned = true;
            return route.fulfill(pending);
        },
    );
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.request().method() === "GET" && returned && !account.current
            ? route.fulfill(pending)
            : route.fallback(),
    );
    await page.route(
        "**/api/auth/browser/v1/auth/2fa/authenticate",
        (route) => {
            account.current = firstAccount;
            return route.fulfill({
                json: { status: 200, meta: { is_authenticated: true } },
            });
        },
    );
    await page.goto(datedForecast);
    await continueWithGoogle(page);
    await page.getByLabel("Authenticator code or recovery code").fill("123456");
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-25");
    await expect(page).toHaveURL(datedForecast);
});

test("a provider sign-in started at the start address opens the workspace over an address kept earlier", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    await keepReturn(page, datedForecast);
    await continueWithGoogle(page);
    await expect(portfolio(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
    expect(await storedReturn(page)).toBeNull();
});

test("a second provider return in the same tab opens the workspace", async ({
    page,
}) => {
    const account = await offerGoogleSignIn(page);
    await page.goto(datedForecast);
    await continueWithGoogle(page);
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-25");
    account.current = null;
    await page.goto("/app/?social_callback=1");
    await expect(portfolio(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
});

for (const [name, address] of [
    ["on another origin", "https://elsewhere.example/app/w/1/"],
    ["without a scheme", "//elsewhere.example/app/w/1/"],
    ["in the console", "/admin/"],
    ["leaving the workspaces", "/app/w/../admin/"],
    ["with a backslash", "/app/w/1/\\plants/"],
    ["over 2,000 characters", `/app/w/1/?view=${"a".repeat(2_000)}`],
    ["without the leading slash", datedForecast.slice(1)],
    ["through a parent segment", `/app/w/..${datedForecast.slice(4)}`],
    [
        "through an encoded parent segment",
        `/app/w/%2e%2e${datedForecast.slice(4)}`,
    ],
    ["that reaches the workspaces from outside", `/outside/..${datedForecast}`],
    [
        "over 2,000 characters once encoded",
        `${datedForecast}&view=${"é".repeat(2_000 - datedForecast.length - 6)}`,
    ],
] as const) {
    test(`a provider return ignores and removes a kept address ${name}`, async ({
        page,
    }) => {
        await offerGoogleSignIn(page);
        await keepReturn(page, address);
        await page.goto("/app/?social_callback=1");
        await expect(portfolio(page)).toBeVisible();
        await expect(page).toHaveURL("/app/w/1/");
        expect(await storedReturn(page)).toBeNull();
    });
}

test("a failed provider return removes the kept address and shows the callback error", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    await keepReturn(page, datedForecast);
    await page.goto(
        "/app/?social_callback=1&error=unknown&error_process=login",
    );
    await expect(page.locator(".notice.error")).toContainText(
        "sign in another way",
    );
    expect(await storedReturn(page)).toBeNull();
});

test("a provider sign-in without session storage opens the workspace", async ({
    page,
}) => {
    await page.addInitScript(() =>
        Object.defineProperty(window, "sessionStorage", {
            get() {
                throw new DOMException("Storage is blocked.", "SecurityError");
            },
        }),
    );
    await offerGoogleSignIn(page);
    await page.goto(datedForecast);
    await continueWithGoogle(page);
    await expect(portfolio(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
});

test("a provider sign-in whose address cannot be written opens the workspace over an address kept earlier", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    await keepReturn(page, "/app/w/1/plants/11/forecast/?date=2026-11-20");
    await page.goto(datedForecast);
    await page.evaluate(() => {
        Storage.prototype.setItem = () => {
            throw new DOMException("Storage is full.", "QuotaExceededError");
        };
    });
    await continueWithGoogle(page);
    await expect(portfolio(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
    expect(await storedReturn(page)).toBeNull();
});

test("a provider return shows the launch card while its sign-in completes", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    const reached = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    await page.route(
        "**/api/auth/browser/v1/auth/provider/complete",
        async (route) => {
            reached.resolve();
            await release.promise;
            await route.fallback();
        },
    );
    await page.goto(datedForecast);
    await continueWithGoogle(page);
    await reached.promise;
    await expect(
        page.getByRole("progressbar", { name: "Loading…" }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Sign in with Google" }),
    ).toHaveCount(0);
    release.resolve();
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-25");
});

test("a provider sign-in whose session read fails stays at the start address with the callback error", async ({
    page,
}) => {
    const account = await offerGoogleSignIn(page);
    const reached = Promise.withResolvers<string | null>();
    const release = Promise.withResolvers<void>();
    await page.route("**/api/session/", async (route) => {
        if (!account.current) return route.fallback();
        reached.resolve(await route.request().headerValue("referer"));
        await release.promise;
        return route.fulfill({
            status: 503,
            json: errorBody(
                "busy",
                "Forecast service is temporarily unavailable.",
            ),
        });
    });
    await page.goto(datedForecast);
    await continueWithGoogle(page);
    const referrer = new URL((await reached.promise) ?? "");
    expect(`${referrer.pathname}${referrer.search}`).toBe("/app/");
    await expect(page).toHaveURL("/app/");
    release.resolve();
    await expect(page.locator(".notice.error")).toContainText(
        "sign in another way",
    );
    await expect(page).toHaveURL("/app/");
});

test("a provider sign-in that ends at email verification leaves a later password sign-in on the workspace", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    const pending = (id: string) => ({
        status: 401,
        json: {
            status: 401,
            data: { flows: [{ id, is_pending: true }] },
            meta: { is_authenticated: false },
        },
    });
    let stage: string | undefined;
    await page.route(
        "**/api/auth/browser/v1/auth/provider/complete",
        (route) => {
            stage = "provider_signup";
            return route.fulfill(pending(stage));
        },
    );
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.request().method() === "GET" && stage
            ? route.fulfill(pending(stage))
            : route.fallback(),
    );
    await page.route("**/api/auth/browser/v1/auth/provider/signup", (route) => {
        if (route.request().method() === "GET")
            return route.fulfill({
                json: {
                    status: 200,
                    data: { email: [{ email: "new@example.test" }] },
                },
            });
        stage = "verify_email";
        return route.fulfill(pending(stage));
    });
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) => {
        stage = undefined;
        return route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "login" }] },
                meta: { is_authenticated: false },
            },
        });
    });
    await page.goto(datedForecast);
    await continueWithGoogle(page);
    await expect(page.getByLabel("Email", { exact: true })).toHaveValue(
        "new@example.test",
    );
    await page.getByLabel("Company name").fill("Sunfield Energy");
    await page
        .getByRole("button", { name: "Create workspace", exact: true })
        .click();
    await page.getByLabel("Email code").fill("123456");
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await expect(
        page.getByText("Email verified. You can sign in now."),
    ).toBeVisible();
    await page
        .getByLabel("Email", { exact: true })
        .fill(firstAccount.user.email);
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(portfolio(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
});

async function holdDocumentNavigations(page: Page, path: RegExp) {
    const requested: string[] = [];
    await page.route(path, (route) => {
        if (!route.request().isNavigationRequest()) return route.fallback();
        requested.push(new URL(route.request().url()).pathname);
        return route.abort("aborted");
    });
    return requested;
}

const startAddress = (page: Page) =>
    page.getByRole("button", { name: "Sign in with Google" });

test("starting a provider sign-in keeps the workspace address of the tab", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    await page.goto(datedForecast);
    const sent = await holdDocumentNavigations(
        page,
        /\/auth\/provider\/redirect$/,
    );
    await startAddress(page).click();
    await expect.poll(() => storedReturn(page)).toBe(datedForecast);
    expect(sent).toEqual(["/api/auth/browser/v1/auth/provider/redirect"]);
});

test("starting a provider sign-in outside the workspaces removes a kept address", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    await keepReturn(page, datedForecast);
    await holdDocumentNavigations(page, /\/auth\/provider\/redirect$/);
    await startAddress(page).click();
    await expect.poll(() => storedReturn(page)).toBeNull();
});

test("starting a provider sign-in without session storage still sends the request", async ({
    page,
}) => {
    await page.addInitScript(() =>
        Object.defineProperty(window, "sessionStorage", {
            get() {
                throw new DOMException("Storage is blocked.", "SecurityError");
            },
        }),
    );
    await offerGoogleSignIn(page);
    await page.goto(datedForecast);
    const sent = await holdDocumentNavigations(
        page,
        /\/auth\/provider\/redirect$/,
    );
    await startAddress(page).click();
    await expect
        .poll(() => sent)
        .toEqual(["/api/auth/browser/v1/auth/provider/redirect"]);
});

test("a failed provider return without session storage shows the callback error", async ({
    page,
}) => {
    await page.addInitScript(() =>
        Object.defineProperty(window, "sessionStorage", {
            get() {
                throw new DOMException("Storage is blocked.", "SecurityError");
            },
        }),
    );
    await offerGoogleSignIn(page);
    await page.goto(
        "/app/?social_callback=1&error=unknown&error_process=login",
    );
    await expect(page.locator(".notice.error")).toContainText(
        "sign in another way",
    );
});

test("a provider return replaces its address with the one the sign-in started at", async ({
    page,
}) => {
    await offerGoogleSignIn(page);
    await page.goto(datedForecast);
    const sent = await holdDocumentNavigations(
        page,
        /\/app\/w\/1\/plants\/11\/forecast\//,
    );
    await continueWithGoogle(page);
    await expect.poll(() => sent).toEqual(["/app/w/1/plants/11/forecast/"]);
    expect(await storedReturn(page)).toBeNull();
});
