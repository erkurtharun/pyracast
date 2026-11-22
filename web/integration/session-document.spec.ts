import { expect, test, type Page } from "@playwright/test";

async function signIn(page: Page, email: string) {
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page
        .getByLabel("Password", { exact: true })
        .fill(process.env.BROWSER_PASSWORD!);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
}

const workspaces = (page: Page) =>
    page.getByRole("combobox", { name: "Workspace" }).locator("option");

test("a sign-in on an open sign-in screen loads a fresh document, and the next account sees none of the previous one", async ({
    page,
}) => {
    await page.goto("/app/");
    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
    await page.evaluate(() => Object.assign(window, { documentMarker: true }));
    await signIn(page, process.env.BROWSER_EMAIL!);
    expect(await page.evaluate(() => "documentMarker" in window)).toBe(false);
    await expect(workspaces(page)).toHaveText(["Tenant"]);
    await expect(page.getByRole("button", { name: /Site/ })).toBeVisible();

    await page.evaluate(() => Object.assign(window, { documentMarker: true }));
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    expect(await page.evaluate(() => "documentMarker" in window)).toBe(false);
    await signIn(page, process.env.BROWSER_OTHER_EMAIL!);
    await expect(workspaces(page)).toHaveText(["Other"]);
    await expect(page.getByRole("button", { name: /Private/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /Site/ })).toHaveCount(0);
    await expect(page.getByText("Tenant", { exact: true })).toHaveCount(0);
});
