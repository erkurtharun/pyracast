import { expect, test, type Page } from "./support/test";
import { allauthErrorBody } from "./support/errors";
import { mockApi, signedInSession, user } from "./support/workspace";

const askPassword = {
    status: 401,
    json: {
        status: 401,
        meta: { is_authenticated: true },
        data: { flows: [{ id: "reauthenticate" }] },
    },
};

async function pendingAddress(page: Page, email: string) {
    await page.route("**/api/auth/browser/v1/account/email", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  json: {
                      status: 200,
                      data: [
                          { email: user.email, primary: true, verified: true },
                          { email, primary: false, verified: false },
                      ],
                  },
              })
            : route.fallback(),
    );
}

function emailChange(page: Page) {
    return page.locator(".account-disclosure").filter({
        has: page.getByText("Change email", { exact: true }),
    });
}

test("an expired account session returns to sign in instead of asking for reauthentication", async ({
    page,
}) => {
    const account = await mockApi(page);
    await page.route(
        "**/api/auth/browser/v1/account/authenticators/totp",
        (route) => {
            account.current = null;
            return route.fulfill({
                status: 401,
                json: { status: 401, meta: { is_authenticated: false } },
            });
        },
    );
    await page.goto("/app/w/");
    await page.getByRole("button", { name: "Account" }).click();
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
});

test("an address code that needs a confirmation opens the prompt and reports no success", async ({
    page,
}) => {
    await mockApi(page);
    await pendingAddress(page, "new@example.test");
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) =>
        route.fulfill(askPassword),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: "Account" }).click();
    await page.getByText("Change email", { exact: true }).click();
    await page.getByLabel("Email verification code").fill("ABCD-EFGH");
    await page.getByRole("button", { name: "Verify new email" }).click();
    await expect(
        emailChange(page).getByRole("heading", {
            name: "Confirm your password",
        }),
    ).toBeVisible();
    await expect(page.getByText(/is now your address/)).toHaveCount(0);
});

test("administrator recovery codes request a fresh authenticator confirmation", async ({
    page,
}) => {
    await mockApi(page, { admin: true });
    await page.route(
        "**/api/auth/browser/v1/account/authenticators/recovery-codes",
        (route) =>
            route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    meta: { is_authenticated: true },
                    data: { flows: [{ id: "mfa_reauthenticate" }] },
                    errors: [
                        {
                            code: "mfa_reauthentication_required",
                            message:
                                "Enter your authenticator code or a recovery code.",
                        },
                    ],
                },
            }),
    );
    await page.goto("/app/w/");
    await page.getByRole("button", { name: "Account" }).click();
    await page.getByRole("button", { name: "Generate recovery codes" }).click();
    await page.getByRole("button", { name: "Replace codes" }).click();
    await expect(
        page.getByRole("heading", { name: "Confirm with your authenticator" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Confirm authenticator" }),
    ).toBeVisible();
});

test("an email change confirms the password in the shared prompt, resumes the request and verifies the code", async ({
    page,
}) => {
    await mockApi(page);
    const submitted: unknown[] = [];
    const confirmations: unknown[] = [];
    let verified = false;
    await page.route("**/api/auth/browser/v1/account/email", (route) => {
        if (route.request().method() === "GET") return route.fallback();
        submitted.push(route.request().postDataJSON());
        if (submitted.length === 2)
            expect(confirmations).toEqual([{ password: "current-password" }]);
        return route.fulfill(
            submitted.length === 1 ? askPassword : { json: { status: 200 } },
        );
    });
    await page.route("**/api/auth/browser/v1/auth/reauthenticate", (route) => {
        confirmations.push(route.request().postDataJSON());
        return route.fulfill({
            json: { status: 200, meta: { is_authenticated: true } },
        });
    });
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) => {
        expect(route.request().postDataJSON()).toEqual({ key: "ABCD-EFGH" });
        verified = true;
        return route.fulfill({
            json: { status: 200, meta: { is_authenticated: true } },
        });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Account" }).click();
    const change = emailChange(page);
    await change.getByText("Change email", { exact: true }).click();
    await expect(
        change.getByText(/Other devices are signed out/),
    ).toBeVisible();
    await change.getByLabel("New email address").fill("new@example.test");
    await change
        .getByRole("button", { name: "Send verification code" })
        .click();
    await expect(
        change.getByRole("heading", { name: "Confirm your password" }),
    ).toBeVisible();
    await change
        .getByLabel("Current password", { exact: true })
        .fill("current-password");
    await change.getByRole("button", { name: "Confirm password" }).click();
    await expect(
        change.getByText(
            "Enter the code sent to new@example.test. It is valid for 10 minutes.",
        ),
    ).toBeVisible();
    expect(submitted).toEqual([
        { email: "new@example.test" },
        { email: "new@example.test" },
    ]);
    expect(confirmations).toEqual([{ password: "current-password" }]);
    await change.getByLabel("Email verification code").fill("ABCD-EFGH");
    await change.getByRole("button", { name: "Verify new email" }).click();
    await expect(
        change.getByText("new@example.test is now your address."),
    ).toBeVisible();
    expect(verified).toBe(true);
});

test("a pending address change resumes after a reload and can be replaced by another address", async ({
    page,
}) => {
    await mockApi(page);
    await pendingAddress(page, "pending@example.test");
    const removed: unknown[] = [];
    await page.route("**/api/auth/browser/v1/account/email", (route) => {
        if (route.request().method() !== "DELETE") return route.fallback();
        removed.push(route.request().postDataJSON());
        return route.fulfill({ json: { status: 200, data: [] } });
    });
    await page.goto("/app/account/");
    const change = emailChange(page);
    await change.getByText("Change email", { exact: true }).click();
    await expect(change.getByText(/pending@example\.test/)).toBeVisible();
    await change.getByRole("button", { name: "Use another address" }).click();
    await expect(
        change.getByText("The address change was cancelled."),
    ).toBeVisible();
    await expect(change.getByLabel("New email address")).toBeVisible();
    expect(removed).toEqual([{ email: "pending@example.test" }]);
});

test("a password change asks for the factor the server names and resumes the same change", async ({
    page,
}) => {
    await mockApi(page);
    const changes: unknown[] = [];
    await page.route(
        "**/api/auth/browser/v1/account/password/change",
        (route) => {
            changes.push(route.request().postDataJSON());
            return route.fulfill(
                changes.length === 1
                    ? askPassword
                    : {
                          json: {
                              status: 200,
                              meta: { is_authenticated: true },
                          },
                      },
            );
        },
    );
    await page.route("**/api/auth/browser/v1/auth/reauthenticate", (route) =>
        route.fulfill({
            json: { status: 200, meta: { is_authenticated: true } },
        }),
    );
    await page.goto("/app/account/");
    const password = page.locator(".password-disclosure");
    await password.locator("summary").click();
    await password
        .getByLabel("Current password", { exact: true })
        .fill("current-password");
    await password
        .getByLabel("New password", { exact: true })
        .fill("a-new-and-long-password");
    await password.getByLabel("Show passwords").check();
    await expect(
        password.getByLabel("New password", { exact: true }),
    ).toHaveAttribute("type", "text");
    await password.getByRole("button", { name: "Change password" }).click();
    await password
        .getByLabel("Current password", { exact: true })
        .fill("current-password");
    await password.getByRole("button", { name: "Confirm password" }).click();
    await expect.poll(() => changes).toHaveLength(2);
    expect(changes[1]).toEqual(changes[0]);
});

test("the account lists where it is signed in and signs out another session", async ({
    page,
}) => {
    await mockApi(page);
    const other = {
        ...signedInSession,
        id: 2,
        ip: "198.51.100.4",
        user_agent:
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
        is_current: false,
    };
    const ended: unknown[] = [];
    await page.route("**/api/auth/browser/v1/auth/sessions", (route) => {
        if (route.request().method() === "DELETE") {
            ended.push(route.request().postDataJSON());
            return route.fulfill({
                json: { status: 200, data: [signedInSession] },
            });
        }
        return route.fulfill({
            json: { status: 200, data: [other, signedInSession] },
        });
    });
    await page.goto("/app/account/");
    const devices = page.getByRole("region", {
        name: "Where you are signed in",
    });
    await expect(devices.getByText("This device")).toBeVisible();
    await expect(devices.getByText("Chrome · Windows")).toBeVisible();
    await devices
        .getByRole("button", { name: "Sign out Safari · iOS" })
        .click();
    await expect(devices.getByText("Safari · iOS")).toHaveCount(0);
    expect(ended).toEqual([{ sessions: [2] }]);
});

test("one sign-out runs at a time, says it is working and names each session by its own details", async ({
    page,
}) => {
    await mockApi(page);
    const sameDevice = (id: number, hours: number) => ({
        ...signedInSession,
        id,
        ip: `198.51.100.${id}`,
        created_at: signedInSession.created_at - hours * 3600,
        last_seen_at: signedInSession.last_seen_at - hours * 3600,
        is_current: false,
    });
    const first = sameDevice(2, 30);
    const second = sameDevice(3, 60);
    const ended: unknown[] = [];
    let release: () => void = () => {};
    const answered = new Promise<void>((resolve) => {
        release = resolve;
    });
    let arrive: () => void = () => {};
    const arrived = new Promise<void>((resolve) => {
        arrive = resolve;
    });
    await page.route("**/api/auth/browser/v1/auth/sessions", async (route) => {
        if (route.request().method() !== "DELETE")
            return route.fulfill({
                json: {
                    status: 200,
                    data: [first, second, signedInSession],
                },
            });
        ended.push(route.request().postDataJSON());
        arrive();
        await answered;
        return route.fulfill({
            json: { status: 200, data: [second, signedInSession] },
        });
    });
    await page.goto("/app/account/");
    const devices = page.getByRole("region", {
        name: "Where you are signed in",
    });
    const rows = devices.getByRole("button", {
        name: /^Sign out Chrome · Windows/,
    });
    await expect(rows).toHaveCount(2);
    const names = await rows.evaluateAll((buttons) =>
        buttons.map((button) => button.getAttribute("aria-label") ?? ""),
    );
    expect(new Set(names).size).toBe(2);
    expect(names.some((name) => name.includes("198.51.100."))).toBe(false);

    await rows.first().click();
    await expect(
        devices.getByRole("button", { name: "Working…", exact: true }),
    ).toBeDisabled();
    await expect(rows).toHaveCount(1);
    await expect(rows).toBeDisabled();
    await expect(
        devices.getByRole("button", { name: "Sign out all other sessions" }),
    ).toBeDisabled();
    await arrived;
    expect(ended).toEqual([{ sessions: [2] }]);
    release();
    await expect(rows).toHaveCount(1);
    await expect(rows).toBeEnabled();
    expect(ended).toEqual([{ sessions: [2] }]);
});

test("the email change runs one action at a time", async ({ page }) => {
    await mockApi(page);
    await pendingAddress(page, "pending@example.test");
    const sent: string[] = [];
    let release: () => void = () => {};
    const answered = new Promise<void>((resolve) => {
        release = resolve;
    });
    let arrive: () => void = () => {};
    const arrived = new Promise<void>((resolve) => {
        arrive = resolve;
    });
    await page.route(
        "**/api/auth/browser/v1/auth/email/verify/resend",
        async (route) => {
            sent.push("resend");
            arrive();
            await answered;
            return route.fulfill({ json: { status: 200 } });
        },
    );
    await page.goto("/app/account/");
    const change = emailChange(page);
    await change.getByText("Change email", { exact: true }).click();
    await change.getByLabel("Email verification code").fill("ABCD-EFGH");
    await change.getByRole("button", { name: "Send the code again" }).click();
    await expect(
        change.getByRole("button", { name: "Working…", exact: true }),
    ).toBeDisabled();
    await expect(
        change.getByRole("button", { name: "Verify new email" }),
    ).toBeDisabled();
    await expect(
        change.getByRole("button", { name: "Use another address" }),
    ).toBeDisabled();
    await arrived;
    expect(sent).toEqual(["resend"]);
    release();
    await expect(change.getByText("A new code was sent.")).toBeVisible();
    await expect(
        change.getByRole("button", { name: "Verify new email" }),
    ).toBeEnabled();
});

test("a resend that finds the code expired starts the change again", async ({
    page,
}) => {
    await mockApi(page);
    await pendingAddress(page, "pending@example.test");
    await page.route(
        "**/api/auth/browser/v1/auth/email/verify/resend",
        (route) =>
            route.fulfill({
                status: 409,
                json: allauthErrorBody(409, "conflict", [
                    {
                        message:
                            "The request conflicts with the current state.",
                    },
                ]),
            }),
    );
    await page.goto("/app/account/");
    const change = emailChange(page);
    await change.getByText("Change email", { exact: true }).click();
    await change.getByRole("button", { name: "Send the code again" }).click();
    await expect(
        change.getByText("The code is no longer valid. Request a new one."),
    ).toBeVisible();
    await expect(change.getByLabel("New email address")).toBeVisible();
    await expect(change.getByLabel("Email verification code")).toHaveCount(0);
});

test("the address form stays disabled while the confirmed request runs", async ({
    page,
}) => {
    await mockApi(page);
    const submitted: unknown[] = [];
    let release: () => void = () => {};
    const answered = new Promise<void>((resolve) => {
        release = resolve;
    });
    let arrive: () => void = () => {};
    const arrived = new Promise<void>((resolve) => {
        arrive = resolve;
    });
    await page.route("**/api/auth/browser/v1/account/email", async (route) => {
        if (route.request().method() === "GET") return route.fallback();
        submitted.push(route.request().postDataJSON());
        if (submitted.length === 1) return route.fulfill(askPassword);
        arrive();
        await answered;
        return route.fulfill({ json: { status: 200 } });
    });
    await page.route("**/api/auth/browser/v1/auth/reauthenticate", (route) =>
        route.fulfill({
            json: { status: 200, meta: { is_authenticated: true } },
        }),
    );
    await page.goto("/app/account/");
    const change = emailChange(page);
    await change.getByText("Change email", { exact: true }).click();
    await change.getByLabel("New email address").fill("new@example.test");
    await change
        .getByRole("button", { name: "Send verification code" })
        .click();
    await change
        .getByLabel("Current password", { exact: true })
        .fill("current-password");
    await change.getByRole("button", { name: "Confirm password" }).click();
    await arrived;
    await expect(change.getByLabel("New email address")).toBeDisabled();
    await expect(
        change.getByRole("button", { name: "Send verification code" }),
    ).toBeDisabled();
    release();
    await expect(
        change.getByText(
            "Enter the code sent to new@example.test. It is valid for 10 minutes.",
        ),
    ).toBeVisible();
    expect(submitted).toEqual([
        { email: "new@example.test" },
        { email: "new@example.test" },
    ]);
});

test("using another address after the change ended starts the change again", async ({
    page,
}) => {
    await mockApi(page);
    let pending = true;
    const removed: unknown[] = [];
    await page.route("**/api/auth/browser/v1/account/email", (route) => {
        const method = route.request().method();
        if (method === "GET")
            return route.fulfill({
                json: {
                    status: 200,
                    data: [
                        { email: user.email, primary: true, verified: true },
                        ...(pending
                            ? [
                                  {
                                      email: "pending@example.test",
                                      primary: false,
                                      verified: false,
                                  },
                              ]
                            : []),
                    ],
                },
            });
        if (method !== "DELETE") return route.fallback();
        removed.push(route.request().postDataJSON());
        return route.fulfill({
            status: 400,
            json: allauthErrorBody(400, "invalid_request", [
                {
                    message: "The address is not assigned to this account.",
                    code: "unknown_email",
                    param: "email",
                },
            ]),
        });
    });
    await page.goto("/app/account/");
    const change = emailChange(page);
    await change.getByText("Change email", { exact: true }).click();
    await expect(change.getByText(/pending@example\.test/)).toBeVisible();
    pending = false;
    await change.getByRole("button", { name: "Use another address" }).click();
    await expect(
        change.getByText("The code is no longer valid. Request a new one."),
    ).toBeVisible();
    await expect(change.getByLabel("New email address")).toBeVisible();
    await expect(
        change.getByText("The address is not assigned to this account."),
    ).toHaveCount(0);
    expect(removed).toEqual([{ email: "pending@example.test" }]);
});

test("a refused removal keeps its failure when the session ends before the addresses are read again", async ({
    page,
}) => {
    await mockApi(page);
    let refused = false;
    let rereads = 0;
    await page.route("**/api/auth/browser/v1/account/email", (route) => {
        const method = route.request().method();
        if (method === "GET") {
            if (refused) {
                rereads += 1;
                return route.fulfill({
                    status: 401,
                    json: {
                        status: 401,
                        meta: { is_authenticated: false },
                        data: { flows: [{ id: "login" }] },
                    },
                });
            }
            return route.fulfill({
                json: {
                    status: 200,
                    data: [
                        { email: user.email, primary: true, verified: true },
                        {
                            email: "pending@example.test",
                            primary: false,
                            verified: false,
                        },
                    ],
                },
            });
        }
        if (method !== "DELETE") return route.fallback();
        refused = true;
        return route.fulfill({
            status: 400,
            json: allauthErrorBody(400, "invalid_request", [
                {
                    message: "The address is not assigned to this account.",
                    code: "unknown_email",
                    param: "email",
                },
            ]),
        });
    });
    await page.goto("/app/account/");
    const change = emailChange(page);
    await change.getByText("Change email", { exact: true }).click();
    await expect(change.getByText(/pending@example\.test/)).toBeVisible();
    await change.getByRole("button", { name: "Use another address" }).click();
    await expect(
        change.getByText("The address is not assigned to this account."),
    ).toBeVisible();
    expect(rereads).toBe(1);
    await expect(change.getByText(/pending@example\.test/)).toBeVisible();
    await expect(
        change.getByText("The code is no longer valid. Request a new one."),
    ).toHaveCount(0);
    await expect(change.getByText(/Something went wrong/)).toHaveCount(0);
});
