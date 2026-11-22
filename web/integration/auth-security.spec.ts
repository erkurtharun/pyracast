import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { totp } from "./support/totp";

test("verified email and a recovery code protect a real browser session", async ({
    page,
}) => {
    test.setTimeout(120_000);
    const email = process.env.BROWSER_SECURITY_EMAIL!;
    const password = process.env.BROWSER_SECURITY_PASSWORD!;
    await page.goto("/app/");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await page.getByLabel("Company name").fill("Browser security company");
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Create company account" }).click();
    await expect(
        page.getByRole("heading", { name: "Verify your email" }),
    ).toBeVisible();
    let verificationCode = "";
    await expect
        .poll(async () => {
            try {
                const messages = JSON.parse(
                    await readFile(process.env.BROWSER_AUTH_MAILBOX!, "utf8"),
                ) as { email: string; template: string; code: string }[];
                verificationCode =
                    messages.findLast(
                        (message) =>
                            message.email === email &&
                            message.template.includes("email_confirmation"),
                    )?.code ?? "";
            } catch {
                /* Mailbox may not exist until the first message commits. */
            }
            return !!verificationCode;
        })
        .toBe(true);
    await page.getByLabel("Email code").fill(verificationCode);
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    const linkedAccountsRead = page.waitForEvent(
        "requestfinished",
        (request) =>
            request.url().endsWith("/api/auth/browser/v1/account/providers") &&
            request.method() === "GET",
    );
    await page.getByRole("button", { name: "Account" }).click();
    expect((await (await linkedAccountsRead).response())?.status()).toBe(200);
    const twoFactor = page.locator("section").filter({
        has: page.getByRole("heading", {
            name: "Two-factor authentication",
            exact: true,
        }),
    });
    await expect(
        twoFactor.getByRole("button", { name: "Set up authenticator" }),
    ).toBeVisible();
    await expect(
        page
            .getByRole("region", { name: "Connected accounts" })
            .getByRole("button", { name: "Connect an account" }),
    ).toBeVisible();
    const sessionResponse = await page.request.get("/api/session/");
    expect(sessionResponse.status()).toBe(200);
    const session = (await sessionResponse.json()) as { csrf_token: string };
    const confirmation = await page.request.post(
        "/api/auth/browser/v1/auth/reauthenticate",
        {
            headers: { "X-CSRFToken": session.csrf_token },
            data: { password },
        },
    );
    expect(confirmation.status()).toBe(200);
    expect(await confirmation.json()).toMatchObject({
        status: 200,
        meta: { is_authenticated: true },
    });
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    await page.getByText("Can't scan? Use setup key", { exact: true }).click();
    const setupKey = page.locator(".mfa-secret-code");
    await expect(setupKey).toBeVisible();
    const setupSecret = await setupKey.innerText();
    // The server's authenticator clock is pinned to this instant, so the code cannot cross a step.
    const code = totp(setupSecret, Number(process.env.BROWSER_TOTP_NOW) * 1000);
    await page.getByLabel("Authenticator code", { exact: true }).fill(code);
    await page.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(setupKey).not.toBeVisible();
    const firstCode = page.locator("main li code").first();
    await expect(firstCode).toBeVisible();
    const recoveryCode = await firstCode.innerText();
    const secondRecoveryCode = await page
        .locator("main li code")
        .nth(1)
        .innerText();
    const thirdRecoveryCode = await page
        .locator("main li code")
        .nth(2)
        .innerText();
    const fourthRecoveryCode = await page
        .locator("main li code")
        .nth(3)
        .innerText();
    const fifthRecoveryCode = await page
        .locator("main li code")
        .nth(4)
        .innerText();
    await page.getByRole("button", { name: "Sign out" }).click();
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Two-factor authentication" }),
    ).toBeVisible();
    await page.getByLabel("Authenticator or recovery code").fill(recoveryCode);
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
    const newPassword = `${password}-new`;
    const passwordSettings = page.locator("section").filter({
        has: page.getByRole("heading", {
            name: "Change password",
            exact: true,
        }),
    });
    await passwordSettings.locator("summary").click();
    await passwordSettings
        .getByLabel("Current password", { exact: true })
        .fill(password);
    await page.getByLabel("New password", { exact: true }).fill(newPassword);
    await page.getByRole("button", { name: "Change password" }).click();
    await passwordSettings
        .getByLabel("Authenticator or recovery code")
        .fill(fourthRecoveryCode);
    await passwordSettings
        .getByRole("button", { name: "Confirm authenticator" })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(page.locator("form").getByRole("alert")).toBeVisible();
    await page.getByLabel("Password", { exact: true }).fill(newPassword);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Two-factor authentication" }),
    ).toBeVisible();
    await page
        .getByLabel("Authenticator or recovery code")
        .fill(secondRecoveryCode);
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
    await page.getByRole("button", { name: "Forgot password?" }).click();
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByRole("button", { name: "Send reset code" }).click();
    await expect(
        page.getByRole("heading", { name: "Choose a new password" }),
    ).toBeVisible();
    let resetCode = "";
    await expect
        .poll(async () => {
            try {
                const messages = JSON.parse(
                    await readFile(process.env.BROWSER_AUTH_MAILBOX!, "utf8"),
                ) as { email: string; template: string; code: string }[];
                resetCode =
                    messages.findLast(
                        (message) =>
                            message.email === email &&
                            message.template.includes("password_reset"),
                    )?.code ?? "";
            } catch {
                /* Mailbox may be written while it is being read. */
            }
            return !!resetCode;
        })
        .toBe(true);
    const resetPassword = `${password}-reset`;
    await page.getByLabel("Email code").fill(resetCode);
    await page.getByLabel("Password", { exact: true }).fill(resetPassword);
    await page
        .getByRole("button", { name: "Reset password", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(resetPassword);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Two-factor authentication" }),
    ).toBeVisible();
    await page
        .getByLabel("Authenticator or recovery code")
        .fill(thirdRecoveryCode);
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
    const changedEmail = email.replace("@", "+changed@");
    const emailChange = page.locator(".account-disclosure").filter({
        has: page.getByText("Change email", { exact: true }),
    });
    await emailChange.getByText("Change email", { exact: true }).click();
    await emailChange.getByLabel("New email address").fill(changedEmail);
    await emailChange
        .getByRole("button", { name: "Send verification code" })
        .click();
    await emailChange
        .getByLabel("Authenticator or recovery code")
        .fill(fifthRecoveryCode);
    await emailChange
        .getByRole("button", { name: "Confirm authenticator" })
        .click();
    await expect(emailChange.getByText(changedEmail)).toBeVisible();
    let changeCode = "";
    await expect
        .poll(async () => {
            try {
                const messages = JSON.parse(
                    await readFile(process.env.BROWSER_AUTH_MAILBOX!, "utf8"),
                ) as { email: string; template: string; code: string }[];
                changeCode =
                    messages.findLast(
                        (message) =>
                            message.email === changedEmail &&
                            message.template.includes("email_confirmation"),
                    )?.code ?? "";
            } catch {
                /* Mailbox may be written while it is being read. */
            }
            return !!changeCode;
        })
        .toBe(true);
    await emailChange.getByLabel("Email verification code").fill(changeCode);
    await emailChange.getByRole("button", { name: "Verify new email" }).click();
    await expect(
        emailChange.getByText(`${changedEmail} is now your address.`),
    ).toBeVisible();
});
