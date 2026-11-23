import { test, expect, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { firstAccount, mockApi } from "./support/workspace";
import { allauthErrorBody, errorBody } from "./support/errors";

test("signup entry opens registration with public information links", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.goto("/app/?signup=1");
    await expect(
        page.getByRole("heading", { name: "Create your company account" }),
    ).toBeVisible();
    await expect(
        page.getByRole("link", { name: "Homepage", exact: true }),
    ).toHaveAttribute("href", "/");
    await expect(
        page.getByRole("link", { name: "Privacy", exact: true }),
    ).toHaveAttribute("href", "/privacy/");
    await expect(
        page.getByRole("link", { name: "Terms", exact: true }),
    ).toHaveAttribute("href", "/terms/");
    await expect(page.getByRole("checkbox")).toHaveCount(0);
    await page
        .getByRole("button", { name: "Sign in", exact: true })
        .first()
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(
        page.getByRole("link", { name: "Privacy", exact: true }),
    ).toBeVisible();
});

for (const [flow, title] of [
    ["verify_email", "Verify your email"],
    ["mfa_authenticate", "Two-factor authentication"],
    ["password_reset_by_code", "Choose a new password"],
    ["provider_signup", "Complete your account"],
] as const) {
    test(`pending ${flow} takes precedence over signup entry`, async ({
        page,
    }) => {
        await mockApi(page, { anonymous: true });
        await page.route(
            "**/api/auth/browser/v1/auth/provider/signup",
            (route) =>
                route.fulfill({ json: { status: 200, data: { email: [] } } }),
        );
        await page.route("**/api/auth/browser/v1/auth/session", (route) =>
            route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    data: { flows: [{ id: flow, is_pending: true }] },
                    meta: { is_authenticated: false },
                },
            }),
        );
        await page.goto("/app/?signup=1");
        await expect(
            page.getByRole("heading", { name: title, exact: true }),
        ).toBeVisible();
        await expect(
            page.getByRole("heading", { name: "Create your company account" }),
        ).not.toBeVisible();
    });
}

test("requires email verification after registration", async ({ page }) => {
    await mockApi(page, { anonymous: true });
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
    await page.goto("/app/");
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
            json: allauthErrorBody(400, "invalid_request", [
                {
                    param: "email",
                    message: "This email is already in use.",
                },
            ]),
        }),
    );
    await page.goto("/app/");
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
    await page.goto("/app/");
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

test("a sign-in server error shows its reference", async ({ page }) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/login", (route) =>
        route.fulfill({
            status: 500,
            json: errorBody(
                "server_error",
                "The server could not complete this request.",
                { request_id: "0123456789abcdef0123456789abcdef" },
            ),
        }),
    );
    await page.goto("/app/");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.locator("form").getByRole("alert")).toContainText(
        "The server could not complete this request. Reference: 0123456789abcdef0123456789abcdef",
    );
});

test("an ended session asks to sign in again", async ({ page }) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/login", (route) =>
        route.fulfill({
            status: 400,
            json: errorBody(
                "session_ended",
                "Your session ended. Sign in again.",
            ),
        }),
    );
    await page.goto("/app/");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.locator("form").getByRole("alert")).toContainText(
        "Your session ended. Sign in again.",
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
    await page.goto("/app/");
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

async function pendingVerification(page: Page) {
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
}

async function pendingStage(page: Page, flow: string) {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/provider/signup", (route) =>
        route.fulfill({ json: { status: 200, data: { email: [] } } }),
    );
    let ended = 0;
    await page.route("**/api/auth/browser/v1/auth/session", (route) => {
        if (route.request().method() === "DELETE") {
            ended += 1;
            return route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    data: { flows: [{ id: "login" }] },
                    meta: { is_authenticated: false },
                },
            });
        }
        return route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: {
                    flows: ended ? [] : [{ id: flow, is_pending: true }],
                },
                meta: { is_authenticated: false },
            },
        });
    });
    return { ended: () => ended };
}

test("the code stage names the address and leaving it ends the pending sign-up on the server", async ({
    page,
}) => {
    const stage = await pendingStage(page, "login");
    await page.route("**/api/auth/browser/v1/auth/signup", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "verify_email", is_pending: true }] },
                meta: { is_authenticated: false },
            },
        }),
    );
    await page.goto("/app/?signup=1");
    await expect(
        page.getByLabel("Password", { exact: true }),
    ).toHaveAccessibleDescription("Use at least 15 characters.");
    await page.getByLabel("Company name").fill("Smart Solar Energy");
    await page.getByLabel("Email").fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Create company account", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Verify your email", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText(
            "We sent a code to owner@example.test. Enter it here to confirm the address.",
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("navigation", { name: "Account access" }),
    ).toHaveCount(0);
    await page
        .getByRole("button", { name: "Use a different address", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back", exact: true }),
    ).toBeVisible();
    expect(stage.ended()).toBe(1);
});

for (const [flow, title, guide, leave] of [
    [
        "verify_email",
        "Verify your email",
        "We sent a code to the address you entered. Enter it here to confirm the address.",
        "Use a different address",
    ],
    [
        "mfa_authenticate",
        "Two-factor authentication",
        "Enter the code from your authenticator app, or one of your recovery codes.",
        "Use a different account",
    ],
    [
        "password_reset_by_code",
        "Choose a new password",
        "If an account uses the address you entered, we sent a code to it. Enter the code and choose a new password.",
        "Cancel password reset",
    ],
    [
        "provider_signup",
        "Complete your account",
        "Confirm your email and company to finish. If you already have an account, sign in first and connect this provider in Account settings.",
        "Use another sign-in method",
    ],
] as const) {
    test(`the pending ${flow} stage explains itself and its exit ends it on the server`, async ({
        page,
    }) => {
        const stage = await pendingStage(page, flow);
        await page.goto("/app/");
        await expect(
            page.getByRole("heading", { name: title, exact: true }),
        ).toBeVisible();
        await expect(page.getByText(guide, { exact: true })).toBeVisible();
        await expect(
            page.getByRole("navigation", { name: "Account access" }),
        ).toHaveCount(0);
        await page.getByRole("button", { name: leave, exact: true }).click();
        await expect(
            page.getByRole("heading", { name: "Welcome back", exact: true }),
        ).toBeVisible();
        expect(stage.ended()).toBe(1);
        await expect(
            page.getByRole("navigation", { name: "Account access" }),
        ).toBeVisible();
    });
}

test("a verification the server no longer holds shows an ended stage that starts again from sign-in", async ({
    page,
}) => {
    const stage = await pendingStage(page, "verify_email");
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) =>
        route.fulfill({
            status: 409,
            json: allauthErrorBody(409, "conflict", [{ message: "Conflict." }]),
        }),
    );
    await page.goto("/app/");
    await page.getByLabel("Email code").fill("ABCD-EFGH");
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "This step has ended", exact: true }),
    ).toBeFocused();
    await expect(
        page.getByText(
            "The code can no longer be used here: it expired, it was used, or too many wrong codes were entered. Start again to get a new code.",
            { exact: true },
        ),
    ).toBeVisible();
    await expect(page.getByLabel("Email code")).toHaveCount(0);
    await expect(page.getByText("Conflict.")).toHaveCount(0);
    await page
        .getByRole("button", { name: "Start again", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back", exact: true }),
    ).toBeVisible();
    expect(stage.ended()).toBe(1);
});

test("a rate-limited verification shows a wait stage that returns to the code or leaves", async ({
    page,
}) => {
    const stage = await pendingStage(page, "verify_email");
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) =>
        route.fulfill({
            status: 429,
            json: allauthErrorBody(429, "throttled", [
                { message: "Too many requests." },
            ]),
        }),
    );
    await page.goto("/app/");
    await page.getByLabel("Email code").fill("ABCD-EFGH");
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Too many attempts", exact: true }),
    ).toBeFocused();
    await expect(
        page.getByText(
            "Wait a few minutes, then try again. The step stays open until then.",
            { exact: true },
        ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Verify your email", exact: true }),
    ).toBeVisible();
    await expect(page.getByLabel("Email code")).toBeVisible();
    expect(stage.ended()).toBe(0);
    await page.getByLabel("Email code").fill("ABCD-EFGH");
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Use a different address", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back", exact: true }),
    ).toBeVisible();
    expect(stage.ended()).toBe(1);
});

for (const [status, code, title] of [
    [409, "conflict", "This step has ended"],
    [429, "throttled", "Too many attempts"],
] as const) {
    test(`a ${status} refusal after a resend replaces the resend notice`, async ({
        page,
    }) => {
        await pendingStage(page, "verify_email");
        await page.route(
            "**/api/auth/browser/v1/auth/email/verify/resend",
            (route) => route.fulfill({ json: { status: 200 } }),
        );
        await page.route("**/api/auth/browser/v1/auth/email/verify", (route) =>
            route.fulfill({
                status,
                json: allauthErrorBody(status, code, [{ message: "Refused." }]),
            }),
        );
        await page.goto("/app/");
        await page
            .getByRole("button", {
                name: "Resend verification code",
                exact: true,
            })
            .click();
        await expect(page.getByText("A new code was requested.")).toBeVisible();
        await page.getByLabel("Email code").fill("ABCD-EFGH");
        await page
            .getByRole("button", { name: "Verify email", exact: true })
            .click();
        await expect(
            page.getByRole("heading", { name: title, exact: true }),
        ).toBeFocused();
        await expect(page.getByText("A new code was requested.")).toHaveCount(
            0,
        );
    });
}

for (const [flow, field, value, submit] of [
    [
        "mfa_authenticate",
        "Authenticator or recovery code",
        "123456",
        "Verify code",
    ],
    [
        "provider_signup",
        "Company name",
        "Solar Company",
        "Complete your account",
    ],
] as const) {
    test(`an ended ${flow} stage does not speak of an emailed code`, async ({
        page,
    }) => {
        await pendingStage(page, flow);
        await page.route(
            /\/api\/auth\/browser\/v1\/auth\/(2fa\/authenticate|provider\/signup)$/,
            (route) =>
                route.request().method() === "POST"
                    ? route.fulfill({
                          status: 409,
                          json: allauthErrorBody(409, "conflict", [
                              { message: "Conflict." },
                          ]),
                      })
                    : route.fallback(),
        );
        await page.goto("/app/");
        await page.getByLabel(field, { exact: true }).fill(value);
        if (flow === "provider_signup")
            await page.getByLabel("Email").fill("owner@example.test");
        await page.getByRole("button", { name: submit, exact: true }).click();
        await expect(
            page.getByRole("heading", {
                name: "This step has ended",
                exact: true,
            }),
        ).toBeFocused();
        await expect(
            page.getByText(
                "This step can no longer be completed. Start again from sign-in.",
                { exact: true },
            ),
        ).toBeVisible();
        await expect(
            page.getByText("The code can no longer be used here"),
        ).toHaveCount(0);
    });
}

test("a resend offered again after a wait stage counts from the code that was sent", async ({
    page,
}) => {
    await page.clock.install();
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/signup", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "verify_email", is_pending: true }] },
                meta: { is_authenticated: false },
            },
        }),
    );
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) =>
        route.fulfill({
            status: 429,
            json: allauthErrorBody(429, "throttled", [
                { message: "Too many requests." },
            ]),
        }),
    );
    await page.goto("/app/?signup=1");
    await page.getByLabel("Company name").fill("Solar Company");
    await page.getByLabel("Email").fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Create company account", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Resend code in 60 s", exact: true }),
    ).toBeDisabled();
    await page.clock.runFor(45_000);
    await page.getByLabel("Email code").fill("ABCD-EFGH");
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Resend code in 15 s", exact: true }),
    ).toBeDisabled();
    await page.clock.runFor(15_000);
    await page.getByLabel("Email code").fill("ABCD-EFGH");
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(
        page.getByRole("button", {
            name: "Resend verification code",
            exact: true,
        }),
    ).toBeEnabled();
});

test("a failed exit keeps the stage and says the sign-in was not ended", async ({
    page,
}) => {
    await pendingStage(page, "mfa_authenticate");
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.request().method() === "DELETE"
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "Try again in a moment."),
              })
            : route.fallback(),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: "Use a different account", exact: true })
        .click();
    const alert = page.getByRole("main").getByRole("alert");
    await expect(alert).toContainText("The sign-in step was not ended");
    await expect(alert).toContainText("Try again in a moment.");
    await expect(
        page.getByRole("heading", {
            name: "Two-factor authentication",
            exact: true,
        }),
    ).toBeVisible();
});

test("resend is disabled with a countdown after a code is sent", async ({
    page,
}) => {
    await page.clock.install();
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/signup", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "verify_email", is_pending: true }] },
                meta: { is_authenticated: false },
            },
        }),
    );
    let resends = 0;
    await page.route(
        "**/api/auth/browser/v1/auth/email/verify/resend",
        (route) => {
            resends += 1;
            return route.fulfill({ json: { status: 200 } });
        },
    );
    await page.goto("/app/?signup=1");
    await page.getByLabel("Company name").fill("Solar Company");
    await page.getByLabel("Email").fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Create company account", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Resend code in 60 s", exact: true }),
    ).toBeDisabled();
    await page.clock.runFor(30_000);
    await expect(
        page.getByRole("button", { name: "Resend code in 30 s", exact: true }),
    ).toBeDisabled();
    await page.clock.runFor(30_000);
    await page
        .getByRole("button", { name: "Resend verification code", exact: true })
        .click();
    await expect(page.getByText("A new code was requested.")).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Resend code in 60 s", exact: true }),
    ).toBeDisabled();
    expect(resends).toBe(1);
});

for (const [status, text] of [
    [429, "Wait a minute before requesting another code."],
    [
        409,
        "No more codes can be sent for this sign-in. Sign in again to receive a new code.",
    ],
] as const) {
    test(`a resend answered with ${status} explains what to do next`, async ({
        page,
    }) => {
        await pendingVerification(page);
        await page.route(
            "**/api/auth/browser/v1/auth/email/verify/resend",
            (route) => route.fulfill({ status, json: { status } }),
        );
        await page.goto("/app/");
        await page
            .getByRole("button", {
                name: "Resend verification code",
                exact: true,
            })
            .click();
        const alert = page.locator("form").getByRole("alert");
        await expect(alert).toContainText(text);
        await expect(alert).not.toContainText("Request failed");
    });
}

test("a resend that ends the pending sign-in asks to sign in again", async ({
    page,
}) => {
    await pendingVerification(page);
    await page.route(
        "**/api/auth/browser/v1/auth/email/verify/resend",
        (route) =>
            route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    data: { flows: [{ id: "login" }] },
                    meta: { is_authenticated: false },
                },
            }),
    );
    await page.goto("/app/");
    const resend = page.getByRole("button", {
        name: "Resend verification code",
        exact: true,
    });
    await resend.click();
    await expect(page.locator("form").getByRole("alert")).toContainText(
        "No more codes can be sent for this sign-in. Sign in again to receive a new code.",
    );
    await expect(resend).toBeDisabled();
});

test("a failed resend shows the server message and can be retried", async ({
    page,
}) => {
    await pendingVerification(page);
    await page.route(
        "**/api/auth/browser/v1/auth/email/verify/resend",
        (route) =>
            route.fulfill({
                status: 500,
                json: allauthErrorBody(500, "server_error", [
                    { message: "Mail is unavailable." },
                ]),
            }),
    );
    await page.goto("/app/");
    const resend = page.getByRole("button", {
        name: "Resend verification code",
        exact: true,
    });
    await resend.click();
    await expect(page.locator("form").getByRole("alert")).toContainText(
        "Mail is unavailable.",
    );
    await expect(resend).toBeEnabled();
});

test("an unexplained 429 on sign-in offers a password reset", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/login", (route) =>
        route.fulfill({ status: 429, json: { status: 429 } }),
    );
    await page.goto("/app/");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.locator("form").getByRole("alert")).toContainText(
        "Too many attempts. Wait a few minutes and try again, or reset your password.",
    );
    await page
        .getByRole("button", { name: "Forgot password?", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Reset your password" }),
    ).toBeVisible();
});

test("an unexplained 429 on sign-up asks to wait", async ({ page }) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/signup", (route) =>
        route.fulfill({ status: 429, json: { status: 429 } }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await page.getByLabel("Company name").fill("Smart Solar Energy");
    await page.getByLabel("Email").fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Create company account", exact: true })
        .click();
    await expect(page.locator("form").getByRole("alert")).toContainText(
        "Too many requests. Wait a few minutes and try again.",
    );
});

test("requires the second factor before opening the workspace", async ({
    page,
}) => {
    const account = await mockApi(page, { anonymous: true });
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
            account.current = firstAccount;
            return route.fulfill({
                json: { status: 200, meta: { is_authenticated: true } },
            });
        },
    );
    await page.goto("/app/");
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
    await mockApi(page, { anonymous: true, admin: true });
    await page.route("**/admin/", (route) =>
        route.fulfill({ contentType: "text/html", body: "<h1>Admin</h1>" }),
    );
    await page.goto("/app/?next=%2Fadmin%2F");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Admin" })).toBeVisible();
});

test("a console deep link survives sign in and the second factor", async ({
    page,
}) => {
    const account = await mockApi(page, { anonymous: true, admin: true });
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
            account.current = firstAccount;
            return route.fulfill({
                json: { status: 200, meta: { is_authenticated: true } },
            });
        },
    );
    await page.route(/\/admin\/plants\/7\/\?tab=review$/, (route) =>
        route.fulfill({
            contentType: "text/html",
            body: "<h1>Plant review</h1>",
        }),
    );
    await page.goto("/app/?next=%2Fadmin%2Fplants%2F7%2F%3Ftab%3Dreview");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .getByRole("button", { name: "Sign in", exact: true })
        .last()
        .click();
    await page.getByLabel("Authenticator or recovery code").fill("123456");
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(
        page.getByRole("heading", { name: "Plant review" }),
    ).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/admin/plants/7/");
});

for (const next of [
    "//evil.example/admin/",
    "/\\evil.example/admin/",
    "/admin/../app/w/",
    "/admin/%2e%2e/app/w/",
    "https://evil.example/admin/",
    "http://[::1/admin/",
    " /admin/plants/7/",
    "/admin/plants/7/\u2028",
]) {
    test(`an administrator lands on the console overview for ${JSON.stringify(next)}`, async ({
        page,
    }) => {
        await mockApi(page, { admin: true });
        await page.route("**/admin/", (route) =>
            route.fulfill({
                contentType: "text/html",
                body: "<h1>Administration</h1>",
            }),
        );
        await page.goto(`/app/?next=${encodeURIComponent(next)}`);
        await expect(
            page.getByRole("heading", { name: "Administration" }),
        ).toBeVisible();
        expect(new URL(page.url()).pathname).toBe("/admin/");
    });
}

test("an administrator opens administration by default", async ({ page }) => {
    await mockApi(page, { admin: true });
    await page.route("**/admin/", (route) =>
        route.fulfill({
            contentType: "text/html",
            body: "<h1>Administration</h1>",
        }),
    );
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Administration" }),
    ).toBeVisible();
});

test("an administrator can choose the customer workspace and return", async ({
    page,
}) => {
    await mockApi(page, { admin: true });
    await page.goto("/app/w/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await expect(
        page.getByRole("link", { name: "Administration" }),
    ).toHaveAttribute("href", "/admin/");
    await page.goto("/app/account/");
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/app/account/");
});

test("a customer cannot enter administration through a return parameter", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/?next=%2Fadmin%2F");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await expect(
        page.getByRole("link", { name: "Administration" }),
    ).toHaveCount(0);
});

test("sign in ignores an external return path", async ({ page }) => {
    await mockApi(page, { anonymous: true });
    await page.goto("/app/?next=https%3A%2F%2Fevil.example%2F");
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
    await page.goto("/app/");
    await page.getByRole("button", { name: "Forgot password?" }).click();
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page.getByRole("button", { name: "Send reset code" }).click();
    await expect(
        page.getByRole("heading", { name: "Choose a new password" }),
    ).toBeVisible();
    await expect(
        page.getByText(
            "If an account uses owner@example.test, we sent a code to it. Enter the code and choose a new password.",
            { exact: true },
        ),
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
    await page.goto("/app/");
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
    expect(await accessibilityViolations(page)).toEqual([]);
});

test("a verification answered with a refusal that is not allauth's does not report the email verified", async ({
    page,
}) => {
    await pendingVerification(page);
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) =>
        route.fulfill({ status: 401, json: { detail: "Unauthorized" } }),
    );
    await page.goto("/app/");
    await page.getByLabel("Email code").fill("123456");
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await expect(
        page.getByText("Request failed (401). Please try again.", {
            exact: true,
        }),
    ).toBeVisible();
    await expect(
        page.getByText("Email verified. You can sign in now."),
    ).toHaveCount(0);
    await expect(page.getByLabel("Email code")).toBeVisible();
});
