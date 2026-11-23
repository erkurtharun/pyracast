import { readFileSync } from "node:fs";
import path from "node:path";
import type { Locator } from "@playwright/test";
import { test, expect, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { standInDisplayMode } from "./support/display-mode";
import { mockApi, user } from "./support/workspace";

const manifestAddress = "/app/manifest.webmanifest";
const touchIconAddress = "/app/icons/apple-touch-icon.png";
const manifest = JSON.parse(
    readFileSync(
        path.resolve(__dirname, "../public/app/manifest.webmanifest"),
        "utf8",
    ),
) as {
    id: string;
    start_url: string;
    scope: string;
    display: string;
    name: string;
    theme_color: string;
};

const headTags = {
    manifest: 'link[rel="manifest"]',
    touchIcon: 'link[rel="apple-touch-icon"]',
    applicationName: 'meta[name="application-name"]',
    homeScreenTitle: 'meta[name="apple-mobile-web-app-title"]',
    favicon: 'link[rel="icon"]',
    themeColour: 'meta[name="theme-color"]',
    launchCapability:
        'meta[name="mobile-web-app-capable"], meta[name="apple-mobile-web-app-capable"]',
};
type HeadTag = keyof typeof headTags;
type ServedTag = { head: Record<string, string>[]; elsewhere: number };

// DOMParser runs no script, so this is the tree the browser's parser builds from the served bytes.
function readServedHead(page: Page, html: string) {
    return page.evaluate(
        ({ html, selectors }) => {
            const parsed = new DOMParser().parseFromString(html, "text/html");
            return Object.fromEntries(
                Object.entries(selectors).map(([tag, selector]) => {
                    const found = [...parsed.querySelectorAll(selector)];
                    const head = found.filter(
                        (element) => element.parentElement === parsed.head,
                    );
                    return [
                        tag,
                        {
                            head: head.map((element) =>
                                Object.fromEntries(
                                    [...element.attributes].map(
                                        ({ name, value }) => [name, value],
                                    ),
                                ),
                            ),
                            elsewhere: found.length - head.length,
                        },
                    ];
                }),
            );
        },
        { html, selectors: headTags },
    ) as Promise<Record<HeadTag, ServedTag>>;
}

const installHead = {
    manifest: { rel: "manifest", href: manifestAddress },
    touchIcon: { rel: "apple-touch-icon", href: touchIconAddress },
    applicationName: { name: "application-name", content: manifest.name },
    homeScreenTitle: {
        name: "apple-mobile-web-app-title",
        content: manifest.name,
    },
};
const absent: ServedTag = { head: [], elsewhere: 0 };

function onlyInHead(attributes: Record<string, string>): ServedTag {
    return { head: [attributes], elsewhere: 0 };
}

function resolvedPath(page: Page, href: string | null | undefined) {
    return new URL(href ?? "", page.url()).pathname;
}

type Layout = "root" | "app";

async function expectServedHead(page: Page, html: string, layout: Layout) {
    const served = await readServedHead(page, html);
    for (const [tag, attributes] of Object.entries(installHead))
        expect(served[tag as HeadTag], tag).toEqual(onlyInHead(attributes));
    expect(served.launchCapability).toEqual(absent);
    expect(served.favicon.elsewhere).toBe(0);
    expect(
        served.favicon.head.map(({ href }) => resolvedPath(page, href)),
    ).toEqual(["/icon.svg"]);
    expect(served.themeColour).toEqual(
        layout === "app"
            ? onlyInHead({ name: "theme-color", content: manifest.theme_color })
            : absent,
    );
}

async function expectLiveHead(page: Page, layout: Layout) {
    const head = page.locator("head");
    const tag = (name: HeadTag) => head.locator(headTags[name]);
    await expect(tag("manifest")).toHaveCount(1);
    await expect(tag("manifest")).toHaveAttribute("href", manifestAddress);
    await expect(tag("manifest")).not.toHaveAttribute("crossorigin");
    await expect(tag("touchIcon")).toHaveCount(1);
    await expect(tag("touchIcon")).toHaveAttribute("href", touchIconAddress);
    for (const name of [tag("applicationName"), tag("homeScreenTitle")]) {
        await expect(name).toHaveCount(1);
        await expect(name).toHaveAttribute("content", manifest.name);
    }
    await expect(tag("launchCapability")).toHaveCount(0);
    await expect(tag("favicon")).toHaveCount(1);
    expect(resolvedPath(page, await tag("favicon").getAttribute("href"))).toBe(
        "/icon.svg",
    );
    if (layout === "app")
        await expect(tag("themeColour")).toHaveAttribute(
            "content",
            manifest.theme_color,
        );
    else await expect(tag("themeColour")).toHaveCount(0);
}

type Address = {
    name: string;
    address: string;
    layout: Layout;
    session?: "anonymous" | "member";
    shown: (page: Page) => Locator;
};

const publicAddresses: Address[] = [
    "/",
    "/tr/",
    "/privacy/",
    "/tr/privacy/",
    "/terms/",
    "/tr/terms/",
].map((address) => ({
    name: `public ${address}`,
    address,
    layout: "root",
    shown: (page) => page.locator("h1"),
}));

const workspacePage: Address = {
    name: "a workspace page",
    address: "/app/w/1/",
    layout: "app",
    session: "member",
    shown: (page) => page.getByRole("heading", { name: "Plants", exact: true }),
};

const pageNotFound = (page: Page) =>
    page.getByRole("heading", { name: "Page not found", exact: true });

// notFound() in a route renders app/app/not-found.tsx inside the /app/ layout; an address no route matches renders the root not-found file in the root layout alone.
const addresses: Address[] = [
    ...publicAddresses,
    {
        name: "an unknown public address",
        address: "/no-such-page/",
        layout: "root",
        session: "anonymous",
        shown: pageNotFound,
    },
    {
        name: "the sign-in screen",
        address: "/app/",
        layout: "app",
        session: "anonymous",
        shown: (page) =>
            page.locator("form").getByRole("button", { name: "Sign in" }),
    },
    workspacePage,
    {
        name: "a missing workspace page",
        address: "/app/w/1/plants/11/unknown/",
        layout: "app",
        session: "member",
        shown: pageNotFound,
    },
    {
        name: "an unknown address under /app/",
        address: "/app/no-such-page/",
        layout: "root",
        session: "member",
        shown: pageNotFound,
    },
];

async function visit(page: Page, { address, session, shown }: Address) {
    if (session) await mockApi(page, { anonymous: session === "anonymous" });
    const served = await (await page.goto(address))!.text();
    await expect(shown(page)).toBeVisible();
    return served;
}

for (const address of addresses)
    test(`${address.name} serves the manifest link, the touch icon and the app's name as children of head ${
        address.layout === "app" ? "with" : "without"
    } the theme colour`, async ({ page }) => {
        const served = await visit(page, address);
        await expectServedHead(page, served, address.layout);
        await expectLiveHead(page, address.layout);
    });

test("a served head tag counts only as a child of head, not in body or in script text", async ({
    page,
}) => {
    const link = `<link rel="manifest" href="${manifestAddress}">`;
    const read = async (html: string) =>
        (await readServedHead(page, html)).manifest;
    expect(
        await read(
            `<!doctype html><html><head>${link}</head><body></body></html>`,
        ),
    ).toEqual(onlyInHead(installHead.manifest));
    expect(
        await read(
            `<!doctype html><html><head></head><body><div hidden>${link}</div></body></html>`,
        ),
    ).toEqual({ head: [], elsewhere: 1 });
    expect(
        await read(
            `<!doctype html><html><head><script>self.payload = '${link}';</script></head><body></body></html>`,
        ),
    ).toEqual(absent);
});

function originAndPath(url: URL) {
    return { origin: url.origin, pathname: url.pathname };
}

// Chromium reports a processed display mode by its enum name: "standalone" as "kStandalone".
function chromiumDisplayMode(display: string) {
    const words = display.split("-");
    return `k${words.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join("")}`;
}

for (const address of [...publicAddresses.slice(0, 2), workspacePage])
    test(`the browser processes the app manifest on ${address.name} with the app's identity, start, scope and display`, async ({
        page,
    }) => {
        await visit(page, address);
        const browser = await page.context().newCDPSession(page);
        const found = await browser.send("Page.getAppManifest", {});
        expect(found.url && new URL(found.url).pathname).toBe(manifestAddress);
        expect(found.errors).toEqual([]);
        const processed = (value: string | undefined) =>
            value === undefined ? value : originAndPath(new URL(value));
        const expected = (value: string) =>
            originAndPath(new URL(value, page.url()));
        expect({
            id: processed(found.manifest.id),
            startUrl: processed(found.manifest.startUrl),
            scope: processed(found.manifest.scope),
            display: found.manifest.display,
        }).toEqual({
            id: expected(manifest.id),
            startUrl: expected(manifest.start_url),
            scope: expected(manifest.scope),
            display: chromiumDisplayMode(manifest.display),
        });
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
