import { test, expect } from "./support/test";
import { mockApi, user } from "./support/workspace";

async function breakSession(page: Parameters<typeof mockApi>[0]) {
    let broken = true;
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: broken
                    ? { ...user, username: 0 }
                    : { ...user, can_admin: false, mfa_required: false },
                csrf_token: "token",
            },
        }),
    );
    return () => {
        broken = false;
    };
}

test("a workspace render failure offers a localized retry", async ({
    page,
}) => {
    await mockApi(page);
    const repair = await breakSession(page);
    await page.goto("/app/");
    const failure = page.getByRole("heading", {
        name: "This page could not be displayed",
        exact: true,
    });
    await expect(failure).toBeVisible();
    repair();
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await expect(failure).toHaveCount(0);
});

test("the workspace failure text follows the language cookie", async ({
    page,
    baseURL,
}) => {
    await mockApi(page);
    await breakSession(page);
    await page
        .context()
        .addCookies([
            { name: "pyracast_language", value: "tr", url: baseURL! },
        ]);
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", {
            name: "Bu sayfa görüntülenemedi",
            exact: true,
        }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Tekrar dene", exact: true }),
    ).toBeVisible();
});

test("a workspace render failure offers a reload for a window without browser controls", async ({
    page,
}) => {
    await mockApi(page);
    const repair = await breakSession(page);
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", {
            name: "This page could not be displayed",
            exact: true,
        }),
    ).toBeVisible();
    await page.evaluate(() => Object.assign(window, { failedDocument: true }));
    repair();
    await page
        .getByRole("button", { name: "Reload page", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    expect(await page.evaluate(() => "failedDocument" in window)).toBe(false);
});
