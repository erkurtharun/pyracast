import { readFileSync } from "node:fs";
import path from "node:path";
import { test, expect, type Page } from "./support/test";
import { mockApi } from "./support/workspace";

const manifest = JSON.parse(
    readFileSync(
        path.resolve(__dirname, "../public/app/manifest.webmanifest"),
        "utf8",
    ),
) as { theme_color: string };

function installLinks(page: Page) {
    const head = page.locator("head");
    return {
        manifest: head.locator('link[rel="manifest"]'),
        touchIcon: head.locator('link[rel="apple-touch-icon"]'),
        themeColour: head.locator('meta[name="theme-color"]'),
    };
}

test("workspace pages link the manifest, the touch icon and the theme colour", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    const links = installLinks(page);
    await expect(links.manifest).toHaveCount(1);
    await expect(links.manifest).toHaveAttribute(
        "href",
        "/app/manifest.webmanifest",
    );
    await expect(links.manifest).not.toHaveAttribute("crossorigin");
    await expect(links.touchIcon).toHaveAttribute(
        "href",
        "/app/apple-touch-icon.png",
    );
    await expect(links.themeColour).toHaveAttribute(
        "content",
        manifest.theme_color,
    );
    await expect(page.locator('head link[rel="icon"]')).toHaveAttribute(
        "href",
        /^\/icon\.svg/,
    );
    const browser = await page.context().newCDPSession(page);
    const found = await browser.send("Page.getAppManifest", {});
    expect(new URL(found.url).pathname).toBe("/app/manifest.webmanifest");
    expect(found.errors).toEqual([]);
});

for (const address of [
    "/",
    "/tr/",
    "/privacy/",
    "/tr/privacy/",
    "/terms/",
    "/tr/terms/",
]) {
    test(`public ${address} links no manifest, touch icon or theme colour`, async ({
        page,
    }) => {
        await page.goto(address);
        const links = installLinks(page);
        await expect(page.locator("h1")).toBeVisible();
        await expect(links.manifest).toHaveCount(0);
        await expect(links.touchIcon).toHaveCount(0);
        await expect(links.themeColour).toHaveCount(0);
    });
}
