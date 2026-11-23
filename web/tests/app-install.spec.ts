import { readFileSync } from "node:fs";
import path from "node:path";
import { test, expect, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { standInDisplayMode } from "./support/display-mode";
import { mockApi, user } from "./support/workspace";

const manifestAddress = "/app/manifest.webmanifest";
const manifest = JSON.parse(
    readFileSync(
        path.resolve(__dirname, "../public/app/manifest.webmanifest"),
        "utf8",
    ),
) as { name: string; theme_color: string };

function installLinks(page: Page) {
    const head = page.locator("head");
    return {
        manifest: head.locator('link[rel="manifest"]'),
        touchIcon: head.locator('link[rel="apple-touch-icon"]'),
        favicon: head.locator('link[rel="icon"]'),
        themeColour: head.locator('meta[name="theme-color"]'),
        applicationName: head.locator('meta[name="application-name"]'),
        homeScreenTitle: head.locator(
            'meta[name="apple-mobile-web-app-title"]',
        ),
        launchCapability: head.locator(
            'meta[name="mobile-web-app-capable"], meta[name="apple-mobile-web-app-capable"]',
        ),
    };
}

async function expectInstallHead(page: Page) {
    const links = installLinks(page);
    await expect(links.manifest).toHaveCount(1);
    await expect(links.manifest).toHaveAttribute("href", manifestAddress);
    await expect(links.manifest).not.toHaveAttribute("crossorigin");
    await expect(links.touchIcon).toHaveCount(1);
    await expect(links.touchIcon).toHaveAttribute(
        "href",
        "/app/icons/apple-touch-icon.png",
    );
    for (const name of [links.applicationName, links.homeScreenTitle]) {
        await expect(name).toHaveCount(1);
        await expect(name).toHaveAttribute("content", manifest.name);
    }
    await expect(links.launchCapability).toHaveCount(0);
    await expect(links.favicon).toHaveCount(1);
    await expect(links.favicon).toHaveAttribute("href", "/icon.svg");
}

const publicAddresses = [
    "/",
    "/tr/",
    "/privacy/",
    "/tr/privacy/",
    "/terms/",
    "/tr/terms/",
];

type Opener = (page: Page) => Promise<void>;

function openPublic(address: string): Opener {
    return async (page) => {
        await page.goto(address);
        await expect(page.locator("h1")).toBeVisible();
    };
}

function notFound(page: Page) {
    return page.getByRole("heading", { name: "Page not found", exact: true });
}

function openNotFound(address: string): Opener {
    return async (page) => {
        await mockApi(page);
        await page.goto(address);
        await expect(notFound(page)).toBeVisible();
    };
}

async function openSignIn(page: Page) {
    await mockApi(page, { anonymous: true });
    await page.goto("/app/");
    await expect(
        page.locator("form").getByRole("button", { name: "Sign in" }),
    ).toBeVisible();
}

const openWorkspacePage: Opener = (page) => openWorkspace(page);

// An address no route matches, under /app/ too, gets the root not-found document outside the /app/ layout.
const publicPages: [string, Opener][] = [
    ...publicAddresses.map((address): [string, Opener] => [
        `public ${address}`,
        openPublic(address),
    ]),
    ["an unknown public address", openNotFound("/no-such-page/")],
    ["an unknown address under /app/", openNotFound("/app/no-such-page/")],
];

const appPages: [string, Opener][] = [
    ["the sign-in screen", openSignIn],
    ["a workspace page", openWorkspacePage],
    ["a missing workspace page", openNotFound("/app/w/1/plants/11/unknown/")],
];

for (const [name, open] of publicPages)
    test(`${name} links the app manifest, the touch icon and the app's name without the theme colour`, async ({
        page,
    }) => {
        await open(page);
        await expectInstallHead(page);
        await expect(installLinks(page).themeColour).toHaveCount(0);
    });

for (const [name, open] of appPages)
    test(`${name} links the app manifest, the touch icon, the app's name and the theme colour`, async ({
        page,
    }) => {
        await open(page);
        await expectInstallHead(page);
        await expect(installLinks(page).themeColour).toHaveAttribute(
            "content",
            manifest.theme_color,
        );
    });

for (const [name, open] of [
    ["public /", openPublic("/")],
    ["public /tr/", openPublic("/tr/")],
    ["a workspace page", openWorkspacePage],
] as const)
    test(`the browser finds the app manifest on ${name}`, async ({ page }) => {
        await open(page);
        const browser = await page.context().newCDPSession(page);
        const found = await browser.send("Page.getAppManifest", {});
        expect(found.url && new URL(found.url).pathname).toBe(manifestAddress);
        expect(found.errors).toEqual([]);
    });

test("the served head of every public page carries the install head without JavaScript", async ({
    browser,
    baseURL,
}) => {
    const context = await browser.newContext({
        baseURL,
        javaScriptEnabled: false,
    });
    try {
        const page = await context.newPage();
        for (const address of publicAddresses) {
            await page.goto(address);
            await expect(page.locator("h1")).toBeVisible();
            await expectInstallHead(page);
        }
    } finally {
        await context.close();
    }
});

type Engine = "webkit" | "gecko";

async function emulateEngine(page: Page, engine: Engine) {
    await page.addInitScript((name: Engine) => {
        delete (window as { onbeforeinstallprompt?: unknown })
            .onbeforeinstallprompt;
        if (name === "webkit")
            Object.assign(window, { GestureEvent: class extends UIEvent {} });
    }, engine);
}

const copy = {
    en: {
        name: "English",
        plants: "Plants",
        install: "Install app",
        browserMenu:
            "Open the browser menu and choose the command with Install in its name, such as Install this site as an app or Install and create shortcut. In some browsers it is inside a submenu.",
        homeScreen:
            "Tap Share, then Add to Home Screen. If Share is not shown, it is in the page menu.",
        dock: "In Safari, choose File > Add to Dock.",
    },
    tr: {
        name: "Turkish",
        plants: "Santraller",
        install: "Uygulamayı yükle",
        browserMenu:
            "Tarayıcı menüsünü açın ve adında yükle sözcüğü geçen komutu seçin; örneğin Bu siteyi uygulama olarak yükleyin ya da Yükle ve kısayol oluştur. Bazı tarayıcılarda bu komut bir alt menüdedir.",
        homeScreen:
            "Paylaş'a, ardından Ana Ekrana Ekle'ye dokunun. Paylaş görünmüyorsa sayfa menüsündedir.",
        dock: "Safari'de Dosya > Dock'a Ekle'yi seçin.",
    },
} as const;
type Language = keyof typeof copy;
const languages = Object.keys(copy) as Language[];

async function openWorkspace(page: Page, language: Language = "en") {
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: { ...user, language_preference: language },
                csrf_token: "signed-token",
                language,
            },
        }),
    );
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: copy[language].plants }),
    ).toBeVisible();
}

function installSteps(page: Page, language: Language = "en") {
    return page
        .locator(".sidebar details")
        .filter({ hasText: copy[language].install });
}

function offerInstall(page: Page, outcome: "accepted" | "dismissed") {
    return page.evaluate((choice) => {
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
                userChoice: Promise.resolve({ outcome: choice }),
            },
        );
        return !window.dispatchEvent(event);
    }, outcome);
}

function installButton(page: Page) {
    return page
        .locator(".sidebar")
        .getByRole("button", { name: "Install app", exact: true });
}

function installPrompts(page: Page) {
    return page.evaluate(
        () => (window as { installPrompts?: number }).installPrompts,
    );
}

test("the install entry uses a saved install event once and disappears after acceptance", async ({
    page,
}) => {
    await openWorkspace(page);
    expect(await offerInstall(page, "accepted")).toBe(true);
    const install = installButton(page);
    await install.click();
    await expect(install).toHaveCount(0);
    await expect(installSteps(page)).toHaveCount(0);
    expect(await installPrompts(page)).toBe(1);
});

test("a dismissed install prompt falls back to the browser menu steps", async ({
    page,
}) => {
    await openWorkspace(page);
    await offerInstall(page, "dismissed");
    const install = installButton(page);
    await install.click();
    await expect(install).toHaveCount(0);
    await expect(installSteps(page)).toHaveCount(1);
    expect(await installPrompts(page)).toBe(1);
});

test("an app installed from the browser menu hides the install entry", async ({
    page,
}) => {
    await openWorkspace(page);
    await offerInstall(page, "accepted");
    await expect(installButton(page)).toBeVisible();
    await page.evaluate(() => window.dispatchEvent(new Event("appinstalled")));
    await expect(page.locator(".sidebar")).not.toContainText("Install app");
    expect(await installPrompts(page)).toBeUndefined();
});

for (const language of languages) {
    const { name, browserMenu, homeScreen, dock } = copy[language];

    test(`without an install event a Chromium browser shows the browser menu steps in ${name}`, async ({
        page,
    }) => {
        await openWorkspace(page, language);
        const steps = installSteps(page, language);
        await steps.locator("summary").focus();
        await page.keyboard.press("Enter");
        await expect(steps).toHaveAttribute("open", "");
        await expect(steps).toContainText(browserMenu);
        expect(await accessibilityViolations(page, ".sidebar")).toEqual([]);
    });

    test.describe("on a phone", () => {
        test.use({
            viewport: { width: 390, height: 844 },
            hasTouch: true,
            isMobile: true,
        });

        test(`a WebKit browser shows the Home Screen steps in ${name}`, async ({
            page,
        }) => {
            await emulateEngine(page, "webkit");
            await openWorkspace(page, language);
            const steps = installSteps(page, language);
            await steps.locator("summary").click();
            await expect(steps).toContainText(homeScreen);
            expect(await accessibilityViolations(page, ".sidebar")).toEqual([]);
        });
    });

    test(`Safari on a Mac shows the Add to Dock steps in ${name}`, async ({
        page,
    }) => {
        await emulateEngine(page, "webkit");
        await openWorkspace(page, language);
        const steps = installSteps(page, language);
        await steps.locator("summary").click();
        await expect(steps).toContainText(dock);
    });
}

test("a browser with neither the install event nor WebKit shows no install entry", async ({
    page,
}) => {
    await emulateEngine(page, "gecko");
    await openWorkspace(page);
    await expect(page.locator(".sidebar")).not.toContainText("Install app");
});

test("an installed window shows no install entry", async ({ page }) => {
    await standInDisplayMode(page, "standalone");
    await openWorkspace(page);
    await expect(
        page.getByRole("button", { name: "Sign out", exact: true }),
    ).toBeVisible();
    await expect(page.locator(".sidebar")).not.toContainText("Install app");
});
