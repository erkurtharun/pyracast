import { expect, test, type Page } from "@playwright/test";
import { mailedCode } from "./support/mailbox";

async function pendingFlows(page: Page) {
    const answer = await page.request.get("/api/auth/browser/v1/auth/session");
    const body = (await answer.json()) as {
        data: { flows: { id: string; is_pending?: boolean }[] };
    };
    return body.data.flows
        .filter((flow) => flow.is_pending)
        .map((flow) => flow.id);
}

async function requestReset(page: Page, email: string) {
    await page
        .getByRole("button", { name: "Forgot password?", exact: true })
        .click();
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByRole("button", { name: "Send reset code" }).click();
    await expect(
        page.getByText(
            `If an account uses ${email}, we sent a code to it. Enter the code and choose a new password.`,
            { exact: true },
        ),
    ).toBeVisible();
}

async function submitResetCode(page: Page, status: number) {
    await page.getByLabel("Email code").fill("WRONG-CODE");
    await page
        .getByLabel("Password", { exact: true })
        .fill(`${process.env.BROWSER_SIGNUP_PASSWORD!}-new`);
    const answered = page.waitForResponse((response) =>
        response.url().endsWith("/auth/password/reset"),
    );
    await page
        .getByRole("button", { name: "Reset password", exact: true })
        .click();
    expect((await answered).status()).toBe(status);
}

async function signOut(page: Page) {
    const signedOut = page.waitForResponse(
        (response) =>
            response.request().method() === "GET" &&
            new URL(response.url()).pathname === "/api/session/",
    );
    await page.getByRole("button", { name: "Sign out" }).click();
    expect(((await (await signedOut).json()) as { user: unknown }).user).toBe(
        null,
    );
}

test("a new company signs up and verifies its email, leaves and loses a password reset, and signs in again", async ({
    page,
}) => {
    test.setTimeout(60_000);
    const email = process.env.BROWSER_SIGNUP_EMAIL!;
    const password = process.env.BROWSER_SIGNUP_PASSWORD!;
    await page.goto("/app/");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await page.getByLabel("Company name").fill("Browser signup company");
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Create company account" }).click();
    await expect(
        page.getByRole("heading", { name: "Verify your email" }),
    ).toBeVisible();
    await expect(
        page.getByText(
            `We sent a code to ${email}. Enter it here to confirm the address.`,
        ),
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
    expect(
        ((await signedIn.json()) as { user: { email: string } }).user.email,
    ).toBe(email);
    await signOut(page);

    await requestReset(page, email);
    expect(await pendingFlows(page)).toEqual(["password_reset_by_code"]);
    await page
        .getByRole("button", { name: "Cancel password reset", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    expect(await pendingFlows(page)).toEqual([]);

    await requestReset(page, email);
    for (const status of [400, 400, 400, 409])
        await submitResetCode(page, status);
    await expect(
        page.getByRole("heading", { name: "This step has ended" }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Start again", exact: true })
        .click();
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await signOut(page);

    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
    await page.reload();
    await expect(
        page
            .locator("form")
            .getByRole("button", { name: "Sign in", exact: true }),
    ).toBeVisible();
    const session = await page.request.get("/api/session/");
    expect(((await session.json()) as { user: unknown }).user).toBeNull();
});
