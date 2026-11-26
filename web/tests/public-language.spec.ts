import type {
    APIResponse,
    BrowserContext,
    Locator,
    Request,
} from "@playwright/test";
import { test, expect, type Page } from "./support/test";
import { languageCookieOf } from "./support/language-cookie";
import negotiation from "./support/language-negotiation.json";
import { productionServer } from "./support/server";
import { sessionAnswer } from "./support/session";
import { firstAccount } from "./support/workspace";
import { publicPages, publicPath } from "../src/lib/public-metadata";

const query = "?from=mail&step=2";
const turkishBrowser = { "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8" };
const englishBrowser = { "Accept-Language": "en-US,en;q=0.9" };

function expectLanguageRedirect(response: APIResponse, location: string) {
    expect(response.status(), location).toBe(307);
    const headers = response.headers();
    expect(headers.location).toBe(location);
    expect(headers["cache-control"]).toBe("private, no-store");
    expect(headers.vary).toBe("Accept-Language, Cookie, CF-IPCountry");
}

test("an English address opens the same Turkish page, query kept, for a visitor who reads Turkish", async ({
    request,
}) => {
    for (const page of publicPages)
        expectLanguageRedirect(
            await request.get(`${publicPath("en", page)}${query}`, {
                headers: turkishBrowser,
                maxRedirects: 0,
            }),
            `${publicPath("tr", page)}${query}`,
        );
    expectLanguageRedirect(
        await request.get("/", {
            headers: { ...turkishBrowser, Host: "elsewhere.example" },
            maxRedirects: 0,
        }),
        "/tr/",
    );
});

test("without a browser language the country decides: Turkey opens Turkish, elsewhere English", async ({
    request,
}) => {
    expectLanguageRedirect(
        await request.get("/terms/", {
            headers: { "CF-IPCountry": "TR" },
            maxRedirects: 0,
        }),
        "/tr/terms/",
    );
    const elsewhere = await request.get("/terms/", {
        headers: { "CF-IPCountry": "DE" },
        maxRedirects: 0,
    });
    expect(elsewhere.status()).toBe(200);
    expect(await elsewhere.text()).toMatch(/<html lang="en"/);
});

for (const [visitor, headers] of [
    ["an English browser", englishBrowser],
    [
        "a browser language the site does not offer",
        { "Accept-Language": "de-DE,fr;q=0.8" },
    ],
    ["no browser language or country", {}],
    [
        "an earlier choice of English in a Turkish browser",
        { ...turkishBrowser, Cookie: "pyracast_language=en" },
    ],
] as const)
    test(`the English address stays English for ${visitor}`, async ({
        request,
    }) => {
        const response = await request.get("/", { headers, maxRedirects: 0 });
        expect(response.status()).toBe(200);
        expect(await response.text()).toMatch(/<html lang="en"/);
    });

test("a Turkish address stays Turkish whatever the visitor's language", async ({
    request,
}) => {
    for (const page of publicPages) {
        const response = await request.get(publicPath("tr", page), {
            headers: { ...englishBrowser, Cookie: "pyracast_language=en" },
            maxRedirects: 0,
        });
        expect(response.status(), page).toBe(200);
        expect(await response.text(), page).toMatch(/<html lang="tr"/);
    }
});

test("a saved Turkish preference opens Turkish in an English browser", async ({
    request,
}) => {
    expectLanguageRedirect(
        await request.get("/privacy/", {
            headers: { ...englishBrowser, Cookie: "pyracast_language=tr" },
            maxRedirects: 0,
        }),
        "/tr/privacy/",
    );
});

test("the application's first screen is in the visitor's language", async ({
    request,
}) => {
    for (const [headers, language] of [
        [turkishBrowser, "tr"],
        [{ ...turkishBrowser, Cookie: "pyracast_language=en" }, "en"],
        [{ "CF-IPCountry": "TR" }, "tr"],
    ] as const) {
        const response = await request.get("/app/", { headers });
        expect(await response.text()).toMatch(
            new RegExp(`<html lang="${language}"`),
        );
    }
});

test("a choice through the language link opens the clean address and is remembered", async ({
    request,
}) => {
    const response = await request.get("/tr/privacy/?language=en&from=mail", {
        headers: turkishBrowser,
        maxRedirects: 0,
    });
    expectLanguageRedirect(response, "/privacy/?from=mail");
    const cookie = response
        .headersArray()
        .find(({ name }) => name.toLowerCase() === "set-cookie")?.value;
    const [pair, ...attributes] = cookie!.split(/;\s*/);
    const { name, maxAge, path, sameSite } = negotiation.cookie;
    expect(pair).toBe(`${name}=en`);
    const named = attributes
        .map((part) => part.toLowerCase())
        .filter((part) => !part.startsWith("expires="));
    expect(new Set(named)).toEqual(
        new Set([
            `path=${path}`,
            `max-age=${maxAge}`,
            "httponly",
            `samesite=${sameSite.toLowerCase()}`,
            ...(productionServer ? ["secure"] : []),
        ]),
    );
});

test.describe("in a Turkish browser", () => {
    test.use({ locale: "tr-TR" });

    test("a visitor who chooses English stays on English at the next visit", async ({
        page,
        context,
    }) => {
        await page.goto("/");
        await expect(page).toHaveURL("/tr/");
        await expect(page.locator("html")).toHaveAttribute("lang", "tr");
        await page.locator('header a[hreflang="en"]').click();
        await expect(page).toHaveURL("/");
        await expect(page.locator("html")).toHaveAttribute("lang", "en");
        const { name, path, httpOnly, sameSite } = negotiation.cookie;
        expect(await languageCookieOf(context)).toMatchObject({
            name,
            value: "en",
            path,
            httpOnly,
            sameSite,
            secure: productionServer,
        });
        await page.goto("/terms/");
        await expect(page).toHaveURL("/terms/");
        await expect(page.locator("html")).toHaveAttribute("lang", "en");
    });
});

function navigationTo(pathname: string) {
    return (request: Request) =>
        request.isNavigationRequest() &&
        new URL(request.url()).pathname === pathname;
}

async function signedInOnTurkishPrivacy(
    page: Page,
    context: BrowserContext,
    baseURL: string,
    sessionAnswered: Promise<void> = Promise.resolve(),
) {
    await context.addCookies([
        { name: "pyracast_language", value: "tr", url: baseURL },
    ]);
    const reads: Request[] = [];
    await page.route("**/api/session/", async (route) => {
        reads.push(route.request());
        await sessionAnswered;
        await route.fulfill({ json: sessionAnswer(firstAccount, null) });
    });
    const saves: Request[] = [];
    await page.route("**/api/session/language/", (route) => {
        saves.push(route.request());
        return route.fulfill({
            json: { ...sessionAnswer(firstAccount, null), language: "en" },
        });
    });
    await page.goto("/privacy/");
    await expect(page).toHaveURL("/tr/privacy/");
    return { reads, saves, link: page.locator('header a[hreflang="en"]') };
}

test("a signed-in person's choice on the public site is saved as their preference", async ({
    page,
    context,
    baseURL,
}) => {
    const { saves, link } = await signedInOnTurkishPrivacy(
        page,
        context,
        baseURL!,
    );
    await link.click();
    await expect(page).toHaveURL("/privacy/");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    expect(saves).toHaveLength(1);
    expect(saves[0]!.method()).toBe("POST");
    expect(saves[0]!.postDataJSON()).toEqual({ language: "en" });
    expect(await saves[0]!.allHeaders()).toMatchObject({
        "x-csrftoken": "signed-token",
        "x-session-tag": firstAccount.tag,
    });
});

test("the language link saves a signed-in choice and then follows the link", async ({
    page,
    context,
    baseURL,
}) => {
    const { link } = await signedInOnTurkishPrivacy(page, context, baseURL!);
    await page.route(
        (url) => url.pathname === "/privacy/",
        (route) => route.fulfill({ status: 204 }),
    );
    const saved = page.waitForRequest("**/api/session/language/");
    const followed = page.waitForRequest(navigationTo("/privacy/"));
    await link.click();
    await saved;
    await followed;
    await expect(page).toHaveURL("/tr/privacy/");
});

const activationsTheBrowserOpens = {
    "a control click": { modifiers: ["ControlOrMeta"] },
    "a middle click": { button: "middle" },
} satisfies Record<string, Parameters<Locator["click"]>[0]>;

for (const [activation, options] of Object.entries(activationsTheBrowserOpens))
    test(`${activation} on the language link saves a signed-in choice and keeps the page where it is`, async ({
        page,
        context,
        baseURL,
    }) => {
        const { saves, link } = await signedInOnTurkishPrivacy(
            page,
            context,
            baseURL!,
        );
        const opened = context.waitForEvent("page");
        const saved = page.waitForRequest("**/api/session/language/");
        await link.click(options);
        await (await saved).response();
        await expect(await opened).toHaveURL("/privacy/");
        await expect(page).toHaveURL("/tr/privacy/");
        expect(saves).toHaveLength(1);
        expect(saves[0]!.postDataJSON()).toEqual({ language: "en" });
    });

test("two quick clicks on the language link send one save and then follow the link", async ({
    page,
    context,
    baseURL,
}) => {
    const sessionAnswered = Promise.withResolvers<void>();
    const { reads, saves, link } = await signedInOnTurkishPrivacy(
        page,
        context,
        baseURL!,
        sessionAnswered.promise,
    );
    await page.route(
        (url) => url.pathname === "/privacy/",
        (route) => route.fulfill({ status: 204 }),
    );
    const followed = page.waitForRequest(navigationTo("/privacy/"));
    await link.click();
    await link.click();
    sessionAnswered.resolve();
    await followed;
    expect(reads).toHaveLength(1);
    expect(saves).toHaveLength(1);
    await expect(page).toHaveURL("/tr/privacy/");
});

test("a save still pending when the person follows another link does not bring them back", async ({
    page,
    context,
    baseURL,
}) => {
    const sessionAnswered = Promise.withResolvers<void>();
    const { link } = await signedInOnTurkishPrivacy(
        page,
        context,
        baseURL!,
        sessionAnswered.promise,
    );
    const termsAnswered = Promise.withResolvers<void>();
    await page.route(
        (url) => url.pathname === "/tr/terms/",
        async (route) => {
            await termsAnswered.promise;
            await route.fallback();
        },
    );
    const read = page.waitForRequest("**/api/session/");
    await link.click();
    await read;
    const terms = page.waitForRequest(navigationTo("/tr/terms/"));
    await page.locator('footer a[href="/tr/terms/"]').dispatchEvent("click");
    await terms;
    const saved = page.waitForRequest("**/api/session/language/");
    sessionAnswered.resolve();
    await (await saved).response();
    termsAnswered.resolve();
    await expect(page).toHaveURL("/tr/terms/");
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
});
