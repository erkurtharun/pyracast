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

function startScreen(page: Parameters<typeof mockApi>[0]) {
    return page.getByRole("link", {
        name: "Go to the start screen",
        exact: true,
    });
}

test("a workspace render failure leads to the start screen in a new document", async ({
    page,
}) => {
    await mockApi(page);
    const repair = await breakSession(page);
    await page.goto("/app/account/");
    await expect(
        page.getByRole("heading", {
            name: "This page could not be displayed",
            exact: true,
        }),
    ).toBeVisible();
    await expect(startScreen(page)).toHaveAttribute("href", "/app/");
    await page.evaluate(() => Object.assign(window, { failedDocument: true }));
    repair();
    await startScreen(page).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    expect(await page.evaluate(() => "failedDocument" in window)).toBe(false);
});

test("a failed session read is titled and leads to the start screen", async ({
    page,
}) => {
    await mockApi(page);
    let unavailable = true;
    await page.route("**/api/session/", (route) =>
        unavailable
            ? route.fulfill({ status: 503, body: "unavailable" })
            : route.fallback(),
    );
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", {
            name: "Pyracast could not be opened",
            exact: true,
        }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Try again", exact: true }),
    ).toBeVisible();
    await expect(startScreen(page)).toHaveAttribute("href", "/app/");
    unavailable = false;
    await startScreen(page).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});

test("a failed language hand-off is titled and leads to the start screen without the language", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    const attempts: string[] = [];
    await page.route("**/api/session/language/", (route) => {
        attempts.push(route.request().method());
        return route.fulfill({ status: 503, body: "unavailable" });
    });
    await page.goto("/app/?language=tr&signup=1");
    await expect(
        page.getByRole("heading", {
            name: "Pyracast could not be opened",
            exact: true,
        }),
    ).toBeVisible();
    await startScreen(page).click();
    await expect(page).toHaveURL("/app/");
    await expect(
        page.getByRole("heading", { name: "Welcome back", exact: true }),
    ).toBeVisible();
    expect(attempts).toEqual(["POST"]);
});

test("a failure of the application frame offers a document reload", async ({
    page,
}) => {
    await page.addInitScript(() => {
        const lang = Object.getOwnPropertyDescriptor(
            HTMLElement.prototype,
            "lang",
        )!;
        Object.defineProperty(HTMLHtmlElement.prototype, "lang", {
            configurable: true,
            get() {
                return lang.get!.call(this);
            },
            set(value: string) {
                if (!sessionStorage.getItem("frameRepaired"))
                    throw new Error("The frame could not set its language.");
                lang.set!.call(this, value);
            },
        });
    });
    await mockApi(page);
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", {
            name: "This page could not be displayed",
            exact: true,
        }),
    ).toBeVisible();
    await page.evaluate(() => {
        Object.assign(window, { failedDocument: true });
        sessionStorage.setItem("frameRepaired", "yes");
    });
    await page
        .getByRole("button", { name: "Reload page / Sayfayı yenile" })
        .click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    expect(await page.evaluate(() => "failedDocument" in window)).toBe(false);
});

test("a missing workspace page leads back to the app without choosing a language", async ({
    page,
}) => {
    await mockApi(page);
    const languages: string[] = [];
    await page.route("**/api/session/language/", (route) => {
        languages.push(route.request().method());
        return route.fallback();
    });
    await page.goto("/app/w/1/plants/11/unknown/");
    await expect(
        page.getByRole("heading", { name: "Page not found", exact: true }),
    ).toBeVisible();
    const app = page.getByRole("link", { name: "Go to the app", exact: true });
    await expect(app).toHaveAttribute("href", "/app/");
    await expect(
        page.getByRole("link", { name: "Uygulamaya git", exact: true }),
    ).toHaveAttribute("href", "/app/");
    await app.click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    expect(new URL(page.url()).searchParams.has("language")).toBe(false);
    expect(languages).toEqual([]);
});
