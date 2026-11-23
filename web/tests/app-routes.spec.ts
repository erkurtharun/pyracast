import { expect, test, type Page } from "./support/test";
import {
    mockApi,
    paged,
    plant,
    region,
    tenantRow,
    user,
} from "./support/workspace";

async function mockMembers(page: Page) {
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [{ id: 1, email: user.email, role: "admin" }],
            },
        }),
    );
    await page.route("**/api/tenants/1/invitations/", (route) =>
        route.fulfill({ json: { invitations: [] } }),
    );
}

test("refresh and Back keep the workspace, the view and the forecast date", async ({
    page,
}) => {
    await mockApi(page);
    await mockMembers(page);
    await page.goto("/app/");
    await expect(page).toHaveURL("/app/w/1/");
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/forecast/");
    await page.getByLabel("Forecast date").fill("2026-11-25");
    await expect(page).toHaveURL(
        "/app/w/1/plants/11/forecast/?date=2026-11-25",
    );
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    await expect(page).toHaveURL(
        "/app/w/1/plants/11/forecast/?date=2026-11-25&view=irradiance",
    );
    await page.reload();
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-25");
    await expect(
        page.getByRole("button", { name: "Irradiance", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/production/");
    await page.goBack();
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-25");
    await expect(
        page.getByRole("button", { name: "Irradiance", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await page.getByRole("button", { name: /All plants/ }).click();
    await expect(page).toHaveURL("/app/w/1/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    await expect(page).toHaveURL("/app/w/1/members/");
    await page.reload();
    await expect(
        page.getByRole("heading", { name: "Members", exact: true, level: 1 }),
    ).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL("/app/w/1/");
    await expect(page.getByRole("heading", { name: /Plants/ })).toBeVisible();
});

test("refresh and Back keep the production day and the plant sub-views", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-11-19T12:00:00Z"));
    await mockApi(page);
    await page.goto("/app/w/1/plants/11/production/");
    const day = page.getByLabel("Day", { exact: true });
    const current = (name: string) =>
        expect(page.getByRole("button", { name, exact: true })).toHaveAttribute(
            "aria-current",
            "page",
        );
    await expect(day).toHaveValue("2026-11-18");
    await day.fill("2026-11-10");
    await expect(page).toHaveURL(
        "/app/w/1/plants/11/production/?date=2026-11-10",
    );
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    await expect(page).toHaveURL(
        "/app/w/1/plants/11/production/?date=2026-11-10&view=comparison",
    );
    await page.reload();
    await current("Comparison");
    await page.getByRole("button", { name: "Actuals", exact: true }).click();
    await expect(page).toHaveURL(
        "/app/w/1/plants/11/production/?date=2026-11-10",
    );
    await expect(day).toHaveValue("2026-11-10");
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/forecast/");
    await page.goBack();
    await expect(day).toHaveValue("2026-11-10");
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
        .getByRole("button", { name: "Email reminders", exact: true })
        .click();
    await expect(page).toHaveURL("/app/w/1/plants/11/settings/?view=reminders");
    await page.reload();
    await current("Email reminders");
});

test("the workspace lives in the address and survives a reload", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
                tenantRow({ id: 2, name: "Second Energy", role: "viewer" }),
            ]),
        }),
    );
    await page.route("**/api/tenants/2/sites/", (route) =>
        route.fulfill({
            json: paged([
                {
                    ...region,
                    id: 12,
                    name: "Konya field",
                    plant: { ...plant, id: 22, name: "West array" },
                },
            ]),
        }),
    );
    await page.goto("/app/w/2/");
    await expect(
        page.getByRole("button", { name: /West array/ }),
    ).toBeVisible();
    await expect(
        page.getByRole("combobox", { name: "Workspace", exact: true }),
    ).toHaveValue("2");
    await page
        .getByRole("combobox", { name: "Workspace", exact: true })
        .selectOption("1");
    await expect(page).toHaveURL("/app/w/1/");
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
    await page.reload();
    await expect(
        page.getByRole("combobox", { name: "Workspace", exact: true }),
    ).toHaveValue("1");
    await page.goBack();
    await expect(page).toHaveURL("/app/w/2/");
    await expect(
        page.getByRole("button", { name: /West array/ }),
    ).toBeVisible();
});

test("an unknown workspace in the address shows no other workspace's plants", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/w/9/");
    await expect(
        page.getByText("This workspace is not available to your account."),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /East array/ })).toHaveCount(
        0,
    );
});

test("the document keeps its title while the workspace changes address", async ({
    page,
}) => {
    await mockApi(page);
    await page.addInitScript(() => {
        const titles: string[] = [];
        Object.assign(window, { titles });
        new MutationObserver(() => {
            const count = document.querySelectorAll("title").length;
            if (count || titles.length)
                titles.push(`${count}:${document.title}`);
        }).observe(document, {
            subtree: true,
            childList: true,
            characterData: true,
        });
    });
    const titlesKeptSinceFirst = async () => {
        const titles = await page.evaluate(
            () => (window as unknown as { titles: string[] }).titles,
        );
        expect(titles.length).toBeGreaterThan(0);
        expect(
            titles.filter(
                (title) => !/^1:(Pyracast|\S.* \| Pyracast)$/.test(title),
            ),
        ).toEqual([]);
    };
    // The observer starts at the first title; the server head shows the document starts with it.
    const titledFromStart = async (address: string) => {
        const html = await (await page.goto(address))!.text();
        expect(html.match(/<title>[^<]*<\/title>/g)).toEqual([
            "<title>Pyracast</title>",
        ]);
        expect(html.indexOf("<title>")).toBeLessThan(html.indexOf("</head>"));
    };
    await titledFromStart("/app/w/1/");
    await expect(page).toHaveTitle("Plants · Sunfield Energy | Pyracast");
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(page).toHaveURL(/\/plants\/\d+\/forecast\/$/);
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page).toHaveURL(/\/plants\/\d+\/settings\/$/);
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(page).toHaveURL(/\/plants\/\d+\/forecast\/$/);
    await page.getByRole("button", { name: "Account" }).click();
    await expect(page).toHaveURL("/app/account/");
    await expect(page).toHaveTitle("Account settings | Pyracast");
    await titlesKeptSinceFirst();
    await titledFromStart("/app/w/1/plants/11/unknown/");
    await expect(
        page.getByRole("heading", { name: "Page not found" }),
    ).toBeVisible();
    await expect(page).toHaveTitle("Pyracast");
    expect(await page.locator("title").count()).toBe(1);
    await titlesKeptSinceFirst();
    await page.route(
        (url) =>
            url.pathname.startsWith("/api/") &&
            url.pathname.endsWith("/plants/21/forecast/"),
        (route) =>
            route.fulfill({
                json: {
                    timezone: region.timezone,
                    freshness: "fresh",
                    result: {},
                    production_diagnostic: { days: null },
                },
            }),
    );
    await titledFromStart("/app/w/1/plants/11/forecast/");
    await expect(
        page.getByRole("heading", { name: "This page could not be displayed" }),
    ).toBeVisible();
    await expect(page).toHaveTitle("Pyracast");
    expect(await page.locator("title").count()).toBe(1);
    await titlesKeptSinceFirst();
});

test("the workspace document carries no message catalog", async ({ page }) => {
    await mockApi(page);
    const html = await (await page.goto("/app/w/1/"))!.text();
    expect(html).not.toContain(
        "Language could not be saved. Please try again.",
    );
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
});

test("plant sections carry their own document title", async ({ page }) => {
    await mockApi(page);
    await page.goto("/app/w/1/plants/11/forecast/");
    await expect(page).toHaveTitle("Forecast · East array | Pyracast");
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/settings/");
    await expect(page).toHaveTitle("Settings · East array | Pyracast");
    await page.getByRole("button", { name: /All plants/ }).click();
    await expect(page).toHaveURL("/app/w/1/");
    await expect(page).toHaveTitle("Plants · Sunfield Energy | Pyracast");
    expect(await page.locator("title").count()).toBe(1);
});

test("a plant screen that fails to render keeps the workspace around its error", async ({
    page,
}) => {
    await mockApi(page);
    await page.route(
        (url) =>
            url.pathname.startsWith("/api/") &&
            url.pathname.endsWith("/plants/21/forecast/"),
        (route) =>
            route.fulfill({
                json: {
                    timezone: region.timezone,
                    freshness: "fresh",
                    result: {},
                    production_diagnostic: { days: null },
                },
            }),
    );
    await page.goto("/app/w/1/plants/11/forecast/");
    await expect(
        page.getByRole("heading", { name: "This page could not be displayed" }),
    ).toBeVisible();
    await expect(
        page.getByRole("combobox", { name: "Workspace", exact: true }),
    ).toHaveValue("1");
    await expect(page.getByRole("main")).toHaveCount(1);
    await page.getByRole("button", { name: /Plants/ }).click();
    await expect(page).toHaveURL("/app/w/1/");
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
});

test("account settings have their own address", async ({ page }) => {
    await mockApi(page);
    await page.goto("/app/account/");
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: /Plants/ }).click();
    await expect(page).toHaveURL("/app/w/1/");
});

test("no legacy entry path remains", async ({ page }) => {
    await mockApi(page);
    const accept = await page.goto("/invitations/accept/#token=invite-secret");
    expect(accept?.status()).toBe(404);
    const home = await page.goto("/?account=security");
    expect(home?.status()).toBe(200);
    await expect(page).toHaveURL("/?account=security");
    for (const legacy of [
        "/app/#token=invite-secret",
        "/app/?token=invite-secret",
        "/app/?account=security",
        "/app/#plant-11/settings",
    ]) {
        await page.goto(legacy);
        await expect(page, legacy).toHaveURL("/app/w/1/");
        await expect(
            page.getByRole("heading", { name: /Plants/ }),
            legacy,
        ).toBeVisible();
        await expect(
            page.getByRole("heading", { name: "Join workspace" }),
        ).toHaveCount(0);
    }
});

test("an invitation link opens the acceptance screen and drops the token from the address", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(
        page.getByRole("heading", { name: "Join workspace" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/invitation/");
});

for (const anonymous of [false, true])
    test(`an invitation survives blocked session storage ${anonymous ? "through sign-in" : "when signed in"}`, async ({
        page,
    }) => {
        await page.addInitScript(() => {
            Storage.prototype.setItem = () => {
                throw new DOMException("Blocked", "SecurityError");
            };
        });
        await mockApi(page, { anonymous });
        await page.goto("/app/invitation/#token=invite-secret");
        await expect(page).toHaveURL("/app/invitation/");
        if (anonymous) {
            await expect(
                page.getByRole("button", {
                    name: "Cancel invitation",
                    exact: true,
                }),
            ).toBeVisible();
            await page
                .getByLabel("Email", { exact: true })
                .fill("solar@example.test");
            await page
                .getByLabel("Password", { exact: true })
                .fill("Test-only-cobalt-79!");
            await page
                .locator("form")
                .getByRole("button", { name: "Sign in" })
                .click();
        }
        await expect(
            page.getByRole("heading", { name: "Join workspace" }),
        ).toBeVisible();
        await expect(page).toHaveURL("/app/invitation/");
    });

test("a sign-out or another account never shows the previous account's cached plants", async ({
    page,
}) => {
    await mockApi(page);
    let account = 1;
    let secondSignedIn = false;
    let releaseSites!: () => void;
    const sitesHeld = new Promise<void>((resolve) => {
        releaseSites = resolve;
    });
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        secondSignedIn = account === 2;
        return route.fallback();
    });
    await page.route("**/api/session/", (route) =>
        secondSignedIn
            ? route.fulfill({
                  json: {
                      user: { ...user, id: 2, email: "other@example.test" },
                      csrf_token: "signed-token",
                  },
              })
            : route.fallback(),
    );
    await page.route("**/api/tenants/1/sites/", async (route) => {
        if (!secondSignedIn) return route.fallback();
        await sitesHeld;
        await route.fulfill({
            json: paged([
                {
                    ...region,
                    id: 13,
                    plant: { ...plant, id: 23, name: "Second account array" },
                },
            ]),
        });
    });
    await page.goto("/app/");
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(
        page.locator("form").getByRole("button", { name: "Sign in" }),
    ).toBeVisible();
    account = 2;
    await page.getByLabel("Email", { exact: true }).fill("other@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Loading plants")).toBeVisible();
    await expect(page.getByRole("button", { name: /East array/ })).toHaveCount(
        0,
    );
    releaseSites();
    await expect(
        page.getByRole("button", { name: /Second account array/ }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /East array/ })).toHaveCount(
        0,
    );
});

test("a tab adopts another account signed in in a second tab at its focus read and keeps none of the first account's data", async ({
    page,
    context,
}) => {
    const other = { ...user, id: 2, email: "other@example.test" };
    let account: "first" | "none" | "second" = "first";
    let readSeen!: () => void;
    const readRequested = new Promise<void>((resolve) => (readSeen = resolve));
    const second = await context.newPage();
    for (const tab of [page, second]) {
        await mockApi(tab);
        await tab.route("**/api/auth/browser/v1/auth/login", (route) => {
            account = "second";
            return route.fallback();
        });
        await tab.route("**/api/auth/browser/v1/auth/session", (route) => {
            if (route.request().method() === "DELETE") account = "none";
            return route.fallback();
        });
        await tab.route("**/api/session/", async (route) => {
            const answered = account;
            if (tab === page && answered === "second") readSeen();
            await route.fulfill({
                json:
                    answered === "none"
                        ? { user: null, csrf_token: "anonymous-token" }
                        : {
                              user: answered === "second" ? other : user,
                              csrf_token: "signed-token",
                          },
            });
        });
        await tab.route("**/api/tenants/1/sites/", (route) =>
            account === "second"
                ? route.fulfill({
                      json: paged([
                          {
                              ...region,
                              id: 13,
                              plant: {
                                  ...plant,
                                  id: 23,
                                  name: "Second account array",
                              },
                          },
                      ]),
                  })
                : route.fallback(),
        );
        await tab.route("**/api/tenants/1/members/", (route) =>
            route.fulfill({
                json: {
                    members: [
                        {
                            id: account === "second" ? other.id : user.id,
                            email:
                                account === "second" ? other.email : user.email,
                            role: "admin",
                        },
                    ],
                },
            }),
        );
        await tab.route("**/api/tenants/1/invitations/", (route) =>
            route.fulfill({ json: { invitations: [] } }),
        );
    }
    await page.goto("/app/w/1/members/");
    await expect(page.getByText(user.email).first()).toBeVisible();
    await page.getByRole("button", { name: /Plants/ }).click();
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
    await second.goto("/app/");
    await expect(
        second.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
    await second.getByRole("button", { name: "Sign out" }).click();
    await expect(
        second.locator("form").getByRole("button", { name: "Sign in" }),
    ).toBeVisible();
    await second.getByLabel("Email", { exact: true }).fill(other.email);
    await second
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await second
        .locator("form")
        .getByRole("button", { name: "Sign in" })
        .click();
    await expect(
        second.getByRole("button", { name: /Second account array/ }),
    ).toBeVisible();
    await page.bringToFront();
    // Headless Chromium fires no focus event for bringToFront.
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await readRequested;
    await expect(
        page.getByRole("button", { name: /Second account array/ }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /East array/ })).toHaveCount(
        0,
    );
    await page.getByRole("button", { name: "Members", exact: true }).click();
    await expect(page).toHaveURL("/app/w/1/members/");
    await expect(page.getByText(other.email).first()).toBeVisible();
    await expect(page.getByText(user.email)).toHaveCount(0);
});
