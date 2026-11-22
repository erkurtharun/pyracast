import { test, expect } from "./support/test";
import type { Locator } from "@playwright/test";
import { accessibilityViolations } from "./support/accessibility";
import { mockApi } from "./support/workspace";

const ink = { light: "#09383a", dark: "#f4f2e8" };

async function expectLogo(place: Locator, surface: keyof typeof ink) {
    const logo = place.getByRole("img", { name: "Pyracast", exact: true });
    await expect(logo).toHaveCount(1);
    await expect(logo).toBeVisible();
    await expect(logo.locator('path[fill="#ffb545"]')).toHaveCount(1);
    await expect(logo.locator(`[fill="${ink[surface]}"]`)).toHaveCount(2);
    await expect(logo.locator("path")).toHaveCount(10);
    const box = (await logo.boundingBox())!;
    expect(box.width / box.height).toBeCloseTo(518 / 101, 1);
}

test("the first paint of the application already draws the logo for light surfaces", async ({
    browser,
    baseURL,
}) => {
    const context = await browser.newContext({
        baseURL,
        javaScriptEnabled: false,
    });
    try {
        const page = await context.newPage();
        await page.goto("/app/");
        await expectLogo(page.locator(".auth-card"), "light");
    } finally {
        await context.close();
    }
});

test("the sign-in story names the logo for dark surfaces once", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expectLogo(page.locator(".auth-story"), "dark");
    await expect(
        page.getByRole("img", { name: "Pyracast", exact: true }),
    ).toHaveCount(1);
    expect(await accessibilityViolations(page)).toEqual([]);
});

test("the workspace sidebar names the logo for dark surfaces once", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await expectLogo(page.getByRole("complementary"), "dark");
    await expect(
        page.getByRole("img", { name: "Pyracast", exact: true }),
    ).toHaveCount(1);
    await page.setViewportSize({ width: 360, height: 740 });
    await expectLogo(page.getByRole("complementary"), "dark");
    expect(await accessibilityViolations(page)).toEqual([]);
});

test("the logo's surfaces take the brand palette", async ({ page }) => {
    await mockApi(page);
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await expect(page.locator("body")).toHaveCSS(
        "background-color",
        "rgb(249, 248, 243)",
    );
    await expect(page.getByRole("complementary")).toHaveCSS(
        "background-color",
        "rgb(9, 56, 58)",
    );
    await page.goto("/");
    await expect(page.getByRole("banner").locator("..")).toHaveCSS(
        "background-color",
        "rgb(249, 248, 243)",
    );
});

for (const path of ["/", "/tr/"]) {
    test(`the public site ${path} links home through the logo for light surfaces`, async ({
        page,
    }) => {
        await page.goto(path);
        for (const place of [
            page.getByRole("banner"),
            page.getByRole("contentinfo"),
        ]) {
            const home = place.getByRole("link", {
                name: "Pyracast",
                exact: true,
            });
            await expect(home).toHaveAttribute("href", path);
            await expectLogo(home, "light");
        }
    });
}
