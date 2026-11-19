import type { Page, Request } from "@playwright/test";
import { errorBody } from "./support/errors";
import { expect, test } from "./support/test";
import { mockApi, paged, plant, region, user } from "./support/workspace";

type Account = typeof user;
type Browser = { account: Account | null };

const other: Account = {
    ...user,
    id: 2,
    username: "other.user",
    email: "other@example.test",
};
const generationKey = "pyracast.sessionGeneration";

function deferred() {
    let resolve!: () => void;
    const promise = new Promise<void>((done) => (resolve = done));
    return { promise, resolve };
}

const arrayOf = (account: Account | null) =>
    account?.id === other.id ? "Second account array" : "East array";

// The tabs of one context share the session cookie, so one account stands for the browser's session.
async function serve(tab: Page, browser: Browser) {
    await mockApi(tab);
    await tab.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: browser.account,
                csrf_token: browser.account
                    ? `token-${browser.account.id}`
                    : "anonymous-token",
            },
        }),
    );
    await tab.route("**/api/auth/browser/v1/auth/login", (route) => {
        browser.account =
            route.request().postDataJSON().email === other.email ? other : user;
        return route.fulfill({
            json: { status: 200, meta: { is_authenticated: true } },
        });
    });
    await tab.route("**/api/auth/browser/v1/auth/session", (route) => {
        if (route.request().method() !== "DELETE") return route.fallback();
        browser.account = null;
        return route.fulfill({
            status: 401,
            json: { status: 401, meta: { is_authenticated: false } },
        });
    });
    await tab.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({
            json: paged([
                {
                    ...region,
                    plant: { ...plant, name: arrayOf(browser.account) },
                },
            ]),
        }),
    );
}

async function sharedSession(tabs: Page[], account: Account | null = user) {
    const browser: Browser = { account };
    for (const tab of tabs) await serve(tab, browser);
    return browser;
}

const signInButton = (tab: Page) =>
    tab.locator("form").getByRole("button", { name: "Sign in" });

const plantCard = (tab: Page, account: Account) =>
    tab.getByRole("button", { name: new RegExp(arrayOf(account)) });

async function signOut(tab: Page) {
    await tab.getByRole("button", { name: "Sign out" }).click();
    await expect(signInButton(tab)).toBeVisible();
}

async function submitSignIn(tab: Page, account: Account) {
    await tab.getByLabel("Email", { exact: true }).fill(account.email);
    await tab
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await signInButton(tab).click();
}

async function switchAccount(tab: Page, from: Account, to: Account) {
    await tab.goto("/app/w/1/");
    await expect(plantCard(tab, from)).toBeVisible();
    await signOut(tab);
    await submitSignIn(tab, to);
    await expect(plantCard(tab, to)).toBeVisible();
}

const sessionRead = (request: Request) =>
    request.url().endsWith("/api/session/") && request.method() === "GET";

// A session read this tab sends now; it travels behind every session request the tab queued before.
async function confirmByFocus(tab: Page) {
    const read = tab.waitForEvent("requestfinished", sessionRead);
    await tab.evaluate(() => window.dispatchEvent(new Event("focus")));
    await read;
}

test("a save in flight while another session takes over is neither repeated nor applied", async ({
    page,
    context,
}) => {
    const second = await context.newPage();
    await sharedSession([page, second]);
    const saves: { token?: string; body: unknown }[] = [];
    const saveSeen = deferred();
    const saveHeld = deferred();
    await page.route(
        "**/api/tenants/1/regions/11/reminders/",
        async (route) => {
            if (route.request().method() !== "PUT") return route.fallback();
            saves.push({
                token: route.request().headers()["x-csrftoken"],
                body: route.request().postDataJSON(),
            });
            saveSeen.resolve();
            await saveHeld.promise;
            await route.fulfill({
                status: 401,
                json: errorBody("not_authenticated", "Sign in."),
            });
        },
    );
    await page.goto("/app/w/1/plants/11/settings/?view=reminders");
    await page.getByLabel("Email language").selectOption("tr");
    await page.getByRole("button", { name: "Save reminders" }).click();
    await saveSeen.promise;
    await switchAccount(second, user, other);
    await expect(
        page.getByRole("heading", { name: arrayOf(other), level: 1 }),
    ).toBeVisible();
    const answered = page.waitForEvent(
        "requestfinished",
        (request) => request.method() === "PUT",
    );
    saveHeld.resolve();
    await answered;
    await confirmByFocus(page);
    expect(saves).toEqual([
        { token: "token-1", body: expect.objectContaining({ language: "tr" }) },
    ]);
    await expect(
        page.getByRole("heading", { name: arrayOf(other), level: 1 }),
    ).toBeVisible();
    await expect(page.getByLabel("Email language")).toHaveValue("en");
});

test("the same account signed in again in another tab shows none of the earlier session's data", async ({
    page,
    context,
}) => {
    const second = await context.newPage();
    const browser = await sharedSession([page, second]);
    let renamed = false;
    let holding = false;
    let readsHeld = false;
    const readGate = deferred();
    const sitesSeen = deferred();
    const sitesHeld = deferred();
    await page.route("**/api/session/", async (route) => {
        if (readsHeld) await readGate.promise;
        await route.fallback();
    });
    await page.route("**/api/tenants/1/sites/", async (route) => {
        if (holding) {
            holding = false;
            sitesSeen.resolve();
            await sitesHeld.promise;
        }
        await route.fulfill({
            json: paged([
                {
                    ...region,
                    plant: {
                        ...plant,
                        name: renamed ? "Renamed array" : arrayOf(user),
                    },
                },
            ]),
        });
    });
    await page.goto("/app/w/1/");
    await expect(plantCard(page, user)).toBeVisible();
    renamed = true;
    holding = true;
    readsHeld = true;
    await switchAccount(second, user, user);
    expect(browser.account).toBe(user);
    readGate.resolve();
    await sitesSeen.promise;
    await expect(page.getByText("Loading plants")).toBeVisible();
    await expect(plantCard(page, user)).toHaveCount(0);
    sitesHeld.resolve();
    await expect(
        page.getByRole("button", { name: /Renamed array/ }),
    ).toBeVisible();
    await expect(plantCard(page, user)).toHaveCount(0);
});

test("Use another account on the invitation screen leaves no other tab on the earlier session", async ({
    page,
    context,
}) => {
    const second = await context.newPage();
    await sharedSession([page, second]);
    await page.goto("/app/w/1/");
    await expect(plantCard(page, user)).toBeVisible();
    await second.goto("/app/invitation/#token=invite-secret");
    await second
        .getByRole("button", { name: "Use another account", exact: true })
        .click();
    await expect(signInButton(second)).toBeVisible();
    await expect(signInButton(page)).toBeVisible();
    await expect(plantCard(page, user)).toHaveCount(0);
});

test("two tabs signing in as different accounts leave every tab on the session the browser holds", async ({
    page,
    context,
}) => {
    const second = await context.newPage();
    const third = await context.newPage();
    const browser = await sharedSession([page, second, third], null);
    const logins = new Map<Page, ReturnType<typeof deferred>>();
    for (const tab of [page, second]) {
        const held = deferred();
        const seen = deferred();
        logins.set(tab, held);
        await tab.route("**/api/auth/browser/v1/auth/login", async (route) => {
            seen.resolve();
            await held.promise;
            await route.fallback();
        });
        await tab.goto("/app/");
        await expect(signInButton(tab)).toBeVisible();
        await submitSignIn(tab, tab === page ? user : other);
        await seen.promise;
    }
    await third.goto("/app/");
    await expect(signInButton(third)).toBeVisible();
    logins.get(page)!.resolve();
    await expect(plantCard(page, user)).toBeVisible();
    await expect(plantCard(third, user)).toBeVisible();
    logins.get(second)!.resolve();
    await expect(plantCard(second, other)).toBeVisible();
    expect(browser.account).toBe(other);
    for (const tab of [page, second, third]) {
        await expect(plantCard(tab, other)).toBeVisible();
        await expect(plantCard(tab, user)).toHaveCount(0);
    }
});

test("a deletion answered after the session changed does not move the new session's address", async ({
    page,
    context,
}) => {
    const second = await context.newPage();
    await sharedSession([page, second]);
    const deleteSeen = deferred();
    const deleteHeld = deferred();
    await page.route("**/api/tenants/1/regions/11/", async (route) => {
        if (route.request().method() !== "DELETE") return route.fallback();
        deleteSeen.resolve();
        await deleteHeld.promise;
        await route.fulfill({ status: 204 });
    });
    await page.goto("/app/w/1/plants/11/settings/");
    await page.getByRole("button", { name: "Delete plant" }).click();
    await page
        .getByRole("button", { name: "Move plant to recycle bin" })
        .click();
    await deleteSeen.promise;
    await switchAccount(second, user, other);
    await expect(
        page.getByRole("heading", { name: arrayOf(other), level: 1 }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Account" }).click();
    await expect(page).toHaveURL("/app/account/");
    const navigations: string[] = [];
    page.on("request", (request) => {
        const url = new URL(request.url());
        if (url.searchParams.has("_rsc")) navigations.push(url.pathname);
    });
    const answered = page.waitForEvent(
        "requestfinished",
        (request) => request.method() === "DELETE",
    );
    deleteHeld.resolve();
    await answered;
    await confirmByFocus(page);
    expect(navigations).toEqual([]);
    await expect(page).toHaveURL("/app/account/");
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
});

test("a tab sends at most one session read and queues one more however often the session changes", async ({
    page,
    context,
}) => {
    const second = await context.newPage();
    await sharedSession([page, second]);
    let reads = 0;
    let counting = false;
    const readSeen = deferred();
    const readHeld = deferred();
    await page.route("**/api/session/", async (route) => {
        if (counting && ++reads === 1) {
            readSeen.resolve();
            await readHeld.promise;
        }
        await route.fallback();
    });
    await page.goto("/app/w/1/");
    await expect(plantCard(page, user)).toBeVisible();
    await second.goto("/app/w/1/");
    await expect(plantCard(second, user)).toBeVisible();
    counting = true;
    await signOut(second);
    await readSeen.promise;
    await submitSignIn(second, user);
    await expect(plantCard(second, user)).toBeVisible();
    await signOut(second);
    await submitSignIn(second, user);
    await expect(plantCard(second, user)).toBeVisible();
    const latest = await second.evaluate(
        (key) => localStorage.getItem(key),
        generationKey,
    );
    expect(latest).toBeTruthy();
    await page.evaluate(
        ([key, value]) =>
            new Promise<void>((resolve) => {
                if (localStorage.getItem(key) === value) resolve();
                window.addEventListener("storage", (event) => {
                    if (event.key === key && event.newValue === value)
                        resolve();
                });
            }),
        [generationKey, latest] as const,
    );
    expect(reads).toBe(1);
    readHeld.resolve();
    await expect(plantCard(page, user)).toBeVisible();
    await confirmByFocus(page);
    expect(reads).toBe(3);
});
