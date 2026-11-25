import { expect, test, type Page } from "@playwright/test";

async function submitSignIn(page: Page, email: string) {
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page
        .getByLabel("Password", { exact: true })
        .fill(process.env.BROWSER_PASSWORD!);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
}

const markDocument = (page: Page) =>
    page.evaluate(() => Object.assign(window, { documentMarker: true }));
const isMarked = (page: Page) =>
    page.evaluate(() => "documentMarker" in window);
const workspaces = (page: Page) =>
    page.getByRole("combobox", { name: "Workspace" });

test("a sign-in on an open sign-in screen loads a fresh document, and the next account sees none of the previous one", async ({
    page,
}) => {
    await page.goto("/app/");
    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
    await markDocument(page);
    await submitSignIn(page, process.env.BROWSER_EMAIL!);
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    expect(await isMarked(page)).toBe(false);
    await expect(workspaces(page).locator("option")).toHaveText(["Tenant"]);
    await expect(page.getByRole("button", { name: /Site/ })).toBeVisible();

    await markDocument(page);
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    expect(await isMarked(page)).toBe(false);

    await markDocument(page);
    await submitSignIn(page, process.env.BROWSER_OTHER_EMAIL!);
    await expect(
        page.getByText("You do not have access to this workspace."),
    ).toBeVisible();
    expect(await isMarked(page)).toBe(false);
    await expect(page.getByRole("button", { name: /Site/ })).toHaveCount(0);
    await workspaces(page).selectOption({ label: "Other" });
    await expect(page.getByRole("button", { name: /Private/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /Site/ })).toHaveCount(0);
    await expect(workspaces(page).locator("option")).toHaveText(["Other"]);
    await expect(page.getByText("Tenant", { exact: true })).toHaveCount(0);
});
