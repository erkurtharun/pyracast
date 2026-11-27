import { expect, test, type Page } from "@playwright/test";

async function signInWithPassword(page: Page, email: string, password: string) {
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
}

test("a customer enrolls, signs in, verifies and removes a passkey", async ({
    page,
}) => {
    test.setTimeout(120_000);
    const email = process.env.BROWSER_PASSKEY_EMAIL!;
    const password = process.env.BROWSER_PASSKEY_PASSWORD!;
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("WebAuthn.enable");
    const { authenticatorId } = await cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            options: {
                protocol: "ctap2",
                transport: "internal",
                hasResidentKey: true,
                hasUserVerification: true,
                isUserVerified: true,
                automaticPresenceSimulation: true,
            },
        },
    );

    await page.goto("/app/");
    await signInWithPassword(page, email, password);
    await page.getByRole("button", { name: "Account", exact: true }).click();

    const settings = page.getByRole("region", { name: "Passkeys" });
    await settings.getByLabel("Passkey name (optional)").fill("Test device");
    await settings.getByRole("button", { name: "Add a passkey" }).click();
    await settings.getByLabel("Current password").fill(password);
    await settings.getByRole("button", { name: "Confirm password" }).click();
    await expect(
        settings.getByRole("heading", { name: "Save your recovery codes" }),
    ).toBeVisible();
    const recoveryCode = await settings.locator("li code").first().innerText();
    await settings
        .getByRole("button", { name: "I have saved my codes" })
        .click();

    const remove = settings.getByRole("button", {
        name: "Remove passkey Test device",
    });
    const signOut = page.getByRole("button", { name: "Sign out" });
    await expect(remove).toBeVisible();
    await signOut.click();
    await page.getByRole("button", { name: "Sign in with a passkey" }).click();
    await expect(signOut).toBeVisible();

    await signOut.click();
    await signInWithPassword(page, email, password);
    await expect(
        page.getByRole("heading", { name: "Two-step verification" }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Confirm with a passkey", exact: true })
        .click();
    await expect(signOut).toBeVisible();

    await signOut.click();
    await signInWithPassword(page, email, password);
    await page
        .getByLabel("Authenticator code or recovery code")
        .fill(recoveryCode);
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(signOut).toBeVisible();

    if (!(await settings.isVisible()))
        await page
            .getByRole("button", { name: "Account", exact: true })
            .click();
    await remove.click();
    await settings
        .getByRole("button", { name: "Confirm with a passkey", exact: true })
        .click();
    await expect(
        settings.getByText("This account has no passkeys."),
    ).toBeVisible();
    await cdp.send("WebAuthn.removeVirtualAuthenticator", { authenticatorId });
    await cdp.detach();
});
