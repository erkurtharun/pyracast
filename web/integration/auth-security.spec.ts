import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { totp } from "./support/totp";

test("verified email and a recovery code protect a real browser session", async ({
    page,
}) => {
    test.setTimeout(120_000);
    const email = process.env.BROWSER_SECURITY_EMAIL!;
    const password = process.env.BROWSER_SECURITY_PASSWORD!;
    await page.goto("/");
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
    await page.getByRole("button", { name: "Account settings" }).click();
    await page
        .getByLabel("Current password", { exact: true })
        .first()
        .fill(password);
    await page.getByRole("button", { name: "Confirm password" }).click();
    await expect(page.getByText("Identity confirmed.")).toBeVisible();
    await page.getByRole("button", { name: "Set up authenticator" }).click();
    const setupKey = page.getByText("Setup key:").locator("code");
    await expect(setupKey).toBeVisible();
    const code = totp(await setupKey.innerText());
    await page.getByLabel("Authenticator code", { exact: true }).fill(code);
    await page.getByRole("button", { name: "Activate authenticator" }).click();
    await expect(setupKey).not.toBeVisible();
    await page.getByRole("button", { name: "Show recovery codes" }).click();
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
    await page.getByRole("button", { name: "Sign out" }).click();
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("heading", { name: "Two-factor authentication" }),
    ).toBeVisible();
    await page.getByLabel("Authenticator or recovery code").fill(recoveryCode);
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Account settings" }).click();
    const newPassword = `${password}-new`;
    await page
        .getByLabel("Current password", { exact: true })
        .last()
        .fill(password);
    await page.getByLabel("New password", { exact: true }).fill(newPassword);
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.locator("form").getByRole("alert")).toBeVisible();
    await page.getByLabel("Password", { exact: true }).fill(newPassword);
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("heading", { name: "Two-factor authentication" }),
    ).toBeVisible();
    await page
        .getByLabel("Authenticator or recovery code")
        .fill(secondRecoveryCode);
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
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
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("heading", { name: "Two-factor authentication" }),
    ).toBeVisible();
    await page
        .getByLabel("Authenticator or recovery code")
        .fill(thirdRecoveryCode);
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});
