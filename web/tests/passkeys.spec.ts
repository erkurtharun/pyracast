import { expect, test, type Page } from "./support/test";
import { firstAccount, mockApi } from "./support/workspace";

const base = "**/api/auth/browser/v1";
const management = `${base}/account/authenticators/webauthn`;
const key = { type: "webauthn", id: 8, name: "Test device" };
const value = { id: "test-key" };
const recoveryCodes = ["10000001", "10000002"] as const;
const success = { status: 200, meta: { is_authenticated: true } };
const askPasskey = {
    status: 401,
    json: {
        status: 401,
        meta: { is_authenticated: true },
        data: {
            flows: [
                {
                    id: "mfa_reauthenticate",
                    types: ["webauthn", "recovery_codes"],
                },
            ],
        },
    },
};

function loginFlows(page: Page, flows: object[]) {
    return page.route(`${base}/auth/session`, (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  status: 401,
                  json: {
                      status: 401,
                      data: { flows },
                      meta: { is_authenticated: false },
                  },
              })
            : route.fallback(),
    );
}

function browserCredentials(page: Page, decline = false) {
    return page.addInitScript(
        ({ decline, value }) => {
            const credential = Object.create(PublicKeyCredential.prototype, {
                toJSON: { value: () => value },
            });
            Object.defineProperties(CredentialsContainer.prototype, {
                create: { value: () => Promise.resolve(credential) },
                get: {
                    value: () =>
                        decline
                            ? Promise.reject(
                                  new DOMException(
                                      "Dismissed",
                                      "NotAllowedError",
                                  ),
                              )
                            : Promise.resolve(credential),
                },
            });
        },
        { decline, value },
    );
}

function assertion(page: Page, method: string, complete: () => void) {
    return page.route(`${base}/auth/webauthn/${method}`, async (route) => {
        if (route.request().method() === "GET") {
            await route.fulfill({
                json: {
                    status: 200,
                    data: {
                        request_options: { publicKey: { challenge: "AQI" } },
                    },
                },
            });
            return;
        }
        expect(route.request().postDataJSON()).toEqual({ credential: value });
        complete();
        await route.fulfill({ json: success });
    });
}

test("dismissing a passkey leaves password sign-in available", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await loginFlows(page, [{ id: "mfa_login_webauthn" }]);
    await browserCredentials(page, true);
    await assertion(page, "login", () => {
        throw new Error("A dismissed prompt cannot submit a credential");
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Sign in with a passkey" }).click();
    await expect(page.getByText(/No passkey was used/)).toBeVisible();
    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
    await expect(
        page
            .locator("form")
            .getByRole("button", { name: "Sign in", exact: true }),
    ).toBeEnabled();
});

test("a browser without passkeys can use a recovery code", async ({ page }) => {
    const account = await mockApi(page, { anonymous: true });
    await page.addInitScript(() => {
        Object.defineProperty(window, "PublicKeyCredential", {
            value: undefined,
        });
    });
    await loginFlows(page, [
        {
            id: "mfa_authenticate",
            is_pending: true,
            types: ["webauthn", "recovery_codes"],
        },
    ]);
    await page.route(`${base}/auth/2fa/authenticate`, async (route) => {
        expect(route.request().postDataJSON()).toEqual({
            code: recoveryCodes[0],
        });
        account.current = firstAccount;
        await route.fulfill({ json: success });
    });
    await page.goto("/app/");
    await page
        .getByLabel("Authenticator code or recovery code")
        .fill(recoveryCodes[0]);
    await expect(
        page.getByRole("button", { name: "Confirm with a passkey" }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});

test("a pending second factor accepts a passkey", async ({ page }) => {
    const account = await mockApi(page, { anonymous: true });
    await browserCredentials(page);
    await loginFlows(page, [
        { id: "mfa_authenticate", is_pending: true, types: ["webauthn"] },
    ]);
    await assertion(page, "authenticate", () => {
        account.current = firstAccount;
    });
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Two-step verification" }),
    ).toBeVisible();
    await expect(
        page.getByLabel("Authenticator code or recovery code"),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Confirm with a passkey" }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});

test("account settings enroll, replace recovery codes and revoke a passkey", async ({
    page,
}) => {
    await mockApi(page);
    await browserCredentials(page);
    await page.route(`${base}/config`, (route) =>
        route.fulfill({
            json: {
                status: 200,
                data: { mfa: { passkey_login_enabled: true } },
            },
        }),
    );
    let keys: (typeof key)[] = [];
    let confirmed = false;
    await page.route(`${base}/account/authenticators`, (route) =>
        route.fulfill({
            json: { status: 200, data: keys },
        }),
    );
    await assertion(page, "reauthenticate", () => {
        confirmed = true;
    });
    await page.route(`${management}**`, async (route) => {
        const request = route.request();
        if (request.method() === "GET") {
            expect(
                new URL(request.url()).searchParams.get("passwordless"),
            ).toBe("true");
            await route.fulfill({
                json: {
                    status: 200,
                    data: {
                        creation_options: {
                            publicKey: {
                                challenge: "AQI",
                                rp: { name: "Pyracast" },
                                user: {
                                    id: "AQI",
                                    name: "Test account",
                                    displayName: "Test account",
                                },
                                pubKeyCredParams: [
                                    { type: "public-key", alg: -7 },
                                ],
                            },
                        },
                    },
                },
            });
        } else if (request.method() === "POST") {
            expect(request.postDataJSON()).toEqual({
                name: key.name,
                credential: value,
            });
            keys = [key];
            await route.fulfill({
                json: {
                    status: 200,
                    data: key,
                    meta: { recovery_codes: recoveryCodes },
                },
            });
        } else if (!confirmed) {
            await route.fulfill(askPasskey);
        } else {
            expect(request.postDataJSON()).toEqual({
                authenticators: [key.id],
            });
            keys = [];
            await route.fulfill({ json: { status: 200 } });
        }
    });
    await page.route(`${base}/account/authenticators/recovery-codes`, (route) =>
        route.fulfill({
            json: { status: 200, data: { unused_codes: ["10000003"] } },
        }),
    );
    await page.goto("/app/account/");
    const settings = page.getByRole("region", { name: "Passkeys" });
    await settings.getByLabel("Passkey name (optional)").fill(key.name);
    await settings.getByRole("button", { name: "Add a passkey" }).click();
    await expect(
        settings.getByText(recoveryCodes[0], { exact: true }),
    ).toBeVisible();
    const downloaded = page.waitForEvent("download");
    await settings
        .getByRole("button", { name: "Download (.txt)", exact: true })
        .click();
    expect((await downloaded).suggestedFilename()).toBe(
        "pyracast-recovery-codes.txt",
    );
    await settings
        .getByRole("button", { name: "I have saved my codes" })
        .click();
    await settings
        .getByRole("button", { name: "Replace recovery codes" })
        .click();
    await settings.getByRole("button", { name: "Replace codes" }).click();
    await expect(settings.getByText("10000003", { exact: true })).toBeVisible();
    await settings
        .getByRole("button", { name: "I have saved my codes" })
        .click();
    await expect(
        settings.getByText(
            "Removing this passkey also deletes your recovery codes.",
        ),
    ).toBeVisible();
    await settings
        .getByRole("button", { name: "Remove passkey Test device" })
        .click();
    await settings
        .getByRole("button", { name: "Confirm with a passkey", exact: true })
        .click();
    await expect(
        settings.getByText("This account has no passkeys."),
    ).toBeVisible();
});

test("recovery code management follows authenticator app changes on the same page", async ({
    page,
}) => {
    await mockApi(page);
    await page.route(`${base}/config`, (route) =>
        route.fulfill({
            json: {
                status: 200,
                data: { mfa: { passkey_login_enabled: true } },
            },
        }),
    );
    let totp = false;
    await page.route(`${base}/account/authenticators`, (route) =>
        route.fulfill({
            json: {
                status: 200,
                data: [
                    key,
                    {
                        type: "recovery_codes",
                        unused_code_count: 2,
                        total_code_count: 2,
                    },
                    ...(totp ? [{ type: "totp" }] : []),
                ],
            },
        }),
    );
    await page.route(`${base}/account/authenticators/totp`, async (route) => {
        const method = route.request().method();
        if (method === "GET") {
            const secret = "JBSWY3DPEHPK3PXP";
            await route.fulfill({
                status: 404,
                json: {
                    status: 404,
                    meta: {
                        secret,
                        totp_url: `otpauth://totp/Pyracast?secret=${secret}`,
                    },
                },
            });
            return;
        }
        expect(["POST", "DELETE"]).toContain(method);
        totp = method === "POST";
        await route.fulfill({ json: { status: 200 } });
    });
    await page.goto("/app/account/");
    const passkeys = page.getByRole("region", { name: "Passkeys" });
    const manage = page.getByRole("button", {
        name: "Replace recovery codes",
        exact: true,
    });
    await expect(
        page.getByText("Authenticator app not set up", { exact: true }),
    ).toBeVisible();
    await passkeys
        .getByRole("button", { name: "Replace recovery codes" })
        .click();
    await expect(
        passkeys.getByRole("heading", { name: "Replace recovery codes?" }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Set up authenticator app" })
        .click();
    await page.getByLabel("Authenticator code", { exact: true }).fill("012345");
    await page
        .getByRole("button", { name: "Activate authenticator app" })
        .click();
    await expect(
        passkeys.getByRole("heading", { name: "Replace recovery codes?" }),
    ).toHaveCount(0);
    await expect(manage).toHaveCount(1);
    await expect(
        passkeys.getByRole("button", { name: "Replace recovery codes" }),
    ).toHaveCount(0);
    const remove = page.getByRole("button", {
        name: "Remove authenticator app",
        exact: true,
    });
    await remove.click();
    await expect(
        page.getByText(
            /If you have a passkey, you can keep using it and your recovery codes/,
        ),
    ).toBeVisible();
    await remove.click();
    await expect(
        passkeys.getByRole("button", { name: "Replace recovery codes" }),
    ).toBeVisible();
    await expect(manage).toHaveCount(1);
});
