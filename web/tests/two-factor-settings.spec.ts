import AxeBuilder from "@axe-core/playwright";
import { readFile } from "node:fs/promises";
import { expect, test, type Page } from "./support/test";
import jsQR from "jsqr";
import { PNG } from "pngjs";
import { mockApi, paged, user } from "./support/workspace";

const secret = "JBSWY3DPEHPK3PXP";
const provisioningUri = `otpauth://totp/Pyracast:solar%40example.test?secret=${secret}&issuer=Pyracast`;
const authenticatorsPath = "/api/auth/browser/v1/account/authenticators";

async function mockTwoFactor(
    page: Page,
    { enabled = false, admin = false, viewer = false } = {},
) {
    await mockApi(page, { admin });
    if (viewer)
        await page.route("**/api/tenants/", (route) =>
            route.fulfill({
                json: paged([
                    { id: 1, name: "Sunfield Energy", role: "viewer" },
                ]),
            }),
        );
    const state = {
        enabled,
        setupRequests: 0,
        activations: [] as string[],
        removals: 0,
        recoveryRequests: [] as string[],
        recoveryCodes: ["10000001", "10000002", "10000003"],
        recoveryViewed: false,
    };
    await page.route(`**${authenticatorsPath}/**`, async (route) => {
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        const path = new URL(route.request().url()).pathname;
        const method = route.request().method();
        if (path.endsWith("/totp")) {
            if (method === "GET") {
                state.setupRequests += 1;
                await route.fulfill({
                    status: 404,
                    json: {
                        status: 404,
                        meta: { secret, totp_url: provisioningUri },
                    },
                });
            } else if (method === "POST") {
                state.activations.push(route.request().postDataJSON().code);
                state.enabled = true;
                await route.fulfill({ json: { status: 200 } });
            } else if (method === "DELETE") {
                state.removals += 1;
                state.enabled = false;
                await route.fulfill({ json: { status: 200 } });
            } else throw new Error(`Unexpected TOTP method: ${method}`);
        } else if (path.endsWith("/recovery-codes")) {
            state.recoveryRequests.push(method);
            if (method === "POST")
                state.recoveryCodes = ["20000001", "20000002", "20000003"];
            const unusedCodes =
                method === "GET" && state.recoveryViewed
                    ? []
                    : state.recoveryCodes;
            state.recoveryViewed = true;
            await route.fulfill({
                json: {
                    status: 200,
                    data: { unused_codes: unusedCodes },
                },
            });
        } else throw new Error(`Unexpected authenticator endpoint: ${path}`);
    });
    await page.route(`**${authenticatorsPath}`, (route) =>
        route.fulfill({
            json: {
                status: 200,
                data: state.enabled
                    ? [
                          { type: "totp" },
                          {
                              type: "recovery_codes",
                              unused_code_count: 3,
                              total_code_count: 3,
                          },
                      ]
                    : [],
            },
        }),
    );
    return state;
}

async function openSettings(page: Page) {
    await page.goto("/app/?workspace=customer");
    await page.getByRole("button", { name: "Account" }).click();
}

function twoFactorPanel(page: Page) {
    return page.locator("section").filter({
        has: page.getByRole("heading", {
            name: "Two-factor authentication",
            exact: true,
        }),
    });
}

test("a viewer enrolls from a local scannable QR and preserves a leading-zero code", async ({
    page,
}) => {
    const state = await mockTwoFactor(page, { viewer: true });
    let recentlyVerified = false;
    await page.route(`**${authenticatorsPath}/recovery-codes`, (route) => {
        if (recentlyVerified) return route.fallback();
        return route.fulfill({
            status: 401,
            json: {
                status: 401,
                meta: { is_authenticated: true },
                errors: [
                    {
                        code: "mfa_reauthentication_required",
                        message: "Verify an authenticator.",
                    },
                ],
            },
        });
    });
    await page.route(
        "**/api/auth/browser/v1/auth/2fa/reauthenticate",
        (route) => {
            expect(route.request().postDataJSON()).toEqual({ code: "012346" });
            recentlyVerified = true;
            return route.fulfill({ json: { status: 200 } });
        },
    );
    const leakedRequests: string[] = [];
    page.on("request", (request) => {
        const url = new URL(request.url());
        if (
            url.origin !== "http://127.0.0.1:3100" &&
            (request.url().includes(secret) ||
                request.url().includes("otpauth"))
        )
            leakedRequests.push(request.url());
    });
    await openSettings(page);
    await expect(
        page.getByRole("button", { name: "Show recovery codes" }),
    ).not.toBeVisible();
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    const qr = page.getByRole("img", { name: /QR/i });
    await expect(qr).toBeVisible();
    const png = PNG.sync.read(await qr.screenshot());
    expect(
        jsQR(new Uint8ClampedArray(png.data), png.width, png.height)?.data,
    ).toBe(provisioningUri);
    await page.getByText("Can't scan? Use setup key", { exact: true }).click();
    await expect(page.getByText(secret, { exact: true })).toBeVisible();
    await expect(
        twoFactorPanel(page).getByText("Enabled", { exact: true }),
    ).not.toBeVisible();
    await page.getByLabel("Authenticator code", { exact: true }).fill("012345");
    await page.getByRole("button", { name: "Activate authenticator" }).click();
    const panel = twoFactorPanel(page);
    await expect(
        panel.getByRole("heading", { name: "Confirm with your authenticator" }),
    ).toBeVisible();
    await expect(
        panel.getByText(/Wait for a new code in your app/),
    ).toBeVisible();
    await expect(page.getByText(secret, { exact: true })).not.toBeVisible();
    await panel.getByLabel("Authenticator or recovery code").fill("012346");
    await panel.getByRole("button", { name: "Confirm authenticator" }).click();
    await expect(
        page.getByRole("heading", { name: "Save your recovery codes" }),
    ).toBeVisible();
    expect(state.activations).toEqual(["012345"]);
    expect(state.recoveryRequests).toEqual(["GET"]);
    await expect(qr).not.toBeVisible();
    await expect(page.getByText(secret, { exact: true })).not.toBeVisible();
    expect(leakedRequests).toEqual([]);
    expect(
        await page.evaluate(() =>
            JSON.stringify({ ...localStorage, ...sessionStorage }),
        ),
    ).not.toContain(secret);
    await page
        .getByRole("button", { name: "I have saved my codes", exact: true })
        .click();
    await page.reload();
    await page.getByRole("button", { name: "Account" }).click();
    await expect(
        page.getByRole("button", { name: "Set up authenticator" }),
    ).not.toBeVisible();
    expect(state.setupRequests).toBe(1);
});

test("failed enrollment retains the key and a corrected code can be submitted", async ({
    page,
}) => {
    const state = await mockTwoFactor(page);
    let attempts = 0;
    await page.route(`**${authenticatorsPath}/totp`, async (route) => {
        if (route.request().method() === "GET") return route.fallback();
        attempts += 1;
        if (attempts === 1)
            return route.fulfill({
                status: 400,
                json: {
                    status: 400,
                    errors: [
                        {
                            code: "invalid",
                            param: "code",
                            message: "Invalid code.",
                        },
                    ],
                },
            });
        return route.fallback();
    });
    await openSettings(page);
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await page.getByText("Can't scan? Use setup key", { exact: true }).click();
    await page.getByLabel("Authenticator code", { exact: true }).fill("000000");
    await page.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(
        page.getByRole("alert").filter({ hasText: "Invalid code." }),
    ).toBeVisible();
    await expect(page.getByText(secret, { exact: true })).toBeVisible();
    await page.getByLabel("Authenticator code", { exact: true }).fill("012345");
    await page.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(
        page.getByRole("heading", { name: "Save your recovery codes" }),
    ).toBeVisible();
    expect(state.activations).toEqual(["012345"]);
});

test("cancelling setup clears the provisioning key without enabling MFA", async ({
    page,
}) => {
    const state = await mockTwoFactor(page);
    await openSettings(page);
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await page.getByText("Can't scan? Use setup key", { exact: true }).click();
    await expect(page.getByText(secret, { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(page.getByText(secret, { exact: true })).not.toBeVisible();
    await expect(page.getByRole("img", { name: /QR/i })).not.toBeVisible();
    await expect(
        page.getByRole("button", { name: "Set up authenticator" }),
    ).toBeVisible();
    expect(state.activations).toEqual([]);
});

test("activation prevents duplicate submissions while the server is responding", async ({
    page,
}) => {
    const state = await mockTwoFactor(page);
    let release: () => void = () => {};
    const responseReady = new Promise<void>((resolve) => {
        release = resolve;
    });
    let submissions = 0;
    await page.route(`**${authenticatorsPath}/totp`, async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        submissions += 1;
        await responseReady;
        state.enabled = true;
        await route.fulfill({ json: { status: 200 } });
    });
    await openSettings(page);
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await page.getByLabel("Authenticator code", { exact: true }).fill("012345");
    const activate = page.getByRole("button", {
        name: "Activate authenticator",
    });
    await activate.evaluate((button) => {
        (button as HTMLButtonElement).click();
        (button as HTMLButtonElement).click();
    });
    await expect(
        page.getByRole("button", { name: "Verifying…" }),
    ).toBeDisabled();
    release();
    await expect(
        page.getByRole("heading", { name: "Save your recovery codes" }),
    ).toBeVisible();
    expect(submissions).toBe(1);
});

test("MFA setup and recovery codes remain accessible on desktop and a 320px screen", async ({
    page,
}, testInfo) => {
    await mockTwoFactor(page);
    await openSettings(page);
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    for (const width of [1440, 320]) {
        await page.setViewportSize({ width, height: 900 });
        await expect(page.getByRole("img", { name: /QR/i })).toBeVisible();
        expect(
            await page.evaluate(
                () => document.documentElement.scrollWidth <= window.innerWidth,
            ),
        ).toBe(true);
        expect((await new AxeBuilder({ page }).analyze()).violations).toEqual(
            [],
        );
        await page.screenshot({
            path: testInfo.outputPath(`mfa-setup-${width}.png`),
            fullPage: true,
        });
    }
    await page.getByLabel("Authenticator code", { exact: true }).fill("012345");
    await page.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(page.getByText("10000001", { exact: true })).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await page.screenshot({
        path: testInfo.outputPath("mfa-recovery-320.png"),
        fullPage: true,
    });
});

test("recovery codes clear after saved acknowledgement and replace only after confirmation", async ({
    page,
}) => {
    const state = await mockTwoFactor(page, { enabled: true });
    await openSettings(page);
    await expect(page.getByText("3 of 3 recovery codes unused.")).toBeVisible();
    await page.getByRole("button", { name: "Show recovery codes" }).click();
    await expect(page.getByText("10000001", { exact: true })).toBeVisible();
    await page
        .getByRole("button", { name: "I have saved my codes", exact: true })
        .click();
    await expect(page.getByText("10000001", { exact: true })).not.toBeVisible();
    await page.getByRole("button", { name: "Show recovery codes" }).click();
    await expect(
        page.getByText(/Your recovery codes were already viewed/),
    ).toBeVisible();
    await page.getByRole("button", { name: "Replace recovery codes" }).click();
    await expect(
        page.getByRole("heading", { name: "Replace recovery codes?" }),
    ).toBeVisible();
    expect(state.recoveryRequests).toEqual(["GET", "GET"]);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(state.recoveryRequests).toEqual(["GET", "GET"]);
    await page.getByRole("button", { name: "Replace recovery codes" }).click();
    await page
        .getByRole("button", { name: "Replace codes", exact: true })
        .click();
    await expect(page.getByText("20000001", { exact: true })).toBeVisible();
    await expect(page.getByText("10000001", { exact: true })).not.toBeVisible();
    expect(state.recoveryRequests).toEqual(["GET", "GET", "POST"]);
    await page.reload();
    await page.getByRole("button", { name: "Account" }).click();
    await expect(page.getByText("20000001", { exact: true })).not.toBeVisible();
    expect(
        await page.evaluate(() =>
            JSON.stringify({ ...localStorage, ...sessionStorage }),
        ),
    ).not.toContain("20000001");
});

test("removing an optional authenticator requires confirmation and cancellation preserves it", async ({
    page,
}) => {
    const state = await mockTwoFactor(page, { enabled: true, viewer: true });
    await openSettings(page);
    await page
        .getByRole("button", { name: "Remove authenticator", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Remove authenticator?" }),
    ).toBeVisible();
    expect(state.removals).toBe(0);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page
        .getByRole("button", { name: "Remove authenticator", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Remove authenticator", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Set up authenticator" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Show recovery codes" }),
    ).not.toBeVisible();
    expect(state.removals).toBe(1);
});

test("a platform administrator sees required MFA without customer enrollment or removal controls", async ({
    page,
}) => {
    const state = await mockTwoFactor(page, { enabled: true, admin: true });
    await openSettings(page);
    await expect(
        page.getByText(
            "Two-factor authentication is required for this account.",
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Remove authenticator" }),
    ).not.toBeVisible();
    await expect(
        page.getByRole("button", { name: "Set up authenticator" }),
    ).not.toBeVisible();
    await expect(
        page.getByRole("button", { name: "Show recovery codes" }),
    ).toBeVisible();
    expect(state.removals).toBe(0);
});

test("a failed authenticator list offers an explicit retry without guessing account status", async ({
    page,
}) => {
    await mockTwoFactor(page, { enabled: true });
    let unavailable = true;
    await page.route(`**${authenticatorsPath}`, (route) => {
        if (unavailable)
            return route.fulfill({
                status: 503,
                json: {
                    status: 503,
                    errors: [{ message: "Security settings unavailable." }],
                },
            });
        return route.fallback();
    });
    await openSettings(page);
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "Security settings unavailable." }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Set up authenticator" }),
    ).not.toBeVisible();
    unavailable = false;
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Show recovery codes" }),
    ).toBeVisible();
});

test("password reauthentication resumes setup and cancellation makes no enrollment request", async ({
    page,
}) => {
    const state = await mockTwoFactor(page);
    let verified = false;
    await page.route(`**${authenticatorsPath}/totp`, (route) => {
        if (verified) return route.fallback();
        return route.fulfill({
            status: 401,
            json: {
                status: 401,
                meta: { is_authenticated: true },
                data: { flows: [{ id: "reauthenticate", is_pending: true }] },
            },
        });
    });
    await page.route("**/api/auth/browser/v1/auth/reauthenticate", (route) => {
        expect(route.request().postDataJSON()).toEqual({
            password: "Test-password-123!",
        });
        verified = true;
        return route.fulfill({ json: { status: 200 } });
    });
    await openSettings(page);
    const panel = twoFactorPanel(page);
    await panel.getByRole("button", { name: "Set up authenticator" }).click();
    await expect(
        panel.getByRole("heading", { name: "Confirm your password" }),
    ).toBeVisible();
    await expect(panel.getByText("Enabled", { exact: true })).not.toBeVisible();
    await panel.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(state.setupRequests).toBe(0);
    await panel.getByRole("button", { name: "Set up authenticator" }).click();
    await panel
        .getByLabel("Current password", { exact: true })
        .fill("Test-password-123!");
    await panel
        .getByRole("button", { name: "Confirm password", exact: true })
        .click();
    await expect(panel.getByRole("img", { name: /QR/i })).toBeVisible();
    expect(state.setupRequests).toBe(1);
});

for (const resumeAllowed of [true, false]) {
    test(`MFA recovery reauthentication ${resumeAllowed ? "resumes the pending request" : "stops after one rejected replay"}`, async ({
        page,
    }) => {
        await mockTwoFactor(page, { enabled: true });
        let verified = false;
        let recoveryRequests = 0;
        let verifications = 0;
        await page.route(`**${authenticatorsPath}/recovery-codes`, (route) => {
            recoveryRequests += 1;
            if (verified && resumeAllowed) return route.fallback();
            return route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    meta: { is_authenticated: true },
                    errors: [
                        {
                            code: "mfa_reauthentication_required",
                            message:
                                "Verify an authenticator before managing two-factor authentication.",
                        },
                    ],
                },
            });
        });
        await page.route(
            "**/api/auth/browser/v1/auth/2fa/reauthenticate",
            (route) => {
                expect(route.request().postDataJSON()).toEqual({
                    code: "000001",
                });
                verifications += 1;
                verified = true;
                return route.fulfill({ json: { status: 200 } });
            },
        );
        await openSettings(page);
        const panel = twoFactorPanel(page);
        await panel
            .getByRole("button", { name: "Show recovery codes" })
            .click();
        await expect(
            panel.getByRole("heading", {
                name: "Confirm with your authenticator",
            }),
        ).toBeVisible();
        await expect(
            page.getByRole("heading", { name: "Account settings" }),
        ).toBeVisible();
        await panel.getByLabel("Authenticator or recovery code").fill("000001");
        await panel
            .getByRole("button", { name: "Confirm authenticator" })
            .click();
        if (resumeAllowed)
            await expect(
                panel.getByText("10000001", { exact: true }),
            ).toBeVisible();
        else {
            await expect(
                panel.getByRole("alert").filter({
                    hasText:
                        "Verification could not be completed. Please try again.",
                }),
            ).toBeVisible();
            await expect(
                panel.getByText("10000001", { exact: true }),
            ).not.toBeVisible();
            await expect(
                panel.getByRole("button", { name: "Show recovery codes" }),
            ).toBeVisible();
        }
        expect(recoveryRequests).toBe(2);
        expect(verifications).toBe(1);
    });
}

test("recovery codes can be copied or downloaded without persisting them in browser storage", async ({
    page,
}) => {
    await mockTwoFactor(page, { enabled: true });
    await page.addInitScript(() => {
        Object.defineProperty(navigator, "clipboard", {
            configurable: true,
            value: {
                writeText: async (text: string) => {
                    (window as Window & { copiedText?: string }).copiedText =
                        text;
                },
            },
        });
    });
    await openSettings(page);
    await page.getByRole("button", { name: "Show recovery codes" }).click();
    await page.getByRole("button", { name: "Copy codes", exact: true }).click();
    await expect(
        page.getByText("Recovery codes copied.", { exact: true }),
    ).toBeVisible();
    expect(
        await page.evaluate(
            () => (window as Window & { copiedText?: string }).copiedText,
        ),
    ).toBe("10000001\n10000002\n10000003");
    const downloading = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download (.txt)", exact: true })
        .click();
    const download = await downloading;
    expect(download.suggestedFilename()).toBe("pyracast-recovery-codes.txt");
    expect(await readFile((await download.path())!, "utf8")).toBe(
        "10000001\n10000002\n10000003\n",
    );
    expect(
        await page.evaluate(() =>
            JSON.stringify({ ...localStorage, ...sessionStorage }),
        ),
    ).not.toContain("10000001");
});

test("switching language preserves the in-progress QR, code draft and shown-once recovery codes", async ({
    page,
}) => {
    const state = await mockTwoFactor(page);
    await page.route("**/api/session/language/", (route) => {
        const language = route.request().postDataJSON().language as string;
        return route.fulfill({
            json: {
                user: { ...user, language_preference: language },
                csrf_token: "signed-token",
                language,
            },
        });
    });
    await openSettings(page);
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await page.getByText("Can't scan? Use setup key", { exact: true }).click();
    const panel = page
        .locator("section")
        .filter({ has: page.locator(".mfa-header") });
    await page.getByLabel("Authenticator code", { exact: true }).fill("012345");
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    await expect(panel.locator("svg[role='img']")).toBeVisible();
    await expect(panel.locator("input[name='code']")).toHaveValue("012345");
    await expect(page.getByText(secret, { exact: true })).toBeVisible();
    expect(state.setupRequests).toBe(1);
    await page.getByLabel("Dil", { exact: true }).selectOption("en");
    await page.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(page.getByText("10000001", { exact: true })).toBeVisible();
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    await expect(page.getByText("10000001", { exact: true })).toBeVisible();
    expect(state.recoveryRequests).toEqual(["GET"]);
});

test("an activation interrupted by password reauthentication requires a fresh OTP", async ({
    page,
}) => {
    const state = await mockTwoFactor(page);
    let verified = false;
    let activations = 0;
    await page.route(`**${authenticatorsPath}/totp`, (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        activations += 1;
        if (verified) return route.fallback();
        return route.fulfill({
            status: 401,
            json: {
                status: 401,
                meta: { is_authenticated: true },
                data: { flows: [{ id: "reauthenticate", is_pending: true }] },
            },
        });
    });
    await page.route("**/api/auth/browser/v1/auth/reauthenticate", (route) => {
        verified = true;
        return route.fulfill({ json: { status: 200 } });
    });
    await openSettings(page);
    const panel = twoFactorPanel(page);
    await panel.getByRole("button", { name: "Set up authenticator" }).click();
    await panel
        .getByLabel("Authenticator code", { exact: true })
        .fill("012345");
    await panel.getByRole("button", { name: "Activate authenticator" }).click();
    await panel
        .getByLabel("Current password", { exact: true })
        .fill("Test-password-123!");
    await panel
        .getByRole("button", { name: "Confirm password", exact: true })
        .click();
    await expect(
        panel.getByLabel("Authenticator code", { exact: true }),
    ).toHaveValue("");
    expect(activations).toBe(1);
    expect(state.activations).toEqual([]);
    await panel
        .getByLabel("Authenticator code", { exact: true })
        .fill("012346");
    await panel.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(
        panel.getByRole("heading", { name: "Save your recovery codes" }),
    ).toBeVisible();
    expect(state.activations).toEqual(["012346"]);
});

test("refreshing a masked CSRF token preserves displayed recovery codes", async ({
    page,
}) => {
    const state = await mockTwoFactor(page, { enabled: true });
    let refreshed = false;
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: {
                    ...user,
                    username: refreshed ? "solar.refreshed" : user.username,
                },
                csrf_token: refreshed ? "fresh-masked-token" : "signed-token",
            },
        }),
    );
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) => {
        refreshed = true;
        return route.fulfill({ json: { status: 200 } });
    });
    await openSettings(page);
    await page.getByRole("button", { name: "Show recovery codes" }).click();
    await expect(page.getByText("10000001", { exact: true })).toBeVisible();
    await page.getByText("Change email", { exact: true }).click();
    await page.getByLabel("Email verification code").fill("ABCD-EFGH");
    await page.getByRole("button", { name: "Verify new email" }).click();
    await expect(
        page.getByText("solar.refreshed", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("10000001", { exact: true })).toBeVisible();
    await expect(
        page.getByRole("button", {
            name: "I have saved my codes",
            exact: true,
        }),
    ).toBeVisible();
    expect(state.recoveryRequests).toEqual(["GET"]);
});

for (const operation of ["remove", "replace"] as const) {
    test(`leaving settings during reauthentication cannot resume pending ${operation}`, async ({
        page,
    }) => {
        const state = await mockTwoFactor(page, { enabled: true });
        let verified = false;
        let resumedMutations = 0;
        const endpoint = operation === "remove" ? "totp" : "recovery-codes";
        const method = operation === "remove" ? "DELETE" : "POST";
        await page.route(`**${authenticatorsPath}/${endpoint}`, (route) => {
            if (route.request().method() !== method) return route.fallback();
            if (verified) {
                resumedMutations += 1;
                return route.fallback();
            }
            return route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    meta: { is_authenticated: true },
                    errors: [
                        {
                            code: "mfa_reauthentication_required",
                            message: "Verify an authenticator.",
                        },
                    ],
                },
            });
        });
        let release: () => void = () => {};
        const authenticationReady = new Promise<void>((resolve) => {
            release = resolve;
        });
        await page.route(
            "**/api/auth/browser/v1/auth/2fa/reauthenticate",
            async (route) => {
                await authenticationReady;
                verified = true;
                await route.fulfill({ json: { status: 200 } });
            },
        );
        await openSettings(page);
        const panel = twoFactorPanel(page);
        await panel
            .getByRole("button", {
                name:
                    operation === "remove"
                        ? "Remove authenticator"
                        : "Replace recovery codes",
                exact: true,
            })
            .click();
        await panel
            .getByRole("button", {
                name:
                    operation === "remove"
                        ? "Remove authenticator"
                        : "Replace codes",
                exact: true,
            })
            .click();
        await panel.getByLabel("Authenticator or recovery code").fill("012345");
        const authenticating = page.waitForRequest(
            "**/api/auth/browser/v1/auth/2fa/reauthenticate",
        );
        const authenticated = page.waitForResponse(
            "**/api/auth/browser/v1/auth/2fa/reauthenticate",
        );
        await panel
            .getByRole("button", { name: "Confirm authenticator" })
            .click();
        await authenticating;
        await page.getByRole("button", { name: "Plants", exact: true }).click();
        await expect(
            page.getByRole("heading", { name: "Plants", exact: true }),
        ).toBeVisible();
        release();
        await (await authenticated).finished();
        await page.getByRole("button", { name: "Account" }).click();
        await expect(
            twoFactorPanel(page).getByRole("button", {
                name: "Show recovery codes",
            }),
        ).toBeVisible();
        expect(resumedMutations).toBe(0);
        expect(state.removals).toBe(0);
        expect(state.recoveryRequests).toEqual([]);
    });
}

test("an activation committed before a lost response reconciles the actual enabled status", async ({
    page,
}) => {
    const state = await mockTwoFactor(page);
    let activations = 0;
    await page.route(`**${authenticatorsPath}/totp`, async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        activations += 1;
        state.enabled = true;
        await route.abort("failed");
    });
    await openSettings(page);
    const panel = twoFactorPanel(page);
    await panel.getByRole("button", { name: "Set up authenticator" }).click();
    await panel
        .getByLabel("Authenticator code", { exact: true })
        .fill("012345");
    await panel.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(panel.getByText("Enabled", { exact: true })).toBeVisible();
    await expect(panel.getByRole("alert")).toHaveText(/\S/);
    await expect(
        panel.getByRole("button", { name: "Set up authenticator" }),
    ).not.toBeVisible();
    await panel.getByRole("button", { name: "Show recovery codes" }).click();
    await expect(panel.getByText("10000001", { exact: true })).toBeVisible();
    expect(activations).toBe(1);
});

test("a malformed authenticator list offers retry without claiming MFA is absent", async ({
    page,
}) => {
    await mockTwoFactor(page, { enabled: true });
    let malformed = true;
    await page.route(`**${authenticatorsPath}`, (route) =>
        malformed
            ? route.fulfill({ json: { status: 200, data: { type: "totp" } } })
            : route.fallback(),
    );
    await openSettings(page);
    const panel = twoFactorPanel(page);
    await expect(panel.getByRole("alert")).toBeVisible();
    await expect(
        panel.getByText("Not set up", { exact: true }),
    ).not.toBeVisible();
    await expect(
        panel.getByRole("button", { name: "Set up authenticator" }),
    ).not.toBeVisible();
    malformed = false;
    await panel.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(panel.getByText("Enabled", { exact: true })).toBeVisible();
});

test("a rejected enrollment code preserves the QR without refetching authenticator status", async ({
    page,
}) => {
    await mockTwoFactor(page);
    let listRequests = 0;
    let enrollmentStarted = false;
    await page.route(`**${authenticatorsPath}`, (route) => {
        if (!enrollmentStarted) return route.fallback();
        listRequests += 1;
        return route.fulfill({
            status: 500,
            json: { status: 500, errors: [{ message: "Status unavailable." }] },
        });
    });
    await page.route(`**${authenticatorsPath}/totp`, (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        return route.fulfill({
            status: 400,
            json: {
                status: 400,
                errors: [
                    {
                        code: "invalid",
                        param: "code",
                        message: "Incorrect authenticator code.",
                    },
                ],
            },
        });
    });
    await openSettings(page);
    const panel = twoFactorPanel(page);
    await panel.getByRole("button", { name: "Set up authenticator" }).click();
    await expect(panel.getByRole("img", { name: /QR/i })).toBeVisible();
    enrollmentStarted = true;
    await panel
        .getByLabel("Authenticator code", { exact: true })
        .fill("000000");
    await panel.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(
        panel
            .getByRole("alert")
            .filter({ hasText: "Incorrect authenticator code." }),
    ).toBeVisible();
    await expect(panel.getByRole("img", { name: /QR/i })).toBeVisible();
    await expect(
        panel.getByLabel("Authenticator code", { exact: true }),
    ).toHaveValue("000000");
    expect(listRequests).toBe(0);
});
