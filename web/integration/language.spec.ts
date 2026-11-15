import { expect, test } from "@playwright/test";

test("account language survives sign-in from another browser without a preference cookie", async ({
    page,
    browser,
    baseURL,
}) => {
    test.setTimeout(60_000);
    await page.goto("/app/");
    await page
        .getByLabel("Email", { exact: true })
        .fill(process.env.BROWSER_EMAIL!);
    await page
        .getByLabel("Password", { exact: true })
        .fill(process.env.BROWSER_PASSWORD!);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(
        page.getByRole("heading", {
            name: "Plants",
            exact: true,
        }),
    ).toBeVisible();
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(
        page.getByRole("heading", {
            name: "Santraller",
            exact: true,
        }),
    ).toBeVisible();
    const saved = await page.request.get("/api/session/");
    const preference = await saved.json();
    expect(preference.user).not.toBeNull();
    expect(preference).toMatchObject({
        language: "tr",
        user: { language_preference: "tr" },
    });
    const second = await browser.newContext({ baseURL, locale: "en-US" });
    try {
        const fresh = await second.newPage();
        await fresh.goto("/");
        await fresh
            .getByLabel("Email", { exact: true })
            .fill(process.env.BROWSER_EMAIL!);
        await fresh
            .getByLabel("Password", { exact: true })
            .fill(process.env.BROWSER_PASSWORD!);
        await fresh
            .locator("form")
            .getByRole("button", { name: "Sign in", exact: true })
            .click();
        await expect(
            fresh.getByRole("heading", {
                name: "Santraller",
                exact: true,
            }),
        ).toBeVisible();
        await expect(fresh.locator("html")).toHaveAttribute("lang", "tr");
        const session = await fresh.request.get("/api/session/");
        expect(await session.json()).toMatchObject({
            language: "tr",
            user: { language_preference: "tr" },
        });
        await fresh.reload();
        await expect(fresh.getByLabel("Dil", { exact: true })).toHaveValue(
            "tr",
        );
    } finally {
        await second.close();
        await page.getByLabel("Dil", { exact: true }).selectOption("en");
        await expect(
            page.getByRole("heading", {
                name: "Plants",
                exact: true,
            }),
        ).toBeVisible();
    }
});
