import { expect, test } from "@playwright/test";
import { mailedCode } from "./support/mailbox";

test("a new company signs up, verifies its email, reaches its workspace and signs out", async ({
    page,
}) => {
    test.setTimeout(60_000);
    const email = process.env.BROWSER_SIGNUP_EMAIL!;
    await page.goto("/app/");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await page.getByLabel("Company name").fill("Browser signup company");
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page
        .getByLabel("Password", { exact: true })
        .fill(process.env.BROWSER_SIGNUP_PASSWORD!);
    await page.getByRole("button", { name: "Create company account" }).click();
    await expect(
        page.getByRole("heading", { name: "Verify your email" }),
    ).toBeVisible();
    await page
        .getByLabel("Email code")
        .fill(await mailedCode(email, "email_confirmation"));
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    const signedIn = await page.request.get("/api/session/");
    expect((await signedIn.json()).user.email).toBe(email);

    const signedOut = page.waitForResponse(
        (response) =>
            response.request().method() === "GET" &&
            new URL(response.url()).pathname === "/api/session/",
    );
    await page.getByRole("button", { name: "Sign out" }).click();
    expect((await (await signedOut).json()).user).toBeNull();
    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
    await page.reload();
    await expect(
        page
            .locator("form")
            .getByRole("button", { name: "Sign in", exact: true }),
    ).toBeVisible();
    const session = await page.request.get("/api/session/");
    expect((await session.json()).user).toBeNull();
});
