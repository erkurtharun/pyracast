import { readFileSync } from "node:fs";
import path from "node:path";
import { test, expect, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
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

type Engine = "webkit" | "gecko";

async function emulateEngine(page: Page, engine: Engine) {
    await page.addInitScript((name: Engine) => {
        delete (window as { onbeforeinstallprompt?: unknown })
            .onbeforeinstallprompt;
        if (name === "webkit")
            Object.assign(window, { GestureEvent: class extends UIEvent {} });
    }, engine);
}

async function openWorkspace(page: Page) {
    await mockApi(page);
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
}

function installSteps(page: Page) {
    return page.locator(".sidebar details").filter({ hasText: "Install app" });
}

test("the install entry uses a saved install event once and disappears after acceptance", async ({
    page,
}) => {
    await openWorkspace(page);
    const prevented = await page.evaluate(() => {
        const event = Object.assign(
            new Event("beforeinstallprompt", { cancelable: true }),
            {
                prompt: () => {
                    Object.assign(window, {
                        installPrompts:
                            ((window as { installPrompts?: number })
                                .installPrompts ?? 0) + 1,
                    });
                    return Promise.resolve();
                },
                userChoice: Promise.resolve({ outcome: "accepted" }),
            },
        );
        return !window.dispatchEvent(event);
    });
    expect(prevented).toBe(true);
    const install = page
        .locator(".sidebar")
        .getByRole("button", { name: "Install app", exact: true });
    await install.click();
    await expect(install).toHaveCount(0);
    await expect(installSteps(page)).toHaveCount(0);
    expect(
        await page.evaluate(
            () => (window as { installPrompts?: number }).installPrompts,
        ),
    ).toBe(1);
});

test("without an install event a Chromium browser shows the browser menu steps", async ({
    page,
}) => {
    await openWorkspace(page);
    const steps = installSteps(page);
    await steps.locator("summary").focus();
    await page.keyboard.press("Enter");
    await expect(steps).toHaveAttribute("open", "");
    await expect(steps).toContainText(
        "Open the browser menu and choose Install or Add to Home screen.",
    );
    expect(await accessibilityViolations(page, ".sidebar")).toEqual([]);
});

test.describe("on a phone", () => {
    test.use({
        viewport: { width: 390, height: 844 },
        hasTouch: true,
        isMobile: true,
    });

    test("a WebKit browser shows the Home Screen steps", async ({ page }) => {
        await emulateEngine(page, "webkit");
        await openWorkspace(page);
        const steps = installSteps(page);
        await steps.locator("summary").click();
        await expect(steps).toContainText(
            "Tap Share, then Add to Home Screen.",
        );
        expect(await accessibilityViolations(page, ".sidebar")).toEqual([]);
    });
});

test("Safari on a Mac shows the Add to Dock steps", async ({ page }) => {
    await emulateEngine(page, "webkit");
    await openWorkspace(page);
    const steps = installSteps(page);
    await steps.locator("summary").click();
    await expect(steps).toContainText("In Safari, choose File > Add to Dock.");
});

test("a browser that cannot install shows no install entry", async ({
    page,
}) => {
    await emulateEngine(page, "gecko");
    await openWorkspace(page);
    await expect(page.locator(".sidebar")).not.toContainText("Install app");
});

test("an installed window shows no install entry", async ({ page }) => {
    await page.addInitScript(() => {
        const media = window.matchMedia.bind(window);
        window.matchMedia = (query) =>
            media(query === "(display-mode: standalone)" ? "all" : query);
    });
    await openWorkspace(page);
    await expect(
        page.getByRole("button", { name: "Sign out", exact: true }),
    ).toBeVisible();
    await expect(page.locator(".sidebar")).not.toContainText("Install app");
});
